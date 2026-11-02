"""遗传算法优化（GA）任务模式。

流程
----
1. 从 ``DesignSpec``（自然语言需求 + 上传附件解析 + 可选 seed vsp3）得到基线几何与工况；
2. 定义 6 个设计变量：翼面积 S、展弦比 AR、梢根比 taper、后掠角 sweep、上反角 dihedral、
   尾翼面积比 tail_scale，边界取基线 ±20%~40% 并受翼展上限等约束裁剪；
3. 每个体真实调用 OpenVSP 建模 + VSPAERO（巡航迎角附近少量点）评估巡航 L/D，
   并施加失速余量 / 静稳定裕度 / 翼展约束惩罚；
4. 锦标赛选择 + 混合交叉 + 高斯变异 + 精英保留，按基因取整缓存避免重复评估；
5. 每代 stdout 输出 ``[ga] gen=k best=... avg=...``；总评估 ≤40 次、总时间 ≤ time_budget_s；
6. 最优基因重建几何 → 完整气动/稳定性分析 → 保存 ``ga_optimized.vsp3`` →
   生成含收敛表/图的 Word 优化报告。

本模块不依赖 LLM；``llm_bounds=True`` 且存在可用 API key 时，仅尝试让模型给出
参数边界建议（失败自动忽略），GA 评估始终走真实 OpenVSP/VSPAERO 计算。
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import AIConfig
from .designer import GRAVITY, DesignSpec, Designer, size_aircraft
from .logging_util import Logger
from .vsp_bridge import (
    VSPBridge,
    VSPError,
    standard_atmosphere,
    summarize_case_parameters,
    summarize_polar,
)

GENE_NAMES = ("wing_area_m2", "aspect_ratio", "taper_ratio",
              "sweep_deg", "dihedral_deg", "tail_scale")
GENE_LABELS = {
    "wing_area_m2": "机翼面积 S (m²)",
    "aspect_ratio": "展弦比 AR",
    "taper_ratio": "梢根比 λ",
    "sweep_deg": "后掠角 (°)",
    "dihedral_deg": "上反角 (°)",
    "tail_scale": "尾翼面积比",
}
VALID_OBJECTIVES = ("max_ld", "max_cl", "min_cd")
OBJECTIVE_LABELS = {
    "max_ld": "巡航升阻比最大",
    "max_cl": "最大升力系数最大",
    "min_cd": "巡航阻力系数最小",
}

#: 评估时清理的 VSPAERO 大文件后缀（节省磁盘）
_EVAL_CLEANUP_SUFFIXES = (
    ".adb", ".adb.cases", ".history", ".lod", ".flt", ".vspgeom", ".vkey",
    ".csf", ".group.0", ".group.1", ".ALL.taglist", ".ControlSurfaces.taglist",
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _num(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


# --------------------------------------------------------------------------- #
# GA 配置
# --------------------------------------------------------------------------- #
@dataclass
class GAConfig:
    """GA 运行参数（缺省值可被 ai_job.json 的 ``ga`` 对象覆盖）。"""

    population: int = 8
    generations: int = 4
    time_budget_s: float = 300.0
    objective: str = "max_ld"
    max_evaluations: int = 40
    seed: int = 42
    elite_count: int = 2
    tournament_size: int = 3
    crossover_rate: float = 0.8
    mutation_rate: float = 0.35
    aoa_npts: int = 5
    wake_iters: int = 2
    n_cpu: int = 4
    sm_tolerance: float = 0.05
    immigrant_fraction: float = 0.1
    llm_bounds: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "GAConfig":
        raw = raw if isinstance(raw, dict) else {}
        objective = str(raw.get("objective") or "max_ld").strip().lower()
        if objective not in VALID_OBJECTIVES:
            objective = "max_ld"
        population = int(_clamp(_num(raw.get("population"), 8), 2, 24))
        generations = int(_clamp(_num(raw.get("generations"), 4), 1, 10))
        elite = int(_clamp(_num(raw.get("elite_count"), 2), 1,
                           max(1, population // 2)))
        return cls(
            population=population,
            generations=generations,
            time_budget_s=_clamp(_num(raw.get("time_budget_s"), 300.0), 30.0, 1800.0),
            objective=objective,
            max_evaluations=int(_clamp(_num(raw.get("max_evaluations"), 40), 2, 40)),
            seed=int(_num(raw.get("seed"), 42)),
            elite_count=elite,
            tournament_size=int(_clamp(_num(raw.get("tournament_size"), 3), 2, 6)),
            crossover_rate=_clamp(_num(raw.get("crossover_rate"), 0.8), 0.0, 1.0),
            mutation_rate=_clamp(_num(raw.get("mutation_rate"), 0.35), 0.0, 1.0),
            aoa_npts=int(_clamp(_num(raw.get("aoa_npts"), 5), 3, 9)),
            wake_iters=int(_clamp(_num(raw.get("wake_iters"), 2), 1, 5)),
            n_cpu=int(_clamp(_num(raw.get("n_cpu"), 4), 1, 16)),
            sm_tolerance=_clamp(_num(raw.get("sm_tolerance"), 0.05), 0.01, 0.2),
            immigrant_fraction=_clamp(_num(raw.get("immigrant_fraction"), 0.1), 0.0, 0.4),
            llm_bounds=bool(raw.get("llm_bounds", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "population": self.population,
            "generations": self.generations,
            "time_budget_s": self.time_budget_s,
            "objective": self.objective,
            "objective_label": OBJECTIVE_LABELS.get(self.objective, self.objective),
            "max_evaluations": self.max_evaluations,
            "seed": self.seed,
            "elite_count": self.elite_count,
            "tournament_size": self.tournament_size,
            "crossover_rate": self.crossover_rate,
            "mutation_rate": self.mutation_rate,
            "aoa_npts": self.aoa_npts,
            "wake_iters": self.wake_iters,
            "n_cpu": self.n_cpu,
            "sm_tolerance": self.sm_tolerance,
            "immigrant_fraction": self.immigrant_fraction,
            "llm_bounds": self.llm_bounds,
        }


# --------------------------------------------------------------------------- #
# 几何 / 工况辅助
# --------------------------------------------------------------------------- #
def _genome_from_geometry(geometry: dict[str, Any]) -> dict[str, float]:
    wing = geometry.get("wing") or {}
    sizing = geometry.get("sizing") or {}
    return {
        "wing_area_m2": _num(sizing.get("area_m2"), _num(wing.get("area_m2"), 1.0)),
        "aspect_ratio": _num(sizing.get("aspect_ratio"),
                             _num(wing.get("aspect_ratio"), 8.0)),
        "taper_ratio": _num(wing.get("taper_ratio"), 0.6),
        "sweep_deg": _num(wing.get("sweep_deg"), 0.0),
        "dihedral_deg": _num(wing.get("dihedral_deg"), 2.0),
        "tail_scale": 1.0,
    }


def genome_to_optimization(genome: dict[str, float]) -> dict[str, float]:
    """把基因转换为 ``designer.size_aircraft`` 的 optimization 覆盖参数。"""
    return {
        "wing_area_m2": round(float(genome["wing_area_m2"]), 4),
        "aspect_ratio": round(float(genome["aspect_ratio"]), 4),
        "taper_ratio": round(float(genome["taper_ratio"]), 4),
        "sweep_deg": round(float(genome["sweep_deg"]), 3),
        "dihedral_deg": round(float(genome["dihedral_deg"]), 3),
        "tail_scale": round(float(genome["tail_scale"]), 4),
    }


def make_genome_bounds(baseline: dict[str, float], spec: DesignSpec
                       ) -> dict[str, dict[str, float]]:
    """以基线为中心生成变量边界（含翼展上限等约束裁剪）。"""
    span_limit = float(spec.max_span_m) if spec.max_span_m else float("inf")
    area_base = max(float(baseline["wing_area_m2"]), 0.05)
    ar_base = max(float(baseline["aspect_ratio"]), 1.0)
    bounds: dict[str, dict[str, float]] = {}
    bounds["wing_area_m2"] = {"low": area_base * 0.80, "high": area_base * 1.20}
    ar_high = ar_base * 1.35
    if span_limit < float("inf"):
        ar_high = min(ar_high, (span_limit ** 2) / max(bounds["wing_area_m2"]["low"], 1e-6))
    bounds["aspect_ratio"] = {"low": max(3.0, ar_base * 0.75),
                              "high": max(ar_base * 0.76, min(16.0, ar_high))}
    bounds["taper_ratio"] = {"low": 0.30, "high": 0.85}
    bounds["sweep_deg"] = {"low": -5.0, "high": 20.0}
    bounds["dihedral_deg"] = {"low": 0.0, "high": 8.0}
    bounds["tail_scale"] = {"low": 0.60, "high": 1.50}
    for name, item in bounds.items():
        item["low"] = min(item["low"], float(baseline[name]))
        item["high"] = max(item["high"], float(baseline[name]))
        item["baseline"] = float(baseline[name])
    return bounds


def build_operating_conditions(spec: DesignSpec,
                               overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """从 spec 与可选 operating_conditions 覆盖构造 GA 评估工况。"""
    overrides = overrides if isinstance(overrides, dict) else {}
    altitude_m = _num(overrides.get("altitude_m"), float(spec.altitude_m))
    velocity = _num(overrides.get("speed_cruise_ms"),
                    _num(overrides.get("velocity_ms"), float(spec.speed_cruise_ms)))
    atmosphere = standard_atmosphere(altitude_m)
    density = _num(overrides.get("rho"), atmosphere["density_kgm3"])
    sound_speed = _num(overrides.get("sound_speed_ms"), atmosphere["sound_speed_ms"])
    return {
        "altitude_m": round(altitude_m, 2),
        "speed_cruise_ms": round(velocity, 3),
        "density_kgm3": round(density, 6),
        "sound_speed_ms": round(sound_speed, 3),
        "temperature_k": round(atmosphere["temperature_k"], 3),
        "mach": round(velocity / max(sound_speed, 1e-6), 5),
        "source": "operating_conditions" if overrides else "design_spec",
    }


def _fit_polar_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """对少量 polar 点拟合 CL(α) 直线与 CD(CL) 二次曲线。"""
    alphas: list[float] = []
    lifts: list[float] = []
    drags: list[float] = []
    for row in rows:
        try:
            beta = float(row.get("Beta", 0.0))
        except (TypeError, ValueError):
            beta = 0.0
        if abs(beta) > 1e-6:
            continue
        try:
            alpha = float(row.get("AoA"))
            lift = float(row.get("CLtot", row.get("CL")))
            drag = float(row.get("CDtot", row.get("CD")))
        except (TypeError, ValueError):
            continue
        if any(math.isnan(value) for value in (alpha, lift, drag)):
            continue
        alphas.append(alpha)
        lifts.append(lift)
        drags.append(drag)
    if len(alphas) < 2:
        return {"ok": False, "error": "有效 polar 点不足", "count": len(alphas)}
    try:
        import numpy as np

        alpha_array = np.asarray(alphas, dtype=float)
        cl_array = np.asarray(lifts, dtype=float)
        cd_array = np.asarray(drags, dtype=float)
        slope, intercept = np.polyfit(alpha_array, cl_array, 1)
        cd_coeffs = np.polyfit(cl_array, cd_array, 2)
        cl_max = float(np.max(cl_array))
        alpha_cl_max = float(alpha_array[int(np.argmax(cl_array))])
        coefficient = dict(zip(("cd_k", "cd_linear", "cd0"), [float(v) for v in cd_coeffs]))
        return {
            "ok": True,
            "count": len(alphas),
            "cl_alpha_per_deg": float(slope),
            "cl_alpha_per_rad": float(slope) * 180.0 / math.pi,
            "cl0": float(intercept),
            "cd0": coefficient["cd0"],
            "cd_k": coefficient["cd_k"],
            "cl_max_polar": cl_max,
            "alpha_at_cl_max_deg": alpha_cl_max,
            "cd_at": lambda cl: (coefficient["cd_k"] * cl ** 2
                                 + coefficient["cd_linear"] * cl
                                 + coefficient["cd0"]),
        }
    except Exception as exc:  # pragma: no cover - numpy 之外兜底
        # 手动最小二乘降级
        n = len(alphas)
        sum_a = sum(alphas)
        sum_cl = sum(lifts)
        sum_aa = sum(a * a for a in alphas)
        sum_ac = sum(a * c for a, c in zip(alphas, lifts))
        denominator = n * sum_aa - sum_a ** 2
        slope = ((n * sum_ac - sum_a * sum_cl) / denominator
                 if abs(denominator) > 1e-12 else 0.0)
        intercept = (sum_cl - slope * sum_a) / n
        cd0 = sum(drags) / n
        return {
            "ok": True,
            "count": n,
            "cl_alpha_per_deg": slope,
            "cl_alpha_per_rad": slope * 180.0 / math.pi,
            "cl0": intercept,
            "cd0": cd0,
            "cd_k": 0.0,
            "cl_max_polar": max(lifts),
            "alpha_at_cl_max_deg": alphas[lifts.index(max(lifts))],
            "cd_at": lambda cl: cd0 + 0.05 * cl * cl,
            "degraded": str(exc),
        }


# --------------------------------------------------------------------------- #
# GA 优化器
# --------------------------------------------------------------------------- #
class GAOptimizer:
    """遗传算法优化器（真实 OpenVSP/VSPAERO 评估，可注入 mock evaluator 用于测试）。"""

    def __init__(self, config: AIConfig, spec: DesignSpec, bridge: VSPBridge,
                 logger: Logger, output_dir: str | Path,
                 designer: Designer | None = None,
                 evaluator: Callable[[dict[str, float], int, Path], dict[str, Any]] | None = None):
        self.config = config
        self.spec = spec
        self.bridge = bridge
        self.logger = logger
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.eval_dir = self.output_dir / "ga_evaluations"
        self.eval_dir.mkdir(parents=True, exist_ok=True)
        self.designer = designer
        self.ga = GAConfig.from_dict(config.ga)
        # 工况覆盖（海拔/巡航速度可来自 ai_job.json 的 operating_conditions）
        self.operating_conditions = build_operating_conditions(
            spec, config.operating_conditions)
        if self.operating_conditions.get("source") == "operating_conditions":
            spec.altitude_m = self.operating_conditions["altitude_m"]
            spec.speed_cruise_ms = self.operating_conditions["speed_cruise_ms"]
            if spec.speed_cruise_ms < spec.speed_min_ms:
                spec.speed_min_ms = spec.speed_cruise_ms
            if spec.speed_cruise_ms > spec.speed_max_ms:
                spec.speed_max_ms = spec.speed_cruise_ms
        # 基线几何 & 基因
        self.baseline_geometry = size_aircraft(spec)
        self.baseline_genome = _genome_from_geometry(self.baseline_geometry)
        self.seed_model: str | None = None
        self.seed_parameters: dict[str, Any] | None = None
        #: case_model（上传案例）优先作为 GA 基线，其次 seed_model
        self.case_model: str | None = None
        self.case_parameters: dict[str, Any] | None = None
        self.based_on_case: bool = False
        self.baseline_source: str = "design_spec"
        self._load_seed_model()
        self.bounds = make_genome_bounds(self.baseline_genome, spec)
        if self.ga.llm_bounds:
            self._suggest_bounds_with_llm()
        self._rng = random.Random(self.ga.seed)
        self._evaluator = evaluator
        self._cache: dict[tuple, dict[str, Any]] = {}
        self.evaluations: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self._started = time.time()
        self.span_limit = (float(spec.max_span_m) if spec.max_span_m
                           else float("inf"))
        weight = spec.mtow_kg * GRAVITY
        area = float(self.baseline_geometry["sizing"]["area_m2"])
        velocity = float(self.operating_conditions["speed_cruise_ms"])
        rho = float(self.operating_conditions["density_kgm3"])
        self._cruise_cl = weight / max(0.5 * rho * velocity ** 2 * area, 1e-9)
        self._clmax_effective = max(spec.clmax * 0.92, 0.7)

    # ------------------------------------------------------------------ 初始化
    def _load_seed_model(self) -> None:
        """加载 GA 基线参考模型：``case_model`` 优先，其次 ``seed_model``。"""
        case_path = (getattr(self.config, "case_model", "") or "").strip()
        seed_path = (self.config.seed_model or "").strip()
        preferred = ""
        if case_path and Path(case_path).is_file():
            preferred = case_path
            self.baseline_source = "case_model"
        elif seed_path:
            preferred = seed_path
            self.baseline_source = "seed_model"
            if case_path:
                self.logger.warn(f"case_model 不可用，GA 回退 seed_model: {case_path}")
        if not preferred:
            return
        candidate = Path(preferred)
        if not candidate.is_file():
            self.logger.warn(f"{self.baseline_source} 不存在（忽略）: {preferred}")
            self.baseline_source = "design_spec"
            self.seed_model = None
            return
        self.seed_model = str(candidate)
        if preferred == case_path:
            self.case_model = str(candidate)
        try:
            params = self.bridge.read_model_parameters(candidate)
        except Exception as exc:  # pragma: no cover - 兼容性兜底
            self.logger.warn(f"读取 {self.baseline_source} 参数失败（仅记录路径）: {exc}")
            return
        self.seed_parameters = params
        if preferred == case_path:
            self.case_parameters = params
            self.based_on_case = bool(params.get("ok"))
        wing = params.get("wing") if isinstance(params, dict) else None
        if not wing:
            self.logger.warn(f"{self.baseline_source} 未解析到主翼参数，仅记录路径")
            return
        genome = dict(self.baseline_genome)
        for source_key, gene_key in (("area_m2", "wing_area_m2"),
                                     ("aspect_ratio", "aspect_ratio"),
                                     ("taper_ratio", "taper_ratio"),
                                     ("sweep_deg", "sweep_deg"),
                                     ("dihedral_deg", "dihedral_deg")):
            value = _num(wing.get(source_key), float("nan"))
            if not (math.isnan(value) or math.isinf(value)):
                genome[gene_key] = float(value)
        genome["wing_area_m2"] = _clamp(genome["wing_area_m2"], 0.05, 500.0)
        genome["aspect_ratio"] = _clamp(genome["aspect_ratio"], 2.0, 20.0)
        genome["taper_ratio"] = _clamp(genome["taper_ratio"], 0.2, 0.95)
        genome["sweep_deg"] = _clamp(genome["sweep_deg"], -20.0, 40.0)
        genome["dihedral_deg"] = _clamp(genome["dihedral_deg"], -5.0, 15.0)
        genome["tail_scale"] = 1.0
        self.baseline_genome = genome
        fuselage = params.get("fuselage") if isinstance(params.get("fuselage"),
                                                        dict) else {}
        source_label = ("上传案例 case_model" if self.based_on_case
                        else "seed_model")
        self.logger.status(
            f"{source_label} 作为 GA 基线: {candidate.name} → "
            f"S={genome['wing_area_m2']:.3f} m², AR={genome['aspect_ratio']:.2f}, "
            f"λ={genome['taper_ratio']:.2f}, 后掠={genome['sweep_deg']:.2f}°, "
            f"机身 L={fuselage.get('length_m')} m")

    def _suggest_bounds_with_llm(self) -> None:
        """可选：让 LLM 给出参数边界建议（失败自动忽略，不影响 GA 主流程）。"""
        try:
            from . import providers

            payload = {
                "baseline": {name: round(float(value), 4)
                             for name, value in self.baseline_genome.items()},
                "spec": {"mtow_kg": self.spec.mtow_kg,
                         "altitude_m": self.spec.altitude_m,
                         "speed_cruise_ms": self.spec.speed_cruise_ms,
                         "configuration": self.spec.configuration},
                "variables": list(GENE_NAMES),
            }
            messages = [
                {"role": "system", "content": "你是飞行器气动优化专家，只输出 JSON。"},
                {"role": "user", "content": (
                    "给定无人机设计基线，请给出各设计变量的合理优化范围（相对基线倍数，"
                    "最小值 min 与最大值 max，例如 0.8/1.2）。只输出 JSON 对象："
                    "{\"wing_area_m2\":{\"min\":0.8,\"max\":1.2}, ...}。基线：" +
                    json.dumps(payload, ensure_ascii=False))},
            ]
            response = providers.chat(
                self.config.base_url, self.config.effective_api_key,
                self.config.model, messages, temperature=0.2,
                timeout=min(self.config.timeout, 60.0))
            content = response.get("content") or ""
            start = content.find("{")
            end = content.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("响应中没有 JSON")
            suggestion = json.loads(content[start:end + 1])
            applied: dict[str, list[float]] = {}
            for name, item in suggestion.items():
                if name not in self.bounds or not isinstance(item, dict):
                    continue
                low_factor = _clamp(_num(item.get("min"), 0.8), 0.5, 1.0)
                high_factor = _clamp(_num(item.get("max"), 1.2), 1.0, 2.0)
                center = float(self.bounds[name]["baseline"])
                self.bounds[name]["low"] = min(self.bounds[name]["low"],
                                               center * low_factor)
                self.bounds[name]["high"] = max(self.bounds[name]["high"],
                                                center * high_factor)
                applied[name] = [round(low_factor, 2), round(high_factor, 2)]
            if applied:
                self.logger.status(f"LLM 参数边界建议（参考，不强制）: {applied}")
        except Exception as exc:
            self.logger.warn(f"LLM 参数边界建议不可用（忽略，不影响 GA）: {exc}")

    # ------------------------------------------------------------------ 评估
    def _genome_key(self, genome: dict[str, float]) -> tuple:
        return tuple(round(float(genome.get(name, 0.0)), 4) for name in GENE_NAMES)

    def _aoa_list(self) -> list[float]:
        npts = max(3, self.ga.aoa_npts)
        start, end = -2.0, 6.0
        return [round(start + index * (end - start) / (npts - 1), 3)
                for index in range(npts)]

    def _geometry_for_genome(self, genome: dict[str, float]) -> dict[str, Any]:
        return size_aircraft(self.spec, genome_to_optimization(genome))

    def _evaluate_genome(self, genome: dict[str, float], index: int) -> dict[str, Any]:
        """真实评估一个个体（子类/测试可注入 evaluator 替换）。"""
        if self._evaluator is not None:
            result = self._evaluator(genome, index, self.eval_dir)
        else:
            result = self._evaluate_real(genome, index)
        scored = self._score(dict(result))
        return scored

    def _score(self, result: dict[str, Any]) -> dict[str, Any]:
        """计算目标值、约束惩罚与适应度。

        静稳定裕度使用"可达到值"：GA 个体只评估气动，重心在最终设计阶段由
        ``Xcg = Xnp − SM_target·MAC`` 解析确定；因此先按设计流程的重心允许范围
        求解可达 SM，再对超出目标容差的部分惩罚，避免对所有个体施加同一偏差惩罚。
        """
        result.setdefault("genome", {})
        if not result.get("ok"):
            result["objective_value"] = -1.0e6
            result["penalty"] = 1.0e6
            result["fitness"] = -2.0e6
            return result
        objective = self.ga.objective
        if objective == "max_ld":
            value = _num(result.get("cruise_ld"), 0.0)
        elif objective == "max_cl":
            value = _num(result.get("clmax"), 0.0)
        else:  # min_cd
            value = -1000.0 * _num(result.get("cd_cruise"), 1.0)
        span = _num(result.get("span_m"), 0.0)
        span_excess = 0.0
        if self.span_limit < float("inf") and span > 0:
            span_excess = max(0.0, span / self.span_limit - 1.0)
        cl_cruise = _num(result.get("cl_cruise"), 0.0)
        stall_excess = max(0.0, cl_cruise / self._clmax_effective - 0.95)
        sm_achievable = self._achievable_static_margin(result)
        result["sm_achievable"] = (round(sm_achievable, 4)
                                   if sm_achievable is not None else None)
        device = result.get("sm")
        deviation_text = None
        if sm_achievable is None:
            sm_excess = 0.02  # 未取到 SM 的轻微惩罚
            deviation = None
        else:
            deviation = abs(sm_achievable - float(self.spec.static_margin))
            sm_excess = max(0.0, deviation - self.ga.sm_tolerance)
        penalty = 80.0 * span_excess + 600.0 * stall_excess + 400.0 * sm_excess
        result["objective_value"] = round(value, 5)
        result["penalty"] = round(penalty, 5)
        result["fitness"] = round(value - penalty, 5)
        result["constraints"] = {
            "span_excess": round(span_excess, 5),
            "stall_excess": round(stall_excess, 5),
            "sm_excess": round(sm_excess, 5),
            "sm_achievable": (round(sm_achievable, 4)
                              if sm_achievable is not None else None),
            "sm_polar_raw": (round(float(device), 4) if device is not None else None),
            "sm_deviation": (round(deviation, 4) if deviation is not None else None),
        }
        return result

    def _achievable_static_margin(self, result: dict[str, Any]) -> float | None:
        """按最终设计流程（移动重心到目标 SM）计算可达静稳定裕度。"""
        sm_raw = result.get("sm")
        if sm_raw is None:
            return None
        try:
            sm_raw = float(sm_raw)
        except (TypeError, ValueError):
            return None
        if math.isnan(sm_raw) or math.isinf(sm_raw):
            return None
        cref = _num(result.get("mac_m"), 0.0)
        xcg_initial = _num(result.get("xcg_initial_m"), 0.0)
        if cref <= 0:
            return None
        x_np = xcg_initial + sm_raw * cref
        x_wing_le = _num(result.get("x_wing_le_m"), xcg_initial)
        root_chord = _num(result.get("root_chord_m"), cref)
        fuselage_length = _num(result.get("fuselage_length_m"), 99.0)
        low = max(x_wing_le + 0.05 * root_chord, 0.02 * fuselage_length)
        high = min(x_wing_le + 0.60 * root_chord, 0.95 * fuselage_length)
        target = float(self.spec.static_margin)
        xcg_design = _clamp(x_np - target * cref, low, max(low, high))
        result["x_np_m"] = round(x_np, 4)
        result["xcg_design_m"] = round(xcg_design, 4)
        return (x_np - xcg_design) / max(cref, 1e-9)

    def _evaluate_real(self, genome: dict[str, float], index: int) -> dict[str, Any]:
        started = time.time()
        try:
            optimization = genome_to_optimization(genome)
            geometry = size_aircraft(self.spec, optimization)
            wing = geometry.get("wing") or {}
            sizing = geometry.get("sizing") or {}
            area = float(sizing.get("area_m2") or 0.0)
            span = float(wing.get("span_m") or 0.0)
            mac = float(sizing.get("mac_m") or wing.get("root_chord_m") or 0.0)
            ref = {
                "sref_m2": area,
                "cref_m": mac,
                "bref_m": span,
                "xcg_m": float(sizing.get("xcg_initial_m") or 0.0),
            }
            flow = {
                "velocity_ms": float(self.operating_conditions["speed_cruise_ms"]),
                "altitude_m": float(self.operating_conditions["altitude_m"]),
                "density_kgm3": float(self.operating_conditions["density_kgm3"]),
                "sound_speed_ms": float(self.operating_conditions["sound_speed_ms"]),
                "mach": float(self.operating_conditions["mach"]),
            }
            model_path = self.eval_dir / f"eval_{index:03d}.vsp3"
            self.bridge.build_geometry(geometry)
            self.bridge.save_vsp3(model_path)
            analysis = self.bridge.run_vspaero(
                model_path, self.eval_dir, ref, flow,
                aoa_list=self._aoa_list(), betas=[0.0], stability=False,
                wake_iters=self.ga.wake_iters, n_cpu=self.ga.n_cpu)
            rows = [row for row in (analysis.get("polar") or {}).get("rows", [])
                    if abs(_num(row.get("Beta"), 0.0)) <= 1e-6]
            fitted = _fit_polar_rows(rows)
            if not fitted.get("ok"):
                raise VSPError(str(fitted.get("error") or "极曲线拟合失败"))
            weight = self.spec.mtow_kg * GRAVITY
            velocity = float(self.operating_conditions["speed_cruise_ms"])
            rho = float(self.operating_conditions["density_kgm3"])
            cl_cruise = weight / max(0.5 * rho * velocity ** 2 * area, 1e-9)
            cd_cruise = float(fitted["cd_at"](cl_cruise))
            cd_cruise = max(cd_cruise, 1e-4)
            cruise_ld = cl_cruise / cd_cruise
            summary = summarize_polar({"rows": rows})
            sm = summary.get("sm_polar")
            if sm is not None and (math.isnan(float(sm)) or math.isinf(float(sm))):
                sm = None
            result = {
                "ok": True,
                "index": index,
                "genome": {name: round(float(value), 4)
                           for name, value in genome.items()},
                "model_path": str(model_path),
                "polar_path": analysis.get("polar_path"),
                "area_m2": round(area, 4),
                "aspect_ratio": round(float(sizing.get("aspect_ratio") or 0.0), 3),
                "span_m": round(span, 4),
                "mac_m": round(mac, 4),
                "xcg_initial_m": round(float(ref["xcg_m"]), 4),
                "x_wing_le_m": round(float(wing.get("x_le_m") or 0.0), 4),
                "root_chord_m": round(float(wing.get("root_chord_m") or mac), 4),
                "fuselage_length_m": round(
                    float((geometry.get("fuselage") or {}).get("length_m") or 0.0), 4),
                "taper_ratio": round(float(optimization["taper_ratio"]), 3),
                "sweep_deg": round(float(optimization["sweep_deg"]), 2),
                "dihedral_deg": round(float(optimization["dihedral_deg"]), 2),
                "tail_scale": round(float(optimization["tail_scale"]), 3),
                "cl_cruise": round(cl_cruise, 4),
                "cd_cruise": round(cd_cruise, 5),
                "cruise_ld": round(cruise_ld, 3),
                "clmax": round(_num(fitted.get("cl_max_polar"), 0.0), 4),
                "alpha_at_clmax_deg": round(
                    _num(fitted.get("alpha_at_cl_max_deg"), 0.0), 2),
                "cl_alpha_per_rad": round(
                    _num(fitted.get("cl_alpha_per_rad"), 0.0), 3),
                "sm": (round(float(sm), 4) if sm is not None else None),
                "aoa_list": self._aoa_list(),
                "elapsed_s": round(time.time() - started, 2),
            }
            self._cleanup_eval_files(model_path)
            return result
        except Exception as exc:
            return {
                "ok": False,
                "index": index,
                "genome": {name: round(float(value), 4)
                           for name, value in genome.items()},
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.time() - started, 2),
            }

    def _cleanup_eval_files(self, model_path: Path) -> None:
        for suffix in _EVAL_CLEANUP_SUFFIXES:
            try:
                (model_path.parent / (model_path.stem + suffix)).unlink(missing_ok=True)
            except OSError:
                pass

    def _evaluate_and_record(self, genome: dict[str, float]) -> dict[str, Any] | None:
        key = self._genome_key(genome)
        if key in self._cache:
            return self._cache[key]
        if self._budget_exhausted():
            return None
        result = self._evaluate_genome(genome, len(self.evaluations))
        self.evaluations.append(result)
        self._cache[key] = result
        status = "ok" if result.get("ok") else f"failed: {result.get('error')}"
        try:
            ld_text = f"{float(result.get('cruise_ld', float('nan'))):.2f}"
        except (TypeError, ValueError):
            ld_text = "n/a"
        self.logger.log(
            "tool",
            f"GA 评估 #{result.get('index')}: L/D={ld_text} "
            f"S={result.get('area_m2', '?')} AR={result.get('aspect_ratio', '?')} "
            f"({result.get('elapsed_s', '?')}s, {status})")
        return result

    def _budget_exhausted(self) -> bool:
        if len(self.evaluations) >= self.ga.max_evaluations:
            return True
        return (time.time() - self._started) >= self.ga.time_budget_s

    # ------------------------------------------------------------------ 遗传操作
    def _random_genome(self) -> dict[str, float]:
        return {name: self._rng.uniform(self.bounds[name]["low"],
                                        self.bounds[name]["high"])
                for name in GENE_NAMES}

    def _initial_population(self) -> list[dict[str, float]]:
        population = [dict(self.baseline_genome)]
        while len(population) < self.ga.population:
            population.append(self._random_genome())
        return population

    def _tournament(self, population: list[dict[str, float]]) -> dict[str, float]:
        size = min(self.ga.tournament_size, len(population))
        contenders = self._rng.sample(population, size)
        scored = [self._cache.get(self._genome_key(genome)) for genome in contenders]
        pairs = [(genome, scored_item or {}) for genome, scored_item in
                 zip(contenders, scored)]
        pairs.sort(key=lambda item: _num(item[1].get("fitness"), -1e18),
                   reverse=True)
        return dict(pairs[0][0])

    def _crossover(self, parent_a: dict[str, float],
                   parent_b: dict[str, float]) -> dict[str, float]:
        if self._rng.random() > self.ga.crossover_rate:
            return dict(parent_a)
        child: dict[str, float] = {}
        for name in GENE_NAMES:
            if self._rng.random() < 0.5:
                child[name] = float(parent_a[name])
            else:
                alpha = self._rng.uniform(0.25, 0.75)
                child[name] = alpha * float(parent_a[name]) + \
                    (1.0 - alpha) * float(parent_b[name])
        return child

    def _mutate(self, genome: dict[str, float]) -> dict[str, float]:
        mutated = dict(genome)
        for name in GENE_NAMES:
            if self._rng.random() >= self.ga.mutation_rate:
                continue
            item = self.bounds[name]
            sigma = 0.20 * (item["high"] - item["low"])
            value = float(mutated[name]) + self._rng.gauss(0.0, sigma)
            mutated[name] = _clamp(value, item["low"], item["high"])
        return mutated

    def _next_generation(self, population: list[dict[str, float]]
                         ) -> list[dict[str, float]]:
        ranked: list[tuple[float, dict[str, float]]] = []
        for genome in population:
            scored = self._cache.get(self._genome_key(genome))
            if scored is None:
                continue
            ranked.append((_num(scored.get("fitness"), -1e18), genome))
        ranked.sort(key=lambda item: item[0], reverse=True)
        elites = [dict(genome) for _, genome in ranked[:self.ga.elite_count]]
        next_population = [dict(genome) for genome in elites]
        while len(next_population) < self.ga.population:
            slots_left = self.ga.population - len(next_population)
            # 随机移民：保持种群多样性，避免过早收敛到基线附近
            if (slots_left <= 1
                    and self._rng.random() < self.ga.immigrant_fraction):
                next_population.append(self._random_genome())
                continue
            parent_a = self._tournament(population)
            parent_b = self._tournament(population)
            child = self._crossover(parent_a, parent_b)
            child = self._mutate(child)
            next_population.append(child)
        return next_population

    # ------------------------------------------------------------------ 主循环
    def optimize(self) -> dict[str, Any]:
        """运行 GA 主循环（只做数值优化，不生成报告）。"""
        started = time.time()
        self.logger.plan(
            f"遗传算法优化: 种群 {self.ga.population} × 代数 {self.ga.generations}，"
            f"目标 {OBJECTIVE_LABELS[self.ga.objective]}，"
            f"评估上限 {self.ga.max_evaluations} 次 / {self.ga.time_budget_s:.0f} s")
        self.logger.status(
            f"GA 工况: 海拔 {self.operating_conditions['altitude_m']:.0f} m, "
            f"巡航 {self.operating_conditions['speed_cruise_ms']:.1f} m/s, "
            f"ρ={self.operating_conditions['density_kgm3']:.4f} kg/m³, "
            f"Ma={self.operating_conditions['mach']:.3f}")
        bounds_text = ", ".join(
            f"{name}:[{self.bounds[name]['low']:.3g},{self.bounds[name]['high']:.3g}]"
            for name in GENE_NAMES)
        self.logger.status(f"GA 变量边界: {bounds_text}")
        baseline_eval = self._evaluate_and_record(dict(self.baseline_genome))
        population = self._initial_population()
        for generation in range(1, self.ga.generations + 1):
            generation_started = time.time()
            for genome in population:
                self._evaluate_and_record(genome)
            valid = [self._cache[self._genome_key(genome)] for genome in population
                     if self._genome_key(genome) in self._cache]
            valid = [item for item in valid if item.get("ok")]
            if valid:
                best = max(valid, key=lambda item: _num(item.get("fitness"), -1e18))
                average = sum(_num(item.get("cruise_ld"), 0.0)
                              for item in valid) / len(valid)
                worst = min(_num(item.get("cruise_ld"), 0.0) for item in valid)
                best_ld = _num(best.get("cruise_ld"), 0.0)
                self.history.append({
                    "gen": generation,
                    "best_fitness": round(_num(best.get("fitness"), 0.0), 4),
                    "best_ld": round(best_ld, 3),
                    "avg_ld": round(average, 3),
                    "worst_ld": round(worst, 3),
                    "evaluations": len(self.evaluations),
                    "elapsed_s": round(time.time() - self._started, 1),
                    "best_genome": dict(best.get("genome") or {}),
                })
                self.logger.log(
                    "ga", f"gen={generation} best={best_ld:.2f} avg={average:.2f} "
                          f"worst={worst:.2f} evals={len(self.evaluations)} "
                          f"elapsed={time.time() - self._started:.1f}s "
                          f"best_gene={best.get('genome')}")
            else:
                self.logger.warn(f"[ga] gen={generation} 没有有效评估结果")
                self.history.append({"gen": generation, "best_fitness": None,
                                     "best_ld": None, "avg_ld": None,
                                     "worst_ld": None,
                                     "evaluations": len(self.evaluations),
                                     "elapsed_s": round(time.time() - self._started, 1)})
            self.logger.log(
                "status",
                f"第 {generation} 代完成，本代耗时 {time.time() - generation_started:.1f}s，"
                f"累计评估 {len(self.evaluations)} 次")
            if self._budget_exhausted():
                self.logger.warn("达到评估次数或时间上限，GA 提前结束")
                break
            if generation < self.ga.generations:
                population = self._next_generation(population)
        valid_evaluations = [item for item in self.evaluations if item.get("ok")]
        if not valid_evaluations:
            raise VSPError("GA 没有产生有效评估结果（请检查 OpenVSP/VSPAERO 环境）")
        best_eval = max(valid_evaluations,
                        key=lambda item: _num(item.get("fitness"), -1e18))
        baseline = self._summary(baseline_eval)
        best = self._summary(best_eval)
        result = {
            "ok": True,
            "objective": self.ga.objective,
            "objective_label": OBJECTIVE_LABELS.get(self.ga.objective),
            "baseline": baseline,
            "best": best,
            "history": self.history,
            "evaluations": [self._compact_evaluation(item)
                            for item in self.evaluations],
            "evaluation_count": len(self.evaluations),
            "generations_run": len(self.history),
            "operating_conditions": dict(self.operating_conditions),
            "bounds": {name: {key: round(float(value), 4)
                              for key, value in self.bounds[name].items()}
                       for name in GENE_NAMES},
            "ga_config": self.ga.to_dict(),
            "seed_model": self.seed_model,
            "case_model": self.case_model,
            "based_on_case": self.based_on_case,
            "case_parameters": (summarize_case_parameters(self.case_parameters)
                                if self.case_parameters else None),
            "baseline_source": self.baseline_source,
            "elapsed_s": round(time.time() - started, 1),
        }
        improvement = 0.0
        if baseline.get("cruise_ld"):
            improvement = ((_num(best.get("cruise_ld"), 0.0)
                            - _num(baseline.get("cruise_ld"), 0.0))
                           / abs(_num(baseline.get("cruise_ld"), 1.0)))
        result["improvement_ratio"] = round(improvement, 4)
        self.logger.status(
            f"GA 收敛: 基线 L/D={baseline.get('cruise_ld')} → "
            f"最优 L/D={best.get('cruise_ld')} "
            f"({improvement * 100:+.1f}%)，评估 {len(self.evaluations)} 次，"
            f"耗时 {result['elapsed_s']}s")
        return result

    def _summary(self, evaluation: dict[str, Any] | None) -> dict[str, Any]:
        if not evaluation:
            return {}
        genome = evaluation.get("genome") or {}
        summary = {
            "genome": dict(genome),
            "area_m2": evaluation.get("area_m2"),
            "aspect_ratio": evaluation.get("aspect_ratio"),
            "taper_ratio": evaluation.get("taper_ratio", genome.get("taper_ratio")),
            "sweep_deg": evaluation.get("sweep_deg", genome.get("sweep_deg")),
            "dihedral_deg": evaluation.get("dihedral_deg",
                                           genome.get("dihedral_deg")),
            "tail_scale": evaluation.get("tail_scale", genome.get("tail_scale")),
            "span_m": evaluation.get("span_m"),
            "cruise_ld": evaluation.get("cruise_ld"),
            "ld_cruise": evaluation.get("cruise_ld"),
            "cl_cruise": evaluation.get("cl_cruise"),
            "cd_cruise": evaluation.get("cd_cruise"),
            "clmax": evaluation.get("clmax"),
            "sm": evaluation.get("sm"),
            "sm_achievable": evaluation.get("sm_achievable"),
            "x_np_m": evaluation.get("x_np_m"),
            "xcg_design_m": evaluation.get("xcg_design_m"),
            "fitness": evaluation.get("fitness"),
            "penalty": evaluation.get("penalty"),
            "model_path": evaluation.get("model_path"),
            "elapsed_s": evaluation.get("elapsed_s"),
        }
        return summary

    def _compact_evaluation(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        keys = ("index", "ok", "error", "genome", "area_m2", "aspect_ratio",
                "span_m", "cl_cruise", "cd_cruise", "cruise_ld", "clmax", "sm",
                "sm_achievable", "x_np_m", "xcg_design_m",
                "fitness", "penalty", "objective_value", "constraints",
                "elapsed_s", "model_path", "polar_path")
        return {key: evaluation.get(key) for key in keys
                if key in evaluation}

    # ------------------------------------------------------------------ 完整任务
    def run(self) -> dict[str, Any]:
        """GA 完整任务：优化 → 最优基因重建 → 完整分析 → 报告。"""
        task_started = time.time()
        self._started = task_started
        ga_result = self.optimize()
        best_genome = dict(ga_result["best"].get("genome") or self.baseline_genome)
        optimization = genome_to_optimization(best_genome)
        self.logger.plan("用 GA 最优基因重建 OpenVSP 模型并做完整气动/稳定性分析")
        designer = self.designer or Designer(self.config, self.logger)
        designer.spec = self.spec
        try:
            designer.bridge = self.bridge
        except Exception:
            pass
        designer.build_aircraft(optimization=optimization,
                                model_name="ga_optimized")
        designer.run_analysis()
        designer.finalize()
        metrics = designer.metrics or {}
        final_aero = metrics.get("aero") or {}
        final_stability = metrics.get("stability") or {}
        baseline = dict(ga_result["baseline"])
        best = dict(ga_result["best"])
        best.update({
            "final_cl_max": final_aero.get("cl_max"),
            "final_ld_max": final_aero.get("ld_max"),
            "final_static_margin": final_stability.get("static_margin"),
            "final_xcg_m": final_stability.get("xcg_m"),
            "final_x_np_m": final_stability.get("neutral_point_m"),
        })
        ga_meta = {
            "history": ga_result["history"],
            "bounds": ga_result["bounds"],
            "best_genome": best_genome,
            "baseline_genome": self.baseline_genome,
            "population": self.ga.population,
            "generations": self.ga.generations,
            "generations_run": ga_result["generations_run"],
            "evaluation_count": ga_result["evaluation_count"],
            "time_budget_s": self.ga.time_budget_s,
            "elapsed_s": ga_result["elapsed_s"],
            "seed_model": self.seed_model,
            "seed_parameters_used": bool(self.seed_parameters),
            "case_model": self.case_model,
            "based_on_case": self.based_on_case,
            "case_parameters": ga_result.get("case_parameters"),
            "baseline_source": self.baseline_source,
            "objective": self.ga.objective,
            "objective_label": ga_result.get("objective_label"),
            "operating_conditions": ga_result["operating_conditions"],
            "ga_config": ga_result["ga_config"],
        }
        metrics["optimization"] = {
            "ok": True,
            "method": "遗传算法 (GA)，真实 OpenVSP + VSPAERO 评估",
            "objective": self.ga.objective,
            "baseline": baseline,
            "best": best,
            "improvement_ratio": ga_result.get("improvement_ratio"),
            "rebuild": False,
            "ga": ga_meta,
        }
        metrics["ga"] = ga_meta
        report_path = ""
        try:
            from .report import generate_report

            report_path = generate_report(
                output_dir=self.output_dir,
                template=self.config.template,
                title="遗传算法优化设计报告",
                spec=self.spec,
                metrics=metrics,
                geometry=designer.geometry,
                logger=self.logger,
                bridge=self.bridge,
            )
            self.logger.status(f"GA 优化报告已生成: {report_path}")
        except Exception as exc:
            self.logger.error(f"GA 报告生成失败: {exc}")
        model_path = str(Path(designer.model_path).resolve()) \
            if designer.model_path else ""
        elapsed = round(time.time() - task_started, 1)
        self.logger.status(
            f"GA 任务完成: 最优模型 {model_path}，耗时 {elapsed}s，"
            f"基线 L/D={baseline.get('cruise_ld')} → 最优 L/D={best.get('cruise_ld')}")
        return {
            "ok": bool(model_path or report_path),
            "task": "ga",
            "mode": "offline",
            "model_path": model_path,
            "report_path": report_path,
            "baseline": baseline,
            "best": best,
            "history": ga_result["history"],
            "evaluations": ga_result["evaluations"],
            "evaluation_count": ga_result["evaluation_count"],
            "generations_run": ga_result["generations_run"],
            "operating_conditions": ga_result["operating_conditions"],
            "bounds": ga_result["bounds"],
            "ga_config": ga_result["ga_config"],
            "seed_model": self.seed_model,
            "case_model": self.case_model,
            "based_on_case": self.based_on_case,
            "case_parameters": ga_result.get("case_parameters"),
            "baseline_source": self.baseline_source,
            "improvement_ratio": ga_result.get("improvement_ratio"),
            "metrics": metrics,
            "exports": designer.exports,
            "elapsed_s": elapsed,
        }

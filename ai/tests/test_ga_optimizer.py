"""ga_optimizer.py 单元测试：配置、边界、缓存、精英、惩罚与 mock 收敛。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from ai_design.config import load_config
from ai_design.designer import parse_requirements
from ai_design.ga_optimizer import (
    GENE_NAMES,
    GAConfig,
    GAOptimizer,
    build_operating_conditions,
    genome_to_optimization,
    make_genome_bounds,
)

AI_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = AI_ROOT / "templates" / "design_report_template.docx"
PROMPT = ("设计一架 50kg 级电动无人机：巡航高度 2000 m，速度范围 25-45 m/s，"
          "常规尾翼布局，翼展不超过 4.2 m，翼型 NACA 4412，"
          "纵向静稳定裕度 10%。")


def make_config(tmp_path: Path, **overrides) -> object:
    raw = {
        "provider": "deepseek",
        "prompt": PROMPT,
        "output_dir": str(tmp_path / "out"),
        "template": str(TEMPLATE),
        "mode": "offline",
        "task": "ga",
        "ga": {"population": 6, "generations": 3, "time_budget_s": 60,
               "objective": "max_ld", "max_evaluations": 30},
    }
    raw.update(overrides)
    path = tmp_path / "ga_job.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return load_config(path, offline=True)


def fake_evaluator(calls: list[dict] | None = None, *, fail: bool = False):
    """确定性 mock 评估器：最优解在翼展上限附近的大 AR。"""
    def evaluator(genome: dict, index: int, eval_dir: Path) -> dict:
        if calls is not None:
            calls.append(dict(genome))
        if fail:
            return {"ok": False, "index": index, "error": "mock failure",
                    "genome": dict(genome)}
        area = float(genome["wing_area_m2"])
        ar_raw = float(genome["aspect_ratio"])
        span = min(math.sqrt(area * ar_raw), 4.2)
        ar = span ** 2 / area
        ld = (26.0 + 1.2 * (ar - 9.0) + 2.5 * (area - 1.72)
              - 2.0 * (genome["taper_ratio"] - 0.6) ** 2)
        ld -= 0.8 * min(0.0, 4.2 - span) if span >= 4.2 else 0.0
        return {
            "ok": True,
            "index": index,
            "genome": dict(genome),
            "area_m2": round(area, 4),
            "aspect_ratio": round(ar, 3),
            "span_m": round(span, 4),
            "mac_m": 0.45,
            "xcg_initial_m": 0.87,
            "x_wing_le_m": 0.76,
            "root_chord_m": 0.55,
            "fuselage_length_m": 2.72,
            "cl_cruise": 0.52,
            "cd_cruise": 0.52 / ld,
            "cruise_ld": round(ld, 3),
            "clmax": 1.3,
            "sm": 0.10,
            "elapsed_s": 0.01,
        }
    return evaluator


# --------------------------------------------------------------------------- #
# 配置 / 边界 / 映射
# --------------------------------------------------------------------------- #
def test_ga_config_defaults_and_clamps():
    default = GAConfig.from_dict({})
    assert default.population == 8
    assert default.generations == 4
    assert default.time_budget_s == 300
    assert default.objective == "max_ld"
    assert default.max_evaluations == 40

    clamped = GAConfig.from_dict({
        "population": 999,
        "generations": 0,
        "time_budget_s": 5,
        "max_evaluations": 999,
        "objective": "not_an_objective",
        "aoa_npts": 1,
    })
    assert clamped.population == 24
    assert clamped.generations == 1
    assert clamped.time_budget_s == 30.0
    assert clamped.max_evaluations == 40
    assert clamped.objective == "max_ld"
    assert clamped.aoa_npts == 3


def test_make_genome_bounds_with_span_limit(tmp_path):
    config = make_config(tmp_path)
    spec = parse_requirements(PROMPT)
    baseline = {"wing_area_m2": 1.72, "aspect_ratio": 9.0,
                "taper_ratio": 0.6, "sweep_deg": 0.0,
                "dihedral_deg": 2.0, "tail_scale": 1.0}
    bounds = make_genome_bounds(baseline, spec)
    assert set(bounds) == set(GENE_NAMES)
    for name in GENE_NAMES:
        assert bounds[name]["low"] <= baseline[name] <= bounds[name]["high"]
    # 翼展上限 4.2 m 约束 AR 上限（S 下限 1.376 m²）
    assert bounds["aspect_ratio"]["high"] <= (4.2 ** 2 / bounds["wing_area_m2"]["low"]) + 1e-6
    assert bounds["aspect_ratio"]["high"] < 16.0


def test_genome_to_optimization():
    genome = {"wing_area_m2": 1.8, "aspect_ratio": 10.5, "taper_ratio": 0.55,
              "sweep_deg": 3.0, "dihedral_deg": 2.5, "tail_scale": 1.2}
    optimization = genome_to_optimization(genome)
    assert optimization == {"wing_area_m2": 1.8, "aspect_ratio": 10.5,
                            "taper_ratio": 0.55, "sweep_deg": 3.0,
                            "dihedral_deg": 2.5, "tail_scale": 1.2}


def test_operating_conditions_override():
    spec = parse_requirements(PROMPT)
    default = build_operating_conditions(spec)
    assert default["altitude_m"] == 2000.0
    assert default["source"] == "design_spec"
    override = build_operating_conditions(
        spec, {"altitude_m": 3000, "speed_cruise_ms": 45.0})
    assert override["altitude_m"] == 3000.0
    assert override["speed_cruise_ms"] == 45.0
    assert override["source"] == "operating_conditions"
    assert override["density_kgm3"] < default["density_kgm3"]


# --------------------------------------------------------------------------- #
# 评估 / 惩罚
# --------------------------------------------------------------------------- #
def test_score_penalty_signals(tmp_path):
    config = make_config(tmp_path)
    spec = parse_requirements(PROMPT)
    optimizer = GAOptimizer(config, spec, bridge=None,
                            logger=_logger(tmp_path), output_dir=tmp_path / "out",
                            evaluator=fake_evaluator())
    good = {
        "ok": True, "cruise_ld": 26.0, "cl_cruise": 0.5, "clmax": 1.3,
        "span_m": 4.0, "sm": 0.10, "mac_m": 0.45, "xcg_initial_m": 0.87,
        "x_wing_le_m": 0.76, "root_chord_m": 0.55, "fuselage_length_m": 2.72,
    }
    scored = optimizer._score(dict(good))
    assert scored["penalty"] == pytest.approx(0.0, abs=1e-6)
    assert scored["fitness"] == pytest.approx(26.0)
    assert scored["constraints"]["sm_achievable"] == pytest.approx(0.10, abs=1e-3)

    # 翼展超限 + 巡航 CL 过高 + 可达 SM 偏离目标 → 惩罚 > 0
    bad = dict(good, span_m=5.5, cl_cruise=1.35, sm=-0.10,
               xcg_initial_m=0.95, x_wing_le_m=1.2)
    bad_scored = optimizer._score(bad)
    assert bad_scored["penalty"] > 0.0
    assert bad_scored["fitness"] < bad_scored["objective_value"]
    assert bad_scored["constraints"]["span_excess"] > 0
    assert bad_scored["constraints"]["stall_excess"] > 0


# --------------------------------------------------------------------------- #
# GA 主循环（mock evaluator）
# --------------------------------------------------------------------------- #
def test_optimize_with_mock_evaluator(tmp_path):
    config = make_config(tmp_path)
    spec = parse_requirements(PROMPT)
    calls: list[dict] = []
    optimizer = GAOptimizer(config, spec, bridge=None,
                            logger=_logger(tmp_path),
                            output_dir=tmp_path / "out",
                            evaluator=fake_evaluator(calls))
    result = optimizer.optimize()
    assert result["ok"] is True
    assert len(result["history"]) == 3
    assert result["generations_run"] == 3
    assert result["evaluation_count"] <= 30
    assert result["evaluation_count"] >= 4
    # 缓存去重：实际评估次数 == evaluator 调用次数
    assert len(calls) == result["evaluation_count"]
    # 精英保留：最优适应度逐代不下降
    best_fitness = [item["best_fitness"] for item in result["history"]]
    assert all(best_fitness[i + 1] >= best_fitness[i] - 1e-6
               for i in range(len(best_fitness) - 1))
    # 收敛：最优不差于基线
    baseline_ld = result["baseline"]["cruise_ld"]
    best_ld = result["best"]["cruise_ld"]
    assert best_ld >= baseline_ld - 1e-6
    assert best_ld > baseline_ld
    assert result["improvement_ratio"] > 0
    # 基因在边界内
    bounds = result["bounds"]
    for name, value in result["best"]["genome"].items():
        assert bounds[name]["low"] - 1e-6 <= value <= bounds[name]["high"] + 1e-6
    # 每代 [ga] 日志
    text = "\n".join(_logger_tail(optimizer))
    assert "[ga] gen=1" in text
    assert "[ga] gen=3" in text
    # 结果字段
    assert result["operating_conditions"]["altitude_m"] == 2000.0
    assert result["ga_config"]["population"] == 6
    assert len(result["evaluations"]) == result["evaluation_count"]


def test_budget_limits_evaluations(tmp_path):
    config = make_config(tmp_path, ga={"population": 8, "generations": 4,
                                       "time_budget_s": 60,
                                       "max_evaluations": 2})
    spec = parse_requirements(PROMPT)
    optimizer = GAOptimizer(config, spec, bridge=None,
                            logger=_logger(tmp_path),
                            output_dir=tmp_path / "out",
                            evaluator=fake_evaluator())
    result = optimizer.optimize()
    assert result["evaluation_count"] <= 2
    assert result["generations_run"] <= 1


def test_all_evaluations_fail_raises(tmp_path):
    from ai_design.vsp_bridge import VSPError

    config = make_config(tmp_path)
    spec = parse_requirements(PROMPT)
    optimizer = GAOptimizer(config, spec, bridge=None,
                            logger=_logger(tmp_path),
                            output_dir=tmp_path / "out",
                            evaluator=fake_evaluator(fail=True))
    with pytest.raises(VSPError):
        optimizer.optimize()


def test_missing_seed_model_warns(tmp_path):
    config = make_config(tmp_path, seed_model=str(tmp_path / "missing.vsp3"))
    assert any("seed_model" in warning for warning in config.warnings)
    spec = parse_requirements(PROMPT)
    optimizer = GAOptimizer(config, spec, bridge=None,
                            logger=_logger(tmp_path),
                            output_dir=tmp_path / "out",
                            evaluator=fake_evaluator())
    assert optimizer.seed_model is None
    assert optimizer.baseline_genome["aspect_ratio"] > 0


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _logger(tmp_path: Path):
    from ai_design.logging_util import Logger

    return Logger(log_file=tmp_path / "ga.log", echo=False)


def _logger_tail(optimizer: GAOptimizer) -> list[str]:
    return optimizer.logger.tail(500)

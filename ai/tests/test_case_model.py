"""case_model（上传 .vsp3 作为设计/优化基线）单元测试。

覆盖：config 校验、真实案例 .vsp3 参数读取、案例参数并入 spec（prompt 修改优先）、
Designer 以案例为基线 sizing、GA 基线优先级（case_model > seed_model）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from ai_design.config import load_config
from ai_design.designer import (
    Designer,
    apply_case_baseline,
    parse_requirements,
    size_aircraft,
)
from ai_design.ga_optimizer import GAOptimizer
from ai_design.logging_util import Logger
from ai_design.vsp_bridge import VSPBridge, summarize_case_parameters

AI_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = AI_ROOT / "examples"
TEMPLATE = AI_ROOT / "templates" / "design_report_template.docx"
CASE_PATH = EXAMPLES_DIR / "case_demo" / "baseline_case.vsp3"

#: 案例（examples/case_demo/baseline_case.vsp3，来自 GA 优化模型）关键参数
CASE_WING = {
    "span_m": 4.2,
    "area_m2": 1.9509,
    "aspect_ratio": 9.042,
    "taper_ratio": 0.7719,
    "sweep_deg": 4.515,
    "dihedral_deg": 4.611,
}
CASE_FUSELAGE = {"length_m": 2.86, "max_diameter_m": 0.2287}

PROMPT = ("在现有案例基础上修改设计：将展弦比调整为 11.0，后掠角改为 6 度，"
          "机身加长 10%，其余参数保持与案例一致，静稳定裕度仍取 10%。")


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _write_config(tmp_path: Path, **overrides) -> Path:
    raw = {
        "provider": "deepseek",
        "prompt": PROMPT,
        "output_dir": str(tmp_path / "out"),
        "template": str(TEMPLATE),
        "mode": "offline",
        "case_model": str(CASE_PATH),
        "design": {"aoa_start": -4.0, "aoa_end": 10.0, "aoa_npts": 8,
                   "wake_iters": 3, "n_cpu": 4, "stability": True},
    }
    raw.update(overrides)
    path = tmp_path / "job.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _logger(tmp_path: Path) -> Logger:
    return Logger(log_file=tmp_path / "case.log", echo=False)


def _fake_case_parameters(path_label: str = "case.vsp3") -> dict:
    """构造与真实案例一致的 read_model_parameters 返回结构。"""
    wings = [
        {"name": "MainWing", **CASE_WING, "symmetric": True},
        {"name": "HTail", "span_m": 1.0, "area_m2": 0.19, "symmetric": True},
        {"name": "VTail", "span_m": 0.6, "area_m2": 0.10, "symmetric": False},
    ]
    return {
        "ok": True,
        "path": str(EXAMPLES_DIR / "case_demo" / path_label),
        "component_count": 3,
        "wing": dict(wings[0]),
        "fuselage": dict(CASE_FUSELAGE),
        "wings": wings,
        "error": None,
    }


class FakeBridge:
    """只实现 GA/Designer 用到的 read_model_parameters。"""

    def __init__(self, case_params: dict | None, seed_params: dict | None = None):
        self.case_params = case_params
        self.seed_params = seed_params
        self.calls: list[str] = []

    def read_model_parameters(self, path):
        label = str(path)
        self.calls.append(label)
        if "case" in Path(label).name.lower():
            params = self.case_params
        else:
            params = self.seed_params
        if not params:
            return {"ok": False, "error": "no model"}
        result = dict(params)
        result["path"] = label
        return result


def _fake_evaluator(genome: dict, index: int, eval_dir: Path) -> dict:
    area = float(genome["wing_area_m2"])
    ar = float(genome["aspect_ratio"])
    span = min(math.sqrt(area * ar), 4.83)
    return {
        "ok": True, "index": index, "genome": dict(genome),
        "area_m2": area, "aspect_ratio": ar, "span_m": span,
        "mac_m": 0.45, "xcg_initial_m": 0.87,
        "x_wing_le_m": 0.76, "root_chord_m": 0.55, "fuselage_length_m": 2.9,
        "cl_cruise": 0.5, "cd_cruise": 0.02,
        "cruise_ld": 25.0 + 0.5 * ar, "clmax": 1.3, "sm": 0.10,
        "elapsed_s": 0.0,
    }


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_config_case_model_parsed(tmp_path):
    config = load_config(_write_config(tmp_path), offline=True)
    assert Path(config.case_model).is_absolute()
    assert Path(config.case_model).name == "baseline_case.vsp3"
    assert not any("case_model" in warning for warning in config.warnings)
    assert config.to_public_dict()["case_model"] == config.case_model


def test_config_case_model_missing_and_wrong_ext(tmp_path):
    config = load_config(
        _write_config(tmp_path, case_model=str(tmp_path / "missing.vsp3")),
        offline=True)
    assert any("case_model 不存在" in warning for warning in config.warnings)

    wrong = tmp_path / "note.txt"
    wrong.write_text("x", encoding="utf-8")
    config = load_config(_write_config(tmp_path, case_model=str(wrong)), offline=True)
    assert any("不是 .vsp3" in warning for warning in config.warnings)


# --------------------------------------------------------------------------- #
# 真实案例参数读取
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_read_real_case_parameters():
    if not CASE_PATH.is_file():
        pytest.skip("示例案例 .vsp3 不存在（运行 examples/make_sample_case.py 生成）")
    bridge = VSPBridge(logger=None)
    params = bridge.read_model_parameters(CASE_PATH)
    assert params["ok"] is True
    assert params["error"] is None
    assert params["component_count"] >= 3
    wing = params["wing"]
    assert wing["area_m2"] == pytest.approx(CASE_WING["area_m2"], rel=1e-3)
    assert wing["aspect_ratio"] == pytest.approx(CASE_WING["aspect_ratio"], rel=1e-2)
    assert wing["taper_ratio"] == pytest.approx(CASE_WING["taper_ratio"], rel=1e-2)
    assert wing["sweep_deg"] == pytest.approx(CASE_WING["sweep_deg"], abs=0.05)
    assert wing["dihedral_deg"] == pytest.approx(CASE_WING["dihedral_deg"], abs=0.05)
    assert params["fuselage"]["length_m"] == pytest.approx(
        CASE_FUSELAGE["length_m"], rel=1e-2)
    summary = summarize_case_parameters(params)
    assert summary["file"] == "baseline_case.vsp3"
    assert summary["wing"]["area_m2"] == pytest.approx(CASE_WING["area_m2"], rel=1e-3)
    assert summary["tail_area_ratio"] and summary["tail_area_ratio"] > 0


# --------------------------------------------------------------------------- #
# apply_case_baseline：案例优先 / prompt 修改优先
# --------------------------------------------------------------------------- #
def test_apply_case_baseline_case_priority():
    spec = parse_requirements("设计一架 50kg 级电动无人机，巡航高度 2000 m。")
    applied = apply_case_baseline(spec, _fake_case_parameters())
    assert applied
    assert spec.wing_area_m2 == pytest.approx(CASE_WING["area_m2"], rel=1e-4)
    assert spec.aspect_ratio == pytest.approx(CASE_WING["aspect_ratio"], rel=1e-4)
    assert spec.taper_ratio == pytest.approx(CASE_WING["taper_ratio"], rel=1e-4)
    assert spec.sweep_deg == pytest.approx(CASE_WING["sweep_deg"], rel=1e-4)
    assert spec.dihedral_deg == pytest.approx(CASE_WING["dihedral_deg"], rel=1e-4)
    assert spec.fuselage_length_m == pytest.approx(CASE_FUSELAGE["length_m"], rel=1e-4)
    assert spec.max_span_m == pytest.approx(CASE_WING["span_m"] * 1.15, rel=1e-4)
    assert spec.tail_scale_h == pytest.approx(spec.tail_scale_v)
    assert spec.sources.get("wing_area_m2") == "case_model"


def test_apply_case_baseline_prompt_override():
    spec = parse_requirements(
        "设计一架 60kg 级无人机，机翼面积 2.4 m²，展弦比 12，"
        "后掠角 8 度，机翼上反角 3 度。")
    applied = apply_case_baseline(spec, _fake_case_parameters())
    # prompt 显式给出的参数不被案例覆盖
    assert spec.wing_area_m2 == pytest.approx(2.4)
    assert spec.aspect_ratio == pytest.approx(12.0)
    assert spec.sweep_deg == pytest.approx(8.0)
    assert spec.dihedral_deg == pytest.approx(3.0)
    # 未提及的 taper / 机身沿用案例
    assert spec.taper_ratio == pytest.approx(CASE_WING["taper_ratio"], rel=1e-4)
    assert spec.fuselage_length_m == pytest.approx(CASE_FUSELAGE["length_m"], rel=1e-4)
    assert "wing_area_m2" not in applied
    assert "taper_ratio" in applied


def test_designer_case_baseline_sizing(tmp_path):
    config = load_config(_write_config(tmp_path), offline=True)
    designer = Designer(config, _logger(tmp_path))
    spec = designer.parse_requirements()
    assert designer.based_on_case is True
    assert designer.case_parameters is not None
    assert designer.case_summary["file"] == "baseline_case.vsp3"
    # prompt 修改的 AR / 后掠 / 机身加长生效；其余沿用案例
    assert spec.aspect_ratio == pytest.approx(11.0)
    assert spec.sweep_deg == pytest.approx(6.0)
    assert spec.fuselage_length_scale == pytest.approx(1.1)
    geometry = size_aircraft(spec)
    sizing = geometry["sizing"]
    wing = geometry["wing"]
    assert sizing["area_m2"] == pytest.approx(CASE_WING["area_m2"], rel=1e-3)
    assert sizing["aspect_ratio"] == pytest.approx(11.0, rel=1e-2)
    assert wing["sweep_deg"] == pytest.approx(6.0)
    assert wing["dihedral_deg"] == pytest.approx(CASE_WING["dihedral_deg"], rel=1e-3)
    assert wing["tip_chord_m"] / wing["root_chord_m"] == pytest.approx(
        CASE_WING["taper_ratio"], rel=1e-2)
    assert geometry["fuselage"]["length_m"] == pytest.approx(
        CASE_FUSELAGE["length_m"] * 1.1, rel=1e-2)
    # 尾翼面积按案例尾翼/机翼面积比保持
    assert sizing["tail_area_total_m2"] == pytest.approx(
        sizing["area_m2"] * 0.1537, rel=5e-2)
    assert any("案例" in line for line in designer.logger.tail(40))


def test_designer_case_missing_falls_back(tmp_path):
    config = load_config(
        _write_config(tmp_path, case_model=str(tmp_path / "missing.vsp3")),
        offline=True)
    designer = Designer(config, _logger(tmp_path))
    spec = designer.parse_requirements()
    assert designer.based_on_case is False
    assert spec.wing_area_m2 is None  # 回退到文本 sizing
    assert any("读取案例" in line or "case_model" in line
               for line in designer.logger.tail(40))


# --------------------------------------------------------------------------- #
# GA：case_model 优先于 seed_model
# --------------------------------------------------------------------------- #
def _ga_config(tmp_path: Path, case_path: str, seed_path: str) -> object:
    raw = {
        "provider": "deepseek",
        "prompt": "设计一架 80kg 级无人机，巡航高度 2000 m。",
        "output_dir": str(tmp_path / "ga_out"),
        "template": str(TEMPLATE),
        "mode": "offline",
        "task": "ga",
        "ga": {"population": 4, "generations": 1, "time_budget_s": 60,
               "max_evaluations": 4},
        "case_model": case_path,
        "seed_model": seed_path,
    }
    path = tmp_path / "ga_job.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return load_config(path, offline=True)


def test_ga_prefers_case_model_baseline(tmp_path):
    case_file = tmp_path / "uploaded_case.vsp3"
    case_file.write_text("<?xml version=\"1.0\"?>", encoding="utf-8")
    seed_file = tmp_path / "previous_seed.vsp3"
    seed_file.write_text("<?xml version=\"1.0\"?>", encoding="utf-8")
    config = _ga_config(tmp_path, str(case_file), str(seed_file))
    spec = parse_requirements(config.prompt)
    bridge = FakeBridge(_fake_case_parameters(),
                        {"ok": True, "wing": {"area_m2": 2.8, "aspect_ratio": 7.0,
                                              "taper_ratio": 0.5, "sweep_deg": 0,
                                              "dihedral_deg": 0},
                         "fuselage": {"length_m": 3.2}, "wings": []})
    optimizer = GAOptimizer(config, spec, bridge=bridge, logger=_logger(tmp_path),
                            output_dir=tmp_path / "ga_out",
                            evaluator=_fake_evaluator)
    assert optimizer.baseline_source == "case_model"
    assert optimizer.based_on_case is True
    assert optimizer.case_model == str(case_file)
    assert optimizer.baseline_genome["wing_area_m2"] == pytest.approx(
        CASE_WING["area_m2"], rel=1e-3)
    assert optimizer.baseline_genome["taper_ratio"] == pytest.approx(
        CASE_WING["taper_ratio"], rel=1e-3)
    result = optimizer.optimize()
    assert result["case_model"] == str(case_file)
    assert result["based_on_case"] is True
    assert result["baseline_source"] == "case_model"
    assert result["case_parameters"]["file"] == "uploaded_case.vsp3"
    # 只读取了案例文件（未读取 seed_model）
    assert all("uploaded_case" in call for call in bridge.calls)


def test_ga_case_missing_falls_back_to_seed(tmp_path):
    seed_file = tmp_path / "previous_seed.vsp3"
    seed_file.write_text("<?xml version=\"1.0\"?>", encoding="utf-8")
    config = _ga_config(tmp_path, str(tmp_path / "missing_case.vsp3"),
                        str(seed_file))
    spec = parse_requirements(config.prompt)
    seed_params = {"ok": True, "wing": {"area_m2": 2.6, "aspect_ratio": 7.5,
                                        "taper_ratio": 0.55, "sweep_deg": 1.0,
                                        "dihedral_deg": 2.0},
                   "fuselage": {"length_m": 3.1}, "wings": [],
                   "component_count": 2, "path": str(seed_file)}
    bridge = FakeBridge(None, seed_params)
    optimizer = GAOptimizer(config, spec, bridge=bridge, logger=_logger(tmp_path),
                            output_dir=tmp_path / "ga_out",
                            evaluator=_fake_evaluator)
    assert optimizer.baseline_source == "seed_model"
    assert optimizer.based_on_case is False
    assert optimizer.case_model is None
    assert optimizer.baseline_genome["wing_area_m2"] == pytest.approx(2.6)


# --------------------------------------------------------------------------- #
# agent result 字段
# --------------------------------------------------------------------------- #
def test_agent_result_case_fields(tmp_path):
    from ai_design.agent import DesignAgent

    config = load_config(_write_config(tmp_path), offline=True)
    logger = _logger(tmp_path)
    designer = Designer(config, logger)
    designer.parse_requirements()
    report = tmp_path / "report.docx"
    from docx import Document

    document = Document()
    document.add_paragraph("case report")
    document.save(str(report))
    agent = DesignAgent(config, logger=logger, designer=designer)
    result = agent._finalize_result({
        "ok": True, "model_path": str(tmp_path / "case_model.vsp3"),
        "report_path": str(report), "metrics": designer.metrics,
    })
    assert result["case_model"] == config.case_model
    assert result["based_on_case"] is True
    assert result["case_parameters"]["file"] == "baseline_case.vsp3"
    assert result["case_parameters"]["wing"]["area_m2"] == pytest.approx(
        CASE_WING["area_m2"], rel=1e-3)
    assert result["case_applied"]

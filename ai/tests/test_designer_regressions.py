"""回归测试：针对五案例验证中发现的后端缺陷。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_design.config import load_config
from ai_design.designer import Designer
from ai_design.logging_util import Logger


def _designer(tmp_path: Path, prompt: str) -> Designer:
    out = tmp_path / "run"
    out.mkdir(parents=True, exist_ok=True)
    cfg = {
        "task": "design",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "prompt": prompt,
        "output_dir": str(out),
        "mode": "offline",
        "max_iterations": 2,
        "template": str(Path(__file__).resolve().parent.parent
                        / "templates" / "design_report_template.docx"),
    }
    path = out / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    config = load_config(path, offline=True)
    return Designer(config, Logger(log_file=out / "test.log"))


@pytest.mark.slow
def test_modify_parameter_persists_sweep(tmp_path: Path):
    """修改后掠角后必须保留在重建模型中（历史 bug：被 size_aircraft 覆盖回 0）。"""
    prompt = ("设计一架 V 尾布局高速靶机：最大起飞重量 120 kg，任务海拔 8000 m，"
              "巡航速度 180 m/s，任务载荷 15 kg，翼展不超过 4.0 m，展弦比 5~7，"
              "机翼后掠角 10~20 度；采用 V 尾和 EJ115 涡喷发动机、NACA 4412 翼型。")
    designer = _designer(tmp_path, prompt)
    designer.parse_requirements()
    designer.build_aircraft()
    designer.run_analysis()
    assert designer.geometry["wing"]["sweep_deg"] == 0.0

    result = designer.modify_parameter("wing", "sweep_deg", 15.0)
    assert result["ok"] is True
    assert designer.geometry["wing"]["sweep_deg"] == 15.0
    params = designer.bridge.read_model_parameters(designer.model_path)
    assert params["ok"] is True
    assert abs(float(params["wing"]["sweep_deg"]) - 15.0) < 1e-6


@pytest.mark.slow
def test_flying_wing_reaches_static_margin_target(tmp_path: Path):
    """飞翼构型应能达到 10% MAC 静稳定裕度目标（历史 bug：重心上限过紧）。"""
    prompt = ("设计一架飞翼布局侦察无人机：最大起飞重量 15 kg，任务海拔 2000 m，"
              "巡航速度 32 m/s，任务载荷 3 kg，翼展不超过 3.0 m，展弦比 7，"
              "机翼后掠角 25 度；采用飞翼布局（无独立尾翼）、电动机和 NACA 4412 翼型。")
    designer = _designer(tmp_path, prompt)
    designer.parse_requirements()
    designer.build_aircraft()
    designer.run_analysis()
    stab = designer.metrics["stability"]
    assert stab["stable"] is True
    assert abs(float(stab["static_margin"]) - 0.10) < 0.02
    # metrics 中同时给出实际几何，避免需求/实际不一致时误读
    design = designer.metrics["design"]
    assert design["actual_aspect_ratio"] is not None
    assert abs(float(design["actual_aspect_ratio"]) - 7.0) < 0.05

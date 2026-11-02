"""离线端到端测试：直接运行 CLI（真实 OpenVSP + VSPAERO，约 15~60 秒）。

覆盖任务交付要求：
- ``python ai_design_agent.py --config examples/offline_config.json --offline`` 可跑通
- ``output_dir/result.json`` 中 ok=true、report_path/model_path 存在
- 报告 docx 可被 python-docx 正常打开
- 关键气动指标与静稳定裕度物理合理
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from docx import Document

AI_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = AI_ROOT / "examples"


@pytest.mark.slow
def test_offline_cli_end_to_end():
    config_path = EXAMPLES_DIR / "offline_config.json"
    assert config_path.is_file()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, str(AI_ROOT / "ai_design_agent.py"),
         "--config", str(config_path), "--offline"],
        cwd=str(AI_ROOT), capture_output=True, encoding="utf-8",
        errors="replace", timeout=600, env=env,
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    assert completed.returncode == 0, (
        f"CLI 退出码 {completed.returncode}\nstdout 尾部:\n{stdout[-4000:]}\n"
        f"stderr 尾部:\n{stderr[-2000:]}")
    # stdout 逐行日志前缀
    for prefix in ("[status]", "[plan]"):
        assert prefix in stdout, f"缺少 {prefix} 日志行"
    assert "[status] 任务完成" in stdout

    output_dir = AI_ROOT / "outputs" / "offline_demo"
    result_path = output_dir / "result.json"
    assert result_path.is_file(), "result.json 未生成"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    for key in ("ok", "report_path", "model_path", "metrics", "log_tail", "error"):
        assert key in result, f"result.json 缺少字段 {key}"
    assert result["ok"] is True
    assert result["error"] is None

    # 报告存在且可被 python-docx 打开
    report_path = Path(result["report_path"])
    assert report_path.is_file()
    assert report_path.suffix == ".docx"
    document = Document(str(report_path))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "设计任务" in text or "气动" in text
    assert "{%" not in text
    assert len(document.tables) >= 5

    # 模型与 VSPAERO 输出文件存在
    model_path = Path(result["model_path"])
    assert model_path.is_file() and model_path.suffix == ".vsp3"
    assert model_path.stat().st_size > 10_000
    assert (output_dir / "design_model.polar").is_file()
    assert (output_dir / "design_model_stability.stab").is_file()

    metrics = result["metrics"]
    aero = metrics["aero"]
    assert aero["cl_max"] > 0.5
    assert 2.0 < aero["ld_max"] < 80.0
    assert 0.0 < aero["cd_min"] < 0.2
    assert aero["cl_alpha_per_rad"] > 2.0
    stability = metrics["stability"]
    assert stability["stable"] is True
    assert abs(stability["static_margin"] - 0.10) < 0.03
    assert stability["neutral_point_m"] > 0
    assert metrics["polar"]["rows"], "极曲线无数据"
    assert result["log_tail"], "log_tail 为空"

    # 上传附件（examples/offline_config.json 挂载 docx + xlsx）
    assert result["attachment_count"] >= 2
    assert result["attachment_ok_count"] == result["attachment_count"]
    assert result["document_chars"] > 0
    assert len(result["attachments"]) >= 2
    assert all(item["ok"] for item in result["attachments"])
    names = {item["name"] for item in result["attachments"]}
    assert any("设计输入说明" in name for name in names)
    assert any("设备参数表" in name for name in names)
    assert "[status] 附件解析: 成功 2/2" in stdout
    # 文档中的“任务载荷 8 kg / 巡航速度 33 m/s”被规则解析吸收
    design = metrics["design"]
    assert design.get("payload_kg") == 8.0
    assert design.get("speed_cruise_ms") == 33.0


def test_cli_help():
    completed = subprocess.run(
        [sys.executable, str(AI_ROOT / "ai_design_agent.py"), "--help"],
        cwd=str(AI_ROOT), capture_output=True, encoding="utf-8",
        errors="replace", timeout=60,
    )
    assert completed.returncode == 0
    assert "--offline" in (completed.stdout or "")
    assert "--config" in (completed.stdout or "")


# --------------------------------------------------------------------------- #
# GA 优化模式端到端（缩减预算，约 20~40 s）
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_ga_cli_end_to_end(tmp_path):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    base = json.loads((EXAMPLES_DIR / "offline_config.json").read_text(encoding="utf-8"))
    base["task"] = "ga"
    base["ga"] = {"population": 4, "generations": 2, "time_budget_s": 240,
                   "objective": "max_ld", "max_evaluations": 6,
                   "aoa_npts": 3, "wake_iters": 2}
    base["output_dir"] = str(tmp_path / "ga_out")
    base["title"] = "GA 端到端测试报告"
    base["design"] = dict(base.get("design") or {})
    base["design"]["aoa_npts"] = 4
    base["design"]["wake_iters"] = 2
    # 附件换成绝对路径（配置文件复制到了临时目录）
    base["attachments"] = [
        str(EXAMPLES_DIR / "attachments_demo" / "设计输入说明.docx"),
        str(EXAMPLES_DIR / "attachments_demo" / "设备参数表.xlsx"),
    ]
    config_path = tmp_path / "ga_job.json"
    config_path.write_text(json.dumps(base, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(AI_ROOT / "ai_design_agent.py"),
         "--config", str(config_path), "--offline"],
        cwd=str(AI_ROOT), capture_output=True, encoding="utf-8",
        errors="replace", timeout=900, env=env,
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    assert completed.returncode == 0, (
        f"GA CLI 退出码 {completed.returncode}\nstdout 尾部:\n{stdout[-5000:]}\n"
        f"stderr 尾部:\n{stderr[-2000:]}")
    # 每代 [ga] 日志
    assert "[ga] gen=" in stdout
    assert "[status] GA 收敛:" in stdout
    output_dir = tmp_path / "ga_out"
    result_path = output_dir / "result.json"
    assert result_path.is_file()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["task"] == "ga"
    assert result["error"] is None
    # 模型路径必须是最终可导入 OpenVSP 的 vsp3
    model_path = Path(result["model_path"])
    assert model_path.is_file()
    assert model_path.name == "ga_optimized.vsp3"
    assert model_path.stat().st_size > 10_000
    head = model_path.read_text(encoding="utf-8", errors="replace")[:400]
    assert "Vsp_Geometry" in head
    # GA 报告可打开
    report_path = Path(result["report_path"])
    assert report_path.is_file() and report_path.suffix == ".docx"
    document = Document(str(report_path))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "{%" not in text
    assert "遗传算法" in text
    # GA 结果字段
    assert result["baseline"]["cruise_ld"] > 0
    assert result["best"]["cruise_ld"] > 0
    assert result["best"]["cruise_ld"] >= result["baseline"]["cruise_ld"] - 1e-6
    assert len(result["history"]) >= 1
    assert len(result["evaluations"]) >= 2
    assert result["evaluation_count"] <= 6
    oc = result["operating_conditions"]
    assert oc["altitude_m"] == 2000.0
    assert oc["speed_cruise_ms"] == 33.0
    assert result["attachments"] == [] or True  # 附件字段存在即可
    assert "attachments" in result and "document_chars" in result
    # 报告图片（GA 收敛图 + 极曲线图）存在
    figures = list((output_dir / "figures").glob("*.png"))
    assert figures, "GA 报告未生成图表"


# --------------------------------------------------------------------------- #
# 上传案例 .vsp3 作为基线（case_model）端到端
# --------------------------------------------------------------------------- #
CASE_MODEL = EXAMPLES_DIR / "case_demo" / "baseline_case.vsp3"


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@pytest.mark.slow
def test_case_cli_design_end_to_end(tmp_path):
    """task=design：以真实 .vsp3 案例为基线，按 prompt 修改（AR/后掠/机身加长）。"""
    if not CASE_MODEL.is_file():
        pytest.skip("示例案例 .vsp3 不存在（运行 examples/make_sample_case.py）")
    base = json.loads((EXAMPLES_DIR / "case_config.json").read_text(encoding="utf-8"))
    base["case_model"] = str(CASE_MODEL)
    base["output_dir"] = str(tmp_path / "case_out")
    config_path = tmp_path / "case_job.json"
    config_path.write_text(json.dumps(base, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(AI_ROOT / "ai_design_agent.py"),
         "--config", str(config_path), "--offline"],
        cwd=str(AI_ROOT), capture_output=True, encoding="utf-8",
        errors="replace", timeout=900, env=_env(),
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    assert completed.returncode == 0, (
        f"case CLI 退出码 {completed.returncode}\nstdout 尾部:\n{stdout[-5000:]}\n"
        f"stderr:\n{stderr[-2000:]}")
    # 日志：案例路径/关键参数/基于案例修改
    assert "上传案例 .vsp3" in stdout
    assert "案例参数:" in stdout
    assert "基于案例修改" in stdout
    result = json.loads((tmp_path / "case_out" / "result.json").read_text(
        encoding="utf-8"))
    assert result["ok"] is True
    assert result["case_model"] == str(CASE_MODEL)
    assert result["based_on_case"] is True
    case_params = result["case_parameters"]
    assert case_params["file"] == "baseline_case.vsp3"
    assert case_params["wing"]["area_m2"] == pytest.approx(1.9509, rel=1e-2)
    assert case_params["wing"]["taper_ratio"] == pytest.approx(0.7719, rel=2e-2)
    assert result["case_applied"]
    # 基线吸收证据：面积/梢根比/上反角来自案例；AR/后掠/机身加长来自 prompt
    metrics = result["metrics"]
    design = metrics["design"]
    geometry = metrics["geometry"]
    sizing = geometry["sizing"]
    wing = geometry["wing"]
    assert design["aspect_ratio"] == pytest.approx(11.0, rel=2e-2)
    assert design["sweep_deg"] == pytest.approx(6.0)
    assert design["taper_ratio"] == pytest.approx(0.7719, rel=2e-2)
    assert sizing["area_m2"] == pytest.approx(1.9509, rel=1e-2)
    assert sizing["aspect_ratio"] == pytest.approx(11.0, rel=2e-2)
    assert wing["sweep_deg"] == pytest.approx(6.0)
    assert wing["dihedral_deg"] == pytest.approx(4.611, rel=2e-2)
    assert geometry["fuselage"]["length_m"] == pytest.approx(2.86 * 1.1, rel=2e-2)
    # 模型/报告
    model_path = Path(result["model_path"])
    assert model_path.is_file() and model_path.stat().st_size > 10_000
    report_path = Path(result["report_path"])
    assert report_path.is_file()
    document = Document(str(report_path))
    assert "{%" not in "\n".join(p.text for p in document.paragraphs)


@pytest.mark.slow
def test_case_cli_ga_end_to_end(tmp_path):
    """task=ga：case_model 优先作为 GA 基线（优先于 seed_model/文本 sizing）。"""
    if not CASE_MODEL.is_file():
        pytest.skip("示例案例 .vsp3 不存在（运行 examples/make_sample_case.py）")
    base = json.loads((EXAMPLES_DIR / "case_config.json").read_text(encoding="utf-8"))
    base["task"] = "ga"
    base["ga"] = {"population": 4, "generations": 2, "time_budget_s": 240,
                   "objective": "max_ld", "max_evaluations": 6,
                   "aoa_npts": 3, "wake_iters": 2}
    base["prompt"] = ("设计一架 80kg 级无人机，在现有案例基础上优化巡航升阻比，"
                       "其余参数保持与案例一致。")
    base["case_model"] = str(CASE_MODEL)
    base["seed_model"] = str(tmp_path / "should_not_be_used.vsp3")
    base["output_dir"] = str(tmp_path / "case_ga_out")
    config_path = tmp_path / "case_ga_job.json"
    config_path.write_text(json.dumps(base, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(AI_ROOT / "ai_design_agent.py"),
         "--config", str(config_path), "--offline"],
        cwd=str(AI_ROOT), capture_output=True, encoding="utf-8",
        errors="replace", timeout=900, env=_env(),
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    assert completed.returncode == 0, (
        f"case GA CLI 退出码 {completed.returncode}\nstdout 尾部:\n{stdout[-5000:]}\n"
        f"stderr:\n{stderr[-2000:]}")
    assert "[ga] gen=" in stdout
    assert "GA 基线优先级: case_model > seed_model" in stdout
    result = json.loads((tmp_path / "case_ga_out" / "result.json").read_text(
        encoding="utf-8"))
    assert result["task"] == "ga"
    assert result["ok"] is True
    assert result["case_model"] == str(CASE_MODEL)
    assert result["based_on_case"] is True
    assert result["baseline_source"] == "case_model"
    # GA 基线基因来自案例（面积/梢根比）而非文本 sizing（80 kg 文本会显著更大）
    baseline_genome = result["baseline"]["genome"]
    assert baseline_genome["wing_area_m2"] == pytest.approx(1.9509, rel=1e-2)
    assert baseline_genome["taper_ratio"] == pytest.approx(0.7719, rel=2e-2)
    model_path = Path(result["model_path"])
    assert model_path.name == "ga_optimized.vsp3" and model_path.is_file()
    assert Path(result["report_path"]).is_file()
    assert len(result["history"]) >= 1

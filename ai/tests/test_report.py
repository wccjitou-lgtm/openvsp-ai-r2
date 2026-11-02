"""report.py 单元测试：模板填充、表格/图片、占位符清理、中文内容。"""

from __future__ import annotations

import math
from pathlib import Path

from docx import Document

from ai_design.report import generate_report
AI_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = AI_ROOT / "templates" / "design_report_template.docx"


def _sample_metrics() -> dict:
    rows = [
        {"AoA": -4.0, "CLtot": -0.21, "CDtot": 0.0142, "CMytot": 0.03,
         "CStot": 0.0, "Beta": 0.0},
        {"AoA": -2.0, "CLtot": 0.05, "CDtot": 0.0128, "CMytot": 0.01,
         "CStot": 0.0, "Beta": 0.0},
        {"AoA": 0.0, "CLtot": 0.33, "CDtot": 0.0138, "CMytot": -0.01,
         "CStot": 0.0, "Beta": 0.0},
        {"AoA": 2.0, "CLtot": 0.52, "CDtot": 0.0201, "CMytot": -0.03,
         "CStot": 0.0, "Beta": 0.0},
        {"AoA": 4.0, "CLtot": 0.72, "CDtot": 0.0301, "CMytot": -0.05,
         "CStot": 0.0, "Beta": 0.0},
        {"AoA": 6.0, "CLtot": 0.91, "CDtot": 0.0452, "CMytot": -0.07,
         "CStot": 0.0, "Beta": 0.0},
    ]
    return {
        "design": {
            "name": "50 kg 级电动无人机", "mtow_kg": 50.0, "altitude_m": 2000.0,
            "speed_min_ms": 25.0, "speed_max_ms": 45.0, "speed_cruise_ms": 33.0,
            "configuration": "conventional", "configuration_label": "常规尾翼布局",
            "engine": "electric", "static_margin": 0.10,
            "airfoil_root": "NACA4412", "max_span_m": 4.2,
            "assumptions": ["未给出巡航速度，按速度范围 40% 位置估算"],
        },
        "geometry": {
            "configuration": "conventional",
            "wing": {"span_m": 3.94, "root_chord_m": 0.547, "tip_chord_m": 0.328,
                     "sweep_deg": 0.0, "dihedral_deg": 2.0, "airfoil": "NACA4412"},
            "fuselage": {"length_m": 2.72, "max_diameter_m": 0.222},
            "htail": {"span_m": 1.04, "area_m2": 0.272},
            "vtail_conventional": {"span_m": 0.61, "area_m2": 0.168},
            "sizing": {"area_m2": 1.724, "aspect_ratio": 9.0, "mac_m": 0.4468,
                       "xcg_initial_m": 0.8723},
            "components": [
                {"role": "wing", "name": "MainWing",
                 "params": {"span_m": 3.94, "area_m2": 1.724},
                 "control_surfaces": [{"name": "Aileron"}]},
                {"role": "fuselage", "name": "Fuselage",
                 "params": {"length_m": 2.72, "max_diameter_m": 0.222}},
            ],
        },
        "aero": {
            "ok": True, "cl_max": 1.293, "alpha_at_cl_max_deg": 10.0,
            "cl_at_zero_deg": 0.33, "cl_alpha_per_rad": 5.47, "cd_min": 0.0098,
            "cd0_estimate": 0.0098, "ld_max": 25.92, "alpha_at_ld_max_deg": 2.0,
            "alpha_zero_lift_deg": -3.1, "stable": True,
        },
        "stability": {
            "static_margin": 0.1001, "static_margin_target": 0.10,
            "neutral_point_m": 0.9608, "xcg_m": 0.9161, "stable": True,
            "derivatives": {"cl_alpha_per_rad": 5.32, "cm_alpha_per_rad": -1.05,
                            "cl_q": 9.69, "cm_q": -14.6},
            "sm_method": "VSPAERO .stab（SM/X_np）",
        },
        "cruise": {"velocity_ms": 33.0, "cl_cruise": 0.519, "cd_cruise": 0.0201,
                   "ld_cruise": 25.86, "power_required_kw": 0.63},
        "optimization": {
            "baseline": {"area_m2": 1.724, "aspect_ratio": 9.0, "ld_cruise": 26.6},
            "best": {"area_m2": 1.724, "aspect_ratio": 9.9, "ld_cruise": 28.0},
            "improvement_ratio": 0.053, "rebuild": False,
        },
        "polar": {"columns": [], "rows": rows},
        "polar_rows": rows,
        "files": {"vsp3": "D:/out/design_model.vsp3", "stl": "D:/out/design_model.stl"},
        "mode": "offline",
    }


def test_generate_report_from_template(tmp_path):
    metrics = _sample_metrics()
    path = generate_report(output_dir=str(tmp_path), template=str(TEMPLATE),
                           title="50 kg 级电动无人机气动设计报告",
                           metrics=metrics)
    from pathlib import Path

    report = Path(path)
    assert report.is_file()
    assert report.stat().st_size > 20_000
    doc = Document(str(report))
    assert len(doc.tables) >= 6
    text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "设计任务与要求" in text
    assert "气动特性计算结果" in text
    assert "结论与建议" in text
    assert "{%" not in text, "报告中残留占位符"
    # 插图（CL-α / L/D-α / 极曲线）
    images = [rel for rel in doc.part.rels.values() if "image" in rel.reltype]
    assert len(images) >= 2
    # 表格中应含气动数据
    all_cell_text = " ".join(
        cell.text for table in doc.tables for row in table.rows for cell in row.cells)
    assert "CL" in all_cell_text or "升力" in all_cell_text


def test_generate_report_without_template_falls_back(tmp_path):
    metrics = _sample_metrics()
    path = generate_report(output_dir=str(tmp_path),
                           template=str(tmp_path / "missing.docx"),
                           title="降级报告", metrics=metrics)
    from pathlib import Path

    doc = Document(str(Path(path)))
    text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "降级报告" in text
    assert "{%" not in text


def test_generate_report_minimal_data(tmp_path):
    """无有效极曲线数据时也能生成报告（不抛异常）。"""
    path = generate_report(output_dir=str(tmp_path), template=str(TEMPLATE),
                           title="空数据报告", metrics={"design": {"name": "测试机"}})
    from pathlib import Path

    doc = Document(str(Path(path)))
    text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "空数据报告" in text

def test_generate_report_mcp_style(tmp_path):
    """MCP 服务器调用方式：result/spec/geometry(部件列表)/reference/report_path 关键字参数，
    且极曲线行使用 MCP 工具层字段命名 alpha_deg/CL/CD/Cm/...（应自动兼容并出图）。"""
    rows = [
        {"alpha_deg": -2.0, "CL": 0.10, "CD": 0.014, "Cm": 0.01,
         "CS": 0.0, "beta_deg": 0.0, "mach": 0.1},
        {"alpha_deg": 0.0, "CL": 0.30, "CD": 0.015, "Cm": 0.00,
         "CS": 0.0, "beta_deg": 0.0, "mach": 0.1},
        {"alpha_deg": 2.0, "CL": 0.50, "CD": 0.020, "Cm": -0.02,
         "CS": 0.0, "beta_deg": 0.0, "mach": 0.1},
        {"alpha_deg": 4.0, "CL": 0.70, "CD": 0.030, "Cm": -0.03,
         "CS": 0.0, "beta_deg": 0.0, "mach": 0.1},
    ]
    result = {
        "ok": True, "rows": rows,
        "summary": {"cl_max": 0.7, "ld_max": 18.0, "cd_min": 0.013},
        "stability": {"static_margin": 0.1, "stable": True},
        "files": {"vsp3": "x.vsp3"},
        "reference": {"sref_m2": 1.2, "cref_m": 0.4, "bref_m": 3.0, "xcg_m": 0.5},
        "flow": {"velocity_ms": 30.0, "altitude_m": 1000.0,
                 "density_kgm3": 1.11},
    }
    spec = {"name": "MCP 测试机", "mtow_kg": 40, "altitude_m": 1000,
            "speed_min_ms": 22, "speed_max_ms": 40,
            "config": "conventional", "engine": "electric"}
    geometry = [
        {"name": "MainWing", "role": "wing",
         "params": {"span_m": 3.0, "area_m2": 1.2}},
        {"name": "Fuselage", "role": "fuselage",
         "params": {"length_m": 2.0}},
    ]
    target = tmp_path / "mcp_report.docx"
    path = generate_report(result=result, spec=spec, geometry=geometry,
                           reference=result["reference"],
                           report_path=str(target))
    assert Path(path).is_file()
    document = Document(path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "MCP 测试机" in text
    assert "{%" not in text
    # MCP 行键应自动映射并生成 3 张图
    figures = list((tmp_path / "figures").glob("*.png"))
    assert len(figures) >= 2, f"MCP 行键未生成图表: {figures}"
    # 极曲线表应包含 CL 数值
    cell_text = " ".join(
        cell.text for table in document.tables for row in table.rows
        for cell in row.cells)
    assert "0.7000" in cell_text or "0.7" in cell_text


def test_normalize_polar_row_mcp_keys():
    from ai_design.report import normalize_polar_row

    row = normalize_polar_row({"alpha_deg": 2.0, "CL": 0.5, "CD": 0.02,
                               "Cm": -0.03, "CS": 0.01, "beta_deg": 0.0,
                               "mach": 0.12, "Cl": 0.004, "Cn": 0.002})
    assert row["AoA"] == 2.0
    assert row["CLtot"] == 0.5
    assert row["CDtot"] == 0.02
    assert row["CMytot"] == -0.03
    assert row["CMxtot"] == 0.004
    assert row["CMztot"] == 0.002
    assert row["Beta"] == 0.0
    assert row["Mach"] == 0.12
    # 已有规范键不被覆盖
    row2 = normalize_polar_row({"AoA": 3.0, "alpha_deg": 9.0, "CLtot": 0.8,
                                "CL": 0.1})
    assert row2["AoA"] == 3.0
    assert row2["CLtot"] == 0.8

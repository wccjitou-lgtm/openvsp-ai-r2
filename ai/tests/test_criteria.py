"""经验设计准则评估回归测试。"""
from __future__ import annotations

from ai_design.criteria import evaluate_design
from ai_design.designer import DesignSpec, size_aircraft


def test_criteria_sections_and_checks():
    spec = DesignSpec(mtow_kg=25, altitude_m=1000, speed_cruise_ms=25,
                      speed_min_ms=25, speed_max_ms=40, payload_kg=5,
                      wing_area_m2=1.33, aspect_ratio=9,
                      configuration="conventional")
    geometry = size_aircraft(spec)
    metrics = {
        "design": {"static_margin": 0.10},
        "flow": {"velocity_ms": 25.0, "density_kgm3": 1.11},
        "cruise": {"cl_cruise": 0.7, "cd_cruise": 0.04},
        "aero": {"cl_max": 1.1},
        "stability": {
            "static_margin": 0.10,
            "derivatives": {
                "cl_alpha_per_rad": 4.0,
                "cm_alpha_per_rad": -0.5,
                "cl_q": 5.0,
                "cm_q": -7.0,
                "cl_beta_per_rad": -0.055,
                "cy_beta_per_rad": -0.17,
                "cn_beta_per_rad": 0.08,
                "cn_r": -0.10,
                "cl_p": -0.58,
            },
        },
    }
    result = evaluate_design(spec, geometry, metrics)
    assert result["available"] is True
    for key in ("wing", "tail", "stability", "flying_qualities",
                "controllability"):
        assert key in result
        assert result[key]["rows"]
    assert result["summary"]["total"] >= 20
    assert result["overall"] in ("pass", "warn", "fail")
    assert result["flying_qualities"]["dynamic"]["short_period"]["stable"] is True

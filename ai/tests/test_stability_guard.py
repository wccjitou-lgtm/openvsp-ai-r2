"""静稳定检查与多轮优化建议回归测试。"""
from __future__ import annotations

from ai_design.designer import DesignSpec
from ai_design.stability_guard import adjustment_plan, apply_changes, assess_static_stability


def _metrics(cma: float, cnb: float, sm: float = 0.10) -> dict:
    return {
        "design": {"static_margin": 0.10},
        "stability": {
            "static_margin": sm,
            "derivatives": {
                "cm_alpha_per_rad": cma,
                "cn_beta_per_rad": cnb,
                "cy_beta_per_rad": -0.10,
            },
        },
    }


def test_stability_assessment_passes_conventional_stable_case():
    result = assess_static_stability(_metrics(-0.5, 0.08))
    assert result["stable"] is True
    assert result["pitch"]["status"] == "pass"
    assert result["yaw"]["status"] == "pass"


def test_pitch_and_yaw_instability_choose_layout_adjustments():
    spec = DesignSpec(configuration="conventional")
    plan = adjustment_plan(spec, assess_static_stability(_metrics(0.1, 0.001)))
    assert plan["pitch_bad"] is True
    assert plan["yaw_bad"] is True
    assert plan["changes"]["tail_volume_h"] > spec.tail_volume_h
    assert plan["changes"]["tail_volume_v"] > spec.tail_volume_v


def test_canard_pitch_instability_reduces_canard_area():
    spec = DesignSpec(configuration="canard")
    plan = adjustment_plan(spec, assess_static_stability(_metrics(0.2, 0.08)))
    assert plan["changes"]["canard_area_scale"] < 1.0


def test_flying_wing_yaw_instability_adds_vertical_fin():
    spec = DesignSpec(configuration="flying_wing")
    plan = adjustment_plan(spec, assess_static_stability(_metrics(-0.5, 0.0)))
    assert plan["changes"]["flying_wing_vertical_tail"] is True

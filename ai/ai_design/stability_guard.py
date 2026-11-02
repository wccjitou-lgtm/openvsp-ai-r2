# -*- coding: utf-8 -*-
"""静稳定性检查与多轮优化建议（首轮后使用）。

只判断俯仰/航向静稳定性并给出总体参数调整，不直接计算气动。
"""
from __future__ import annotations

from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def assess_static_stability(metrics: dict[str, Any]) -> dict[str, Any]:
    """返回俯仰/航向静稳定状态：pass/fail/unknown。"""
    metrics = _as_dict(metrics)
    stability = _as_dict(metrics.get("stability"))
    design = _as_dict(metrics.get("design"))
    derivatives = _as_dict(stability.get("derivatives"))
    sm = _num(stability.get("static_margin"))
    target = _num(design.get("static_margin")) or 0.10
    cma = _num(derivatives.get("cm_alpha_per_rad"))
    cnb = _num(derivatives.get("cn_beta_per_rad"))
    cyb = _num(derivatives.get("cy_beta_per_rad"))

    pitch_reasons = []
    pitch_status = "unknown"
    if sm is not None and cma is not None:
        pitch_reasons.append(f"SM={sm*100:.2f}% MAC；CMα={cma:.4f}/rad")
        if sm >= max(0.02, target - 0.04) and cma < 0.0:
            pitch_status = "pass"
        else:
            pitch_status = "fail"
            if sm < 0.02 or sm < target - 0.04:
                pitch_reasons.append("静稳定裕度不足或偏小")
            if cma >= 0.0:
                pitch_reasons.append("CMα≥0，俯仰静不稳定")
    elif sm is not None or cma is not None:
        pitch_reasons.append("缺少 SM 或 CMα，无法判定俯仰静稳定")

    yaw_reasons = []
    yaw_status = "unknown"
    if cnb is not None:
        yaw_reasons.append(f"Cnβ={cnb:.4f}/rad" + (f"；Cyβ={cyb:.4f}/rad" if cyb is not None else ""))
        if cnb >= 0.02 and (cyb is None or cyb < 0.0):
            yaw_status = "pass"
        else:
            yaw_status = "fail"
            if cnb < 0.02:
                yaw_reasons.append("Cnβ 不足，航向静不稳定/接近中性")
            if cyb is not None and cyb >= 0.0:
                yaw_reasons.append("Cyβ 符号异常")
    elif cyb is not None:
        yaw_reasons.append("缺少 Cnβ，无法判定航向静稳定")

    return {
        "pitch": {"status": pitch_status, "reason": "；".join(pitch_reasons),
                  "sm": sm, "target_sm": target, "cma": cma},
        "yaw": {"status": yaw_status, "reason": "；".join(yaw_reasons),
                "cnb": cnb, "cyb": cyb},
        "stable": pitch_status == "pass" and yaw_status == "pass",
        "unknown": pitch_status == "unknown" or yaw_status == "unknown",
    }


def adjustment_plan(spec: Any, assessment: dict[str, Any]) -> dict[str, Any]:
    """根据布局选择保守的下一步参数调整。"""
    changes: dict[str, Any] = {}
    notes: list[str] = []
    pitch_bad = _as_dict(assessment.get("pitch")).get("status") != "pass"
    yaw_bad = _as_dict(assessment.get("yaw")).get("status") != "pass"
    config = str(getattr(spec, "configuration", "conventional") or "conventional")

    if pitch_bad:
        if config == "canard":
            old = float(getattr(spec, "canard_area_scale", 1.0) or 1.0)
            changes["canard_area_scale"] = max(0.45, old * 0.82)
            notes.append("鸭翼布局：缩小鸭翼面积，降低前翼对焦点的前移")
        elif config == "flying_wing":
            old = float(getattr(spec, "static_margin", 0.10) or 0.10)
            changes["static_margin"] = min(0.20, old + 0.03)
            notes.append("飞翼：提高静稳定目标，使重心前移；仍不保证真实气动稳定")
        else:
            old = float(getattr(spec, "tail_volume_h", 0.50) or 0.50)
            changes["tail_volume_h"] = min(1.20, old * 1.25)
            notes.append("常规/串列/V 尾：增大平尾容量，增强俯仰恢复")

    if yaw_bad:
        if config == "flying_wing" and not bool(getattr(spec, "flying_wing_vertical_tail", False)):
            changes["flying_wing_vertical_tail"] = True
            notes.append("飞翼：增加中央垂尾以提供航向静稳定")
        else:
            old = float(getattr(spec, "tail_volume_v", 0.035) or 0.035)
            changes["tail_volume_v"] = min(0.100, old * 1.30)
            notes.append("增大垂尾容量，修复航向静稳定")

    return {"changes": changes, "notes": notes,
            "pitch_bad": pitch_bad, "yaw_bad": yaw_bad}


def apply_changes(spec: Any, plan: dict[str, Any]) -> list[str]:
    applied = []
    for key, value in _as_dict(plan.get("changes")).items():
        old = getattr(spec, key, None)
        setattr(spec, key, value)
        applied.append(f"{key}: {old} → {value}")
    return applied


__all__ = ["assess_static_stability", "adjustment_plan", "apply_changes"]

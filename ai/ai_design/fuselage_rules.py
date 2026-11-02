# -*- coding: utf-8 -*-
"""机身规则：圆截面、平滑曲率、经验长度与机头位置。

经验依据（总体设计常用范围；参见 Raymer/Roskam 总体设计章节）：
- 常规小型固定翼机身长度约为 0.70~0.80 翼展；
- 高空长航时/细长机身约为 0.70 翼展；
- 飞翼中央体约为 0.50~0.55 翼展；
- 机翼前缘约位于机身长度的 22%~30% 处（机头段 22%~30% L）；
- 尾臂约 2.5~3.0 MAC（由尾容量和重心/焦点再修正）。

本模块生成圆截面站位（宽度=高度），并用余弦/平滑过渡曲线生成机头、
直中段和收细尾段，避免截面之间出现明显折角。
"""
from __future__ import annotations

import math
from typing import Any


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _smoothstep(t: float) -> float:
    t = _clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


STYLE_ID = "circular_profile"
STYLE_LABEL = "十截面圆筒机身（首尾半半径）"
STYLE_DESC = ("机身由 10 个均匀分布的圆截面组成：首、尾截面半径为 0.5R，"
              "其余 8 个截面半径均为 R，中段为标准圆柱；"
              "首尾采用圆形端盖；长度按 0.80~0.84 翼展经验并受尾臂/重心/焦点约束；"
              "半径按工况确定。")


def list_styles() -> list[dict[str, Any]]:
    return [{"style": STYLE_ID, "label": STYLE_LABEL,
             "description": STYLE_DESC, "width_ratio": 1.0}]


def _radius_from_conditions(spec: Any, length_m: float = 0.0) -> tuple[float, str]:
    mtow = max(float(getattr(spec, "mtow_kg", 30.0) or 30.0), 1.0)
    payload = float(getattr(spec, "payload_kg", 0.0) or 0.0)
    payload_frac = payload / mtow
    speed = max(float(getattr(spec, "speed_cruise_ms", 30.0) or 30.0),
                float(getattr(spec, "speed_max_ms", 30.0) or 30.0))
    altitude = float(getattr(spec, "altitude_m", 0.0) or 0.0)
    engine = str(getattr(spec, "engine", "") or "").lower()

    radius = 0.055 * mtow ** (1.0 / 3.0)
    factor = 1.0
    reasons = [f"基准 r=0.055·MTOW^(1/3)={radius:.3f} m"]
    if engine not in ("electric", "") or "ej" in engine or "turb" in engine:
        factor *= 0.82
        reasons.append("喷气/涡桨动力 → 迎风面积减小")
    if speed >= 90.0:
        factor *= 0.85
        reasons.append("高速 → 半径×0.85")
    elif speed <= 35.0:
        factor *= 1.03
        reasons.append("低速 → 半径×1.03")
    if altitude >= 5000.0:
        factor *= 0.92
        reasons.append("高空 → 半径×0.92")
    if payload_frac >= 0.22:
        factor *= 1.12
        reasons.append("大载荷 → 半径×1.12")
    mass_radius = radius * factor

    # 设计原则 1：内部容积约束。按载荷/设备体积求圆柱半径。
    payload_volume = max(payload, 0.0) / 250.0 + 0.02
    length_for_volume = max(float(length_m or 0.0), 0.8)
    volume_radius = math.sqrt(payload_volume / (math.pi * length_for_volume))

    # 设计原则 2：长径比约束。低速无人机常用 L/D≈8~12，
    # 高空长航时允许更细长（L/D≈10~14）。
    config = str(getattr(spec, "configuration", "") or "")
    if altitude >= 5000.0:
        ld_limit = 14.0
    elif config == "flying_wing":
        ld_limit = 8.0
    else:
        ld_limit = 12.0
    fineness_radius = length_for_volume / (2.0 * ld_limit)

    radius = max(mass_radius, volume_radius, fineness_radius)
    reasons.append(f"质量基准半径 {mass_radius:.3f} m")
    reasons.append(f"容积基准半径 {volume_radius:.3f} m")
    reasons.append(f"长径比约束半径 {fineness_radius:.3f} m（目标 L/D≤{ld_limit:.0f}）")
    reasons.append(f"取最大值 r={radius:.3f} m")
    radius = _clamp(radius, 0.08, 0.65)
    return radius, "；".join(reasons)


def _make_profile(length: float, nose: float, tail_start: float,
                  tail_ratio: float = 0.24, nose_ratio: float = 0.03) -> list[list[float]]:
    """10 个均匀圆截面；首、尾截面半径 0.5R，其余 8 个为 R。

    首尾端盖仍由 OpenVSP 设为 ROUND_END_CAP。
    """
    del nose, tail_start, tail_ratio, nose_ratio
    stations: list[list[float]] = []
    for i in range(10):
        factor = 0.5 if i in (0, 9) else 1.0
        stations.append([round(i / 9.0, 5), factor, factor, 0.0])
    return stations

def build_fuselage_rules(spec: Any, span_m: float, mac_m: float,
                         root_chord_m: float | None = None,
                         tip_chord_m: float | None = None) -> dict[str, Any]:
    span = max(float(span_m), 0.3)
    mac = max(float(mac_m), 0.05)
    root = float(root_chord_m or (0.8 * mac))
    tip = float(tip_chord_m or (root * float(getattr(spec, "taper_ratio", 0.6) or 0.6)))
    area = 0.5 * (root + tip) * span
    config = str(getattr(spec, "configuration", "") or "conventional")
    engine = str(getattr(spec, "engine", "") or "").lower()
    speed = max(float(getattr(spec, "speed_cruise_ms", 30.0) or 30.0),
                float(getattr(spec, "speed_max_ms", 30.0) or 30.0))
    altitude = float(getattr(spec, "altitude_m", 0.0) or 0.0)
    sm = float(getattr(spec, "static_margin", 0.10) or 0.10)
    vh = float(getattr(spec, "tail_volume_h", 0.50) or 0.50)
    explicit_length = getattr(spec, "fuselage_length_m", None)

    if config == "flying_wing":
        target = 0.52 * span
        nose = max(0.25 * target, 0.80 * mac)
        tail_arm = 0.0
        tail_chord = 0.0
        tail_margin = 0.15 * root
        length = max(target, nose + root + tail_margin)
        wing_x_le = nose
        x_ac = wing_x_le + 0.25 * mac
        xcg = wing_x_le + 0.27 * mac
        np_target = xcg + sm * mac
        tail_start = 0.72
        rationale = (f"飞翼中央体：长度 0.52·b（经验范围）={target:.2f} m；"
                     f"机头 {nose:.2f} m、根弦 {root:.2f} m、尾端余量 {tail_margin:.2f} m；"
                     f"无独立尾翼")
    else:
        ratio = 0.82
        if config == "vtail":
            ratio = 0.84
        if altitude >= 5000.0:
            ratio = 0.78
        target = ratio * span
        # 机头长度按 22~30% 机身经验；先给初值再迭代。
        nose = max(0.27 * target, 0.90 * mac)
        tail_arm = _clamp(2.6 * mac + 0.10 * span, 0.7, 8.0)
        if config == "vtail":
            tail_arm *= 1.05
        if engine not in ("electric", "") or speed >= 90.0:
            tail_arm *= 1.10
        if altitude >= 5000.0:
            tail_arm *= 1.05
        sh = vh * area * mac / max(tail_arm, 0.1)
        tail_chord = _clamp((sh / 4.0) ** 0.5, 0.08, 1.5)
        tail_margin = _clamp(0.12 * root, 0.04, 0.4)
        length = max(target, nose + root + tail_arm + tail_chord + tail_margin)
        # 迭代：总长按经验值/布局取大者；机头占 22%~30% L。
        for _ in range(3):
            nose = _clamp(max(0.27 * length, 0.90 * mac),
                          0.24 * length, 0.32 * length)
            min_tail_arm = 2.2 * mac
            tail_arm = max(min_tail_arm,
                           length - nose - root - tail_chord - tail_margin)
            layout = nose + root + tail_arm + tail_chord + tail_margin
            if layout > length:
                length = layout
        wing_x_le = nose
        x_ac = wing_x_le + 0.25 * mac
        xcg = wing_x_le + 0.27 * mac
        np_target = xcg + sm * mac
        tail_le = wing_x_le + root + tail_arm
        tail_ac = tail_le + 0.25 * tail_chord
        # 目标焦点检查：若尾翼气动中心不足，优先延长尾段而不是缩短机头。
        if tail_ac < np_target + 0.12 * mac:
            extra = np_target + 0.12 * mac - tail_ac
            tail_arm += extra
            length += extra
        tail_start = _clamp(0.72, 0.55, 0.85)
        rationale = (f"机身长度目标 {target:.2f} m（0.82·b 经验，较常规略长）；实际 {length:.2f} m；"
                     f"机头 {nose:.2f} m（{100*nose/length:.0f}% L，约 27%）；"
                     f"根弦 {root:.2f} m；尾臂 {tail_arm:.2f} m；"
                     f"尾翼弦 {tail_chord:.2f} m；重心 x≈{xcg:.2f} m，目标焦点 x≈{np_target:.2f} m")

    if explicit_length:
        length = _clamp(float(explicit_length) *
                        max(0.5, float(getattr(spec, "fuselage_length_scale", 1.0) or 1.0)),
                        0.6, 20.0)
        if config != "flying_wing":
            nose = _clamp(nose, 0.20 * length, 0.30 * length)
            wing_x_le = nose
        rationale += f"；显式长度 {length:.2f} m"

    radius, radius_reason = _radius_from_conditions(spec, length)
    stations = _make_profile(length, wing_x_le, 0.72)
    rationales = rationale + "；" + radius_reason
    return {
        "style": STYLE_ID,
        "style_label": STYLE_LABEL,
        "rationale": rationales,
        "length_m": round(length, 4),
        "max_diameter_m": round(2.0 * radius, 4),
        "radius_m": round(radius, 4),
        "width_ratio": 1.0,
        "section": "ellipse",
        "section_exponent": 2.0,
        "end_caps": "round",
        "stations": stations,
        "length_diameter_ratio": round(length / max(2.0 * radius, 1e-6), 2),
        "tail_arm_m": round(tail_arm, 4),
        "tail_chord_m": round(tail_chord, 4),
        "nose_m": round(wing_x_le, 4),
        "wing_x_le_m": round(wing_x_le, 4),
        "xcg_m": round(xcg, 4),
        "np_target_m": round(np_target, 4),
    }


def radius_at_x(fuselage: dict[str, Any], x_m: float) -> float:
    """按站位线性插值某 x 处的圆截面半径（m）。"""
    try:
        length = max(float(fuselage.get("length_m", 0.0)), 1e-6)
        radius = float(fuselage.get("radius_m") or
                       (float(fuselage.get("max_diameter_m", 0.0)) / 2.0))
        stations = fuselage.get("stations") or []
        if not stations:
            return radius
        norm = max(0.0, min(float(x_m) / length, 1.0))
        prev = stations[0]
        for cur in stations:
            x0, f0 = float(prev[0]), float(prev[1])
            x1, f1 = float(cur[0]), float(cur[1])
            if norm <= x1 or cur is stations[-1]:
                if x1 <= x0:
                    return radius * f1
                t = max(0.0, min((norm - x0) / (x1 - x0), 1.0))
                return radius * (f0 + (f1 - f0) * t)
            prev = cur
    except Exception:
        pass
    return float(fuselage.get("radius_m") or 0.0)


__all__ = ["STYLE_ID", "STYLE_LABEL", "STYLE_DESC", "list_styles",
           "build_fuselage_rules", "radius_at_x"]

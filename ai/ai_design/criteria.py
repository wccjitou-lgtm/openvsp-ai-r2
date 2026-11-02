# -*- coding: utf-8 -*-
"""概念设计阶段的机翼/尾翼经验准则、飞行品质与操纵性估算。

本模块不替代 CFD/风洞，只用总体设计常用经验公式给出约束检查，
并把数值和结论写入设计报告。动态特性采用 VSPAERO 稳定性导数
做简化二自由度估算，标注为“工程估算”。
"""
from __future__ import annotations

import math
from typing import Any

GRAVITY = 9.80665


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _status(value: float | None, low: float, high: float,
            hard: bool = False) -> tuple[str, str]:
    if value is None:
        return "unknown", "缺少数据"
    if low <= value <= high:
        return "pass", f"在 {low:.3g}~{high:.3g} 内"
    if value < low:
        return ("fail" if hard else "warn"), f"低于下限 {low:.3g}"
    return ("fail" if hard else "warn"), f"高于上限 {high:.3g}"


def _row(rows: list[list[Any]], name: str, value: Any, unit: str = "",
         limit: str = "", status: str = "") -> None:
    rows.append([name, value, unit, limit, status])


def _check(checks: list[dict[str, Any]], ident: str, name: str,
           value: float | None, low: float, high: float, unit: str,
           hard: bool = False, note: str = "") -> None:
    status, reason = _status(value, low, high, hard=hard)
    checks.append({
        "id": ident, "name": name, "value": value, "low": low,
        "high": high, "unit": unit, "status": status,
        "note": (reason + ("；" + note if note else "")),
    })


def _mass_inertia(spec: Any, geometry: dict[str, Any]) -> dict[str, float]:
    """简化惯量估算（概念级，用于飞行品质数量级判断）。"""
    mass = max(float(getattr(spec, "mtow_kg", 20.0) or 20.0), 0.1)
    wing = _as_dict(geometry.get("wing"))
    fuselage = _as_dict(geometry.get("fuselage"))
    span = max(float(wing.get("span_m", 1.0) or 1.0), 0.2)
    root = max(float(wing.get("root_chord_m", 0.2) or 0.2), 0.05)
    length = max(float(fuselage.get("length_m", root * 4.0) or root * 4.0), root)
    return {
        "mass_kg": mass,
        "ixx": 0.18 * mass * (span / 2.0) ** 2,
        "iyy": 0.22 * mass * (length / 2.0) ** 2,
        "izz": 0.25 * mass * (span / 2.0) ** 2,
    }


def _eigen_2x2(a11: float, a12: float, a21: float, a22: float) -> dict[str, Any]:
    trace = a11 + a22
    det = a11 * a22 - a12 * a21
    disc = trace * trace - 4.0 * det
    if disc >= 0.0:
        r1 = 0.5 * (trace + math.sqrt(disc))
        r2 = 0.5 * (trace - math.sqrt(disc))
        return {"stable": r1 < 0 and r2 < 0, "real": True,
                "roots": [round(r1, 4), round(r2, 4)]}
    wn = math.sqrt(max(det, 0.0))
    zeta = (-trace / (2.0 * wn)) if wn > 1e-9 else 0.0
    return {"stable": trace < 0 and det > 0, "real": False,
            "omega_n": round(wn, 4), "zeta": round(zeta, 4)}


def evaluate_design(spec: Any, geometry: dict[str, Any],
                    metrics: dict[str, Any]) -> dict[str, Any]:
    """返回机翼/尾翼/稳定性/飞行品质/操纵性准则检查。"""
    geometry = _as_dict(geometry)
    metrics = _as_dict(metrics)
    wing = _as_dict(geometry.get("wing"))
    htail = _as_dict(geometry.get("htail"))
    vtail = _as_dict(geometry.get("vtail")) or _as_dict(geometry.get("vtail_conventional"))
    vtail_right = _as_dict(geometry.get("vtail_right"))
    fuselage = _as_dict(geometry.get("fuselage"))
    sizing = _as_dict(geometry.get("sizing"))
    design = _as_dict(metrics.get("design"))
    aero = _as_dict(metrics.get("aero"))
    stability = _as_dict(metrics.get("stability"))
    derivatives = _as_dict(stability.get("derivatives"))
    flow = _as_dict(metrics.get("flow"))
    mass = _mass_inertia(spec, geometry)
    checks: list[dict[str, Any]] = []

    wing_rows: list[list[Any]] = []
    area = _num(sizing.get("area_m2")) or _num(wing.get("area_m2")) or 0.0
    span = _num(wing.get("span_m")) or 0.0
    ar = _num(sizing.get("aspect_ratio"))
    taper = _num(wing.get("taper_ratio"))
    if taper is None and wing:
        root = _num(wing.get("root_chord_m")); tip = _num(wing.get("tip_chord_m"))
        taper = (tip / root) if root else None
    sweep = _num(wing.get("sweep_deg")) or 0.0
    dihedral = _num(wing.get("dihedral_deg")) or 0.0
    mac = _num(sizing.get("mac_m")) or _num(wing.get("root_chord_m")) or 0.2
    mtow = max(float(getattr(spec, "mtow_kg", 20.0) or 20.0), 0.1)
    wing_loading = mtow / area if area > 0 else None
    _row(wing_rows, "机翼面积", round(area, 4) if area else None, "m²")
    _row(wing_rows, "翼展", round(span, 4) if span else None, "m")
    _row(wing_rows, "展弦比 AR", round(ar, 3) if ar else None, "—")
    _row(wing_rows, "梢根比 λ", round(taper, 3) if taper else None, "—")
    _row(wing_rows, "机翼后掠角", round(sweep, 2), "°")
    _row(wing_rows, "上反角", round(dihedral, 2), "°")
    _row(wing_rows, "翼载 W/S", round(wing_loading, 2) if wing_loading else None, "kg/m²")
    _check(checks, "wing_loading", "翼载", wing_loading, 15.0, 90.0, "kg/m²")
    _check(checks, "wing_ar", "展弦比", ar, 4.0, 20.0, "—")
    _check(checks, "wing_taper", "梢根比", taper, 0.35, 0.70, "—")
    _check(checks, "wing_dihedral", "上反角", dihedral, -2.0, 6.0, "°")

    # 失速速度：显式失速上限优先；缺省时沿用最小速度/裕度
    rho = _num(flow.get("density_kgm3")) or 1.225
    clmax = (_num(aero.get("cl_max_in_scan")) or _num(aero.get("cl_max"))
             or _num(design.get("clmax")) or float(getattr(spec, "clmax", 1.2) or 1.2))
    stall_speed = math.sqrt(2.0 * mtow * GRAVITY / max(rho * area * clmax, 1e-9)) if area > 0 else None
    explicit_stall = _num(getattr(spec, "stall_speed_max_ms", None))
    stall_limit = explicit_stall or (float(getattr(spec, "speed_min_ms", 30) or 30) /
                                     max(float(getattr(spec, "stall_margin", 1.15) or 1.15), 1.0))
    if stall_speed:
        _row(wing_rows, "失速速度估算", round(stall_speed, 2), "m/s",
             f"≤{stall_limit:.2f}" + ("（显式）" if explicit_stall else "（默认裕度）"))
        _check(checks, "stall_speed", "失速速度", stall_speed, 0.0, stall_limit, "m/s")

    # 尾翼容量
    tail_rows: list[list[Any]] = []
    lh = lv = None
    vh_val = vv_val = None
    if htail:
        sh = _num(htail.get("area_m2")) or 0.5 * (
            (_num(htail.get("root_chord_m")) or 0) + (_num(htail.get("tip_chord_m")) or 0)) * (
            _num(htail.get("span_m")) or 0)
        lh = max((_num(htail.get("x_le_m")) or 0.0) + 0.25 * (_num(htail.get("root_chord_m")) or 0.0)
                 - ((_num(wing.get("x_le_m")) or 0.0) + 0.25 * mac), 0.05)
        if area > 0 and mac > 0:
            vh_val = sh * lh / (area * mac)
        _row(tail_rows, "平尾面积", round(sh, 4), "m²")
        _row(tail_rows, "平尾容量系数 Vh", round(vh_val, 4) if vh_val else None, "—",
             f"{float(getattr(spec, 'tail_volume_h', 0.5) or 0.5):.2f}")
    if vtail:
        sv = _num(vtail.get("area_m2"))
        if sv is None:
            sv = 0.5 * ((_num(vtail.get("root_chord_m")) or 0) + (_num(vtail.get("tip_chord_m")) or 0)) * (
                _num(vtail.get("span_m")) or _num(vtail.get("panel_span_m")) or 0)
            if vtail.get("panel_span_m"):
                sv *= 2.0
        lv = max((_num(vtail.get("x_le_m")) or 0.0) + 0.25 * (_num(vtail.get("root_chord_m")) or 0.0)
                 - ((_num(wing.get("x_le_m")) or 0.0) + 0.25 * mac), 0.05)
        if area > 0 and span > 0:
            vv_val = sv * lv / (area * span)
        _row(tail_rows, "垂尾面积", round(sv, 4), "m²")
        _row(tail_rows, "垂尾容量系数 Vv", round(vv_val, 4) if vv_val else None, "—",
             f"{float(getattr(spec, 'tail_volume_v', 0.035) or 0.035):.3f}")
    if vh_val is not None:
        _check(checks, "vtail_h", "平尾容量 Vh", vh_val,
               0.35, 0.85, "—", note="常用 0.4~0.8")
    if vv_val is not None:
        _check(checks, "vtail_v", "垂尾容量 Vv", vv_val,
               0.025, 0.060, "—", note="常用 0.03~0.05")
    if htail:
        htail_area = (_num(htail.get("area_m2")) or
                      _num(htail.get("full_area_m2")) or
                      0.5 * ((_num(htail.get("root_chord_m")) or 0) +
                             (_num(htail.get("tip_chord_m")) or 0)) *
                      (_num(htail.get("span_m")) or 0))
        h_ar = ((_num(htail.get("span_m")) or 0) ** 2 /
                max(htail_area, 1e-9))
        _row(tail_rows, "平尾展弦比", round(h_ar, 2), "—")
        _check(checks, "htail_ar", "平尾展弦比", h_ar, 2.5, 6.0, "—")
    if vtail:
        v_span = _num(vtail.get("span_m")) or 0.0
        v_area = _num(vtail.get("area_m2")) or 0.0
        if v_area:
            _check(checks, "vtail_ar", "垂尾展弦比", v_span ** 2 / v_area, 1.2, 3.5, "—")

    # 静稳定性
    sm_rows: list[list[Any]] = []
    sm = _num(stability.get("static_margin"))
    sm_target = _num(design.get("static_margin")) or float(getattr(spec, "static_margin", 0.1) or 0.1)
    _row(sm_rows, "静稳定裕度 SM", round(sm * 100.0, 2) if sm is not None else None, "% MAC",
         f"目标 {sm_target*100:.1f}%")
    _check(checks, "static_margin", "静稳定裕度", sm, 0.05, 0.20, "—",
           note=f"目标 {sm_target*100:.0f}% MAC")
    cma = _num(derivatives.get("cm_alpha_per_rad"))
    cla = _num(derivatives.get("cl_alpha_per_rad"))
    cnb = _num(derivatives.get("cn_beta_per_rad"))
    cyb = _num(derivatives.get("cy_beta_per_rad"))
    clb = _num(derivatives.get("cl_beta_per_rad"))
    if cla is not None: _row(sm_rows, "CLα", round(cla, 3), "1/rad")
    if cma is not None: _row(sm_rows, "CMα", round(cma, 4), "1/rad")
    if cnb is not None: _row(sm_rows, "Cnβ", round(cnb, 4), "1/rad")
    if cyb is not None: _row(sm_rows, "Cyβ", round(cyb, 4), "1/rad")
    if clb is not None: _row(sm_rows, "Clβ", round(clb, 4), "1/rad")
    if cma is not None:
        _check(checks, "cma", "纵向静稳定 CMα<0", cma, -10.0, 0.0, "1/rad")
    if cnb is not None:
        _check(checks, "cnb", "航向静稳定 Cnβ>0", cnb, 0.02, 0.20, "1/rad")
    if cyb is not None:
        _check(checks, "cyb", "侧力导数 Cyβ<0", cyb, -1.0, -0.01, "1/rad")
    if clb is not None:
        _check(checks, "clb", "横向静稳定 Clβ<0", clb, -0.5, -0.005, "1/rad")

    # 飞行动力学近似
    fq_rows: list[list[Any]] = []
    dynamic: dict[str, Any] = {}
    qbar = 0.5 * rho * (float(flow.get("velocity_ms") or 30.0) ** 2)
    velocity = float(flow.get("velocity_ms") or 30.0)
    iyy = mass["iyy"]; izz = mass["izz"]; ixx = mass["ixx"]
    cmq = _num(derivatives.get("cm_q"))
    clq = _num(derivatives.get("cl_q"))
    if cma is not None and cla is not None:
        a11 = -qbar * area * cla / max(mass["mass_kg"] * velocity, 1e-9)
        a12 = 1.0
        a21 = qbar * area * mac * cma / max(iyy, 1e-9)
        a22 = (qbar * area * mac * mac * (cmq or 0.0) /
               max(2.0 * velocity * iyy, 1e-9))
        sp = _eigen_2x2(a11, a12, a21, a22)
        dynamic["short_period"] = sp
        _row(fq_rows, "短周期 ωn", sp.get("omega_n"), "rad/s", "0.5~10")
        _row(fq_rows, "短周期 ζ", sp.get("zeta"), "—", "0.3~2.0")
        _check(checks, "sp_wn", "短周期频率", sp.get("omega_n"), 0.5, 10.0, "rad/s")
        _check(checks, "sp_zeta", "短周期阻尼", sp.get("zeta"), 0.25, 2.0, "—")
    wn_ph = math.sqrt(2.0) * GRAVITY / max(velocity, 0.1)
    cl_cruise = _num(metrics.get("cruise", {}).get("cl_cruise")) or 0.5
    cd_cruise = _num(metrics.get("cruise", {}).get("cd_cruise")) or 0.04
    zeta_ph = (cd_cruise / max(cl_cruise, 1e-6)) / math.sqrt(2.0)
    dynamic["phugoid"] = {"omega_n": round(wn_ph, 3), "zeta": round(zeta_ph, 3)}
    _row(fq_rows, "长周期 ωn", round(wn_ph, 3), "rad/s", "0.1~1.5")
    _row(fq_rows, "长周期 ζ", round(zeta_ph, 3), "—", "0.02~0.3")
    _check(checks, "ph_wn", "长周期频率", wn_ph, 0.1, 1.5, "rad/s")
    _check(checks, "ph_zeta", "长周期阻尼", zeta_ph, 0.02, 0.30, "—")
    cnr = _num(derivatives.get("cn_r"))
    if cnb is not None:
        n_beta = qbar * area * span * cnb / max(izz, 1e-9)
        wn_dr = math.sqrt(max(n_beta, 0.0))
        nr = (qbar * area * span * span * (cnr or 0.0) /
              max(2.0 * velocity * izz, 1e-9))
        y_beta = (qbar * area * (cyb or 0.0) /
                  max(mass["mass_kg"] * velocity, 1e-9))
        zeta_dr = -((nr + y_beta) / (2.0 * wn_dr)) if wn_dr > 1e-6 else None
        dynamic["dutch_roll"] = {"omega_n": round(wn_dr, 3),
                                 "zeta": round(zeta_dr, 3) if zeta_dr is not None else None}
        _row(fq_rows, "荷兰滚 ωn", round(wn_dr, 3), "rad/s", "0.2~3")
        _row(fq_rows, "荷兰滚 ζ", round(zeta_dr, 3) if zeta_dr is not None else None, "—", ">0.05")
        _check(checks, "dr_wn", "荷兰滚频率", wn_dr, 0.2, 3.0, "rad/s")
        _check(checks, "dr_zeta", "荷兰滚阻尼", zeta_dr, 0.05, 1.0, "—")
    clp = _num(derivatives.get("cl_p"))
    if clp is not None and clp < -1e-9:
        qsb = qbar * area * span * span
        denom = qsb * clp / max(2.0 * velocity, 1e-9)
        if denom < -1e-9:
            tau_roll = -ixx / denom
            dynamic["roll_mode_tau_s"] = round(tau_roll, 3)
            _row(fq_rows, "滚转模态时间常数", round(tau_roll, 3), "s", "<1.5")
            _check(checks, "roll_tau", "滚转模态时间常数", tau_roll, 0.0, 1.5, "s")
    spiral_ok = None
    if clb is not None and clb < 0 and cnb is not None and cnb > 0:
        spiral_ok = "稳定趋势"
    elif clb is not None or cnb is not None:
        spiral_ok = "需复核"
    dynamic["spiral"] = spiral_ok
    _row(fq_rows, "螺旋稳定性趋势", spiral_ok, "—", "Clβ<0, Cnβ>0")

    # 操纵性估算
    ctl_rows: list[list[Any]] = []
    if vh_val is not None:
        elevator_power = 1.4 * vh_val
        _row(ctl_rows, "升降舵操纵功率估算", round(elevator_power, 3), "1/rad", ">0.3")
        _check(checks, "elevator_power", "升降舵操纵功率", elevator_power, 0.3, 5.0, "1/rad")
    if vv_val is not None:
        rudder_power = 1.2 * vv_val
        _row(ctl_rows, "方向舵操纵功率估算", round(rudder_power, 3), "1/rad", ">0.02")
        _check(checks, "rudder_power", "方向舵操纵功率", rudder_power, 0.02, 1.0, "1/rad")
    if span > 0 and area > 0:
        aileron_power = 0.05 * (span / max(mac, 1e-6)) * 0.25
        _row(ctl_rows, "副翼滚转功率估算", round(aileron_power, 3), "1/rad", ">0.02")
        _check(checks, "aileron_power", "副翼滚转功率", aileron_power, 0.02, 1.5, "1/rad")
    trim_margin = None
    if cma is not None and cla is not None:
        trim_margin = abs(cla / cma) if abs(cma) > 1e-9 else None
        _row(ctl_rows, "俯仰配平能力指标", round(trim_margin, 2) if trim_margin else None, "—", ">5")
        _check(checks, "pitch_trim", "俯仰配平能力", trim_margin, 5.0, 200.0, "—")

    all_status = [c["status"] for c in checks]
    fail_count = all_status.count("fail")
    warn_count = all_status.count("warn")
    overall = "fail" if fail_count else ("warn" if warn_count else "pass")
    return {
        "available": bool(checks),
        "wing": {"rows": wing_rows},
        "tail": {"rows": tail_rows},
        "stability": {"rows": sm_rows},
        "flying_qualities": {"rows": fq_rows, "dynamic": dynamic},
        "controllability": {"rows": ctl_rows},
        "checks": checks,
        "summary": {"pass": all_status.count("pass"), "warn": warn_count,
                    "fail": fail_count, "total": len(checks)},
        "overall": overall,
        "inertia_estimate": {k: round(v, 3) for k, v in mass.items()},
        "note": "动态与操纵性为简化二自由度/工程估算，需用专项工具或试飞复核。",
    }

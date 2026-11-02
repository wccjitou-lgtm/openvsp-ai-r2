"""设计引擎：自然语言需求 → 结构化 spec → 参数 sizing → 几何 → VSPAERO → 指标 → 优化。

offline 模式必须完全离线可运行：使用规则解析需求 + 解析 sizing + OpenVSP/VSPAERO，
不依赖任何网络服务。
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import vsp_bridge
from .config import AIConfig
from .logging_util import Logger
from .vsp_bridge import (
    GRAVITY,
    VSPBridge,
    VSPError,
    derive_stability_derivatives,
    standard_atmosphere,
    summarize_case_parameters,
    summarize_polar,
)

# --------------------------------------------------------------------------- #
# 设计需求
# --------------------------------------------------------------------------- #
_CONFIG_KEYWORDS = (
    ("flying_wing", ("飞翼", "无尾", "flying wing", "flying-wing", "翼身融合")),
    ("canard", ("鸭翼", "鸭式", "canard")),
    ("tandem", ("串列翼", "串列", "tandem")),
    ("vtail", ("v 尾", "v尾", "v-tail", "v tail", "v 型尾", "v型尾", "v尾翼")),
    ("conventional", ("常规尾", "常规布局", "平尾", "水平尾翼", "t 尾", "t尾", "双垂尾", "双立尾", "conventional")),
)
_TWIN_VTAIL_KEYWORDS = (
    "双垂尾", "双垂直尾翼", "双立尾", "两片垂尾", "双垂",
    "twin vertical tail", "twin vtail", "two vertical tails",
)
_ENGINE_KEYWORDS = (
    ("electric", ("电动", "电机", "锂电池", "electric", "e-vtol", "电池")),
    ("EJ115", ("ej115",)),
    ("TWP50", ("twp50",)),
    ("turboprop", ("涡桨", "turboprop")),
    ("turbojet", ("涡喷", "turbojet")),
)


@dataclass
class DesignSpec:
    """结构化设计需求（可由规则解析或 LLM 辅助填充）。"""

    name: str = "AI 设计的无人机"
    mtow_kg: float = 50.0
    altitude_m: float = 2000.0
    speed_min_ms: float = 25.0
    speed_max_ms: float = 45.0
    speed_cruise_ms: float = 35.0
    payload_kg: float | None = None
    configuration: str = "conventional"       # conventional | vtail | flying_wing
    engine: str = "electric"
    wing_area_m2: float | None = None
    aspect_ratio: float | None = None
    taper_ratio: float = 0.60
    sweep_deg: float = 0.0
    dihedral_deg: float = 2.0
    airfoil_root: str = "NACA4412"
    airfoil_tip: str | None = None
    clmax: float = 1.30
    static_margin: float = 0.10
    stall_margin: float = 1.15               # 最小速度/失速速度
    stall_speed_max_ms: float | None = None  # 显式失速上限，独立于最小速度
    min_operating_speed_ms: float | None = None
    airfoil_locked: bool = False
    requirement_set: dict[str, Any] | None = None
    tail_dihedral_deg: float = 40.0
    tail_volume_h: float = 0.50
    tail_volume_v: float = 0.035
    max_span_m: float | None = None
    fuselage_length_m: float | None = None
    fuselage_diameter_m: float | None = None
    #: 机身规则显式覆盖（None=按构型/工况自动选择）
    fuselage_style: str | None = None
    #: 案例基线：机身长度缩放（parse 到“机身加长 X%”时设置）
    fuselage_length_scale: float = 1.0
    #: 按机身局部宽度外移机翼/平尾根部，并裁剪埋入部分（仅气动分析面）
    trim_roots_to_fuselage: bool = True
    #: 静稳定多轮优化参数
    canard_area_scale: float = 1.0
    flying_wing_vertical_tail: bool = False
    #: 案例基线：尾翼面积缩放（apply_case_baseline 按案例尾翼/机翼面积比设置）
    tail_scale_h: float = 1.0
    tail_scale_v: float = 1.0
    twin_vtail: bool = False
    max_ar: float = 16.0
    assumptions: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    raw_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {
            "name": self.name,
            "mtow_kg": round(self.mtow_kg, 3),
            "altitude_m": round(self.altitude_m, 1),
            "speed_min_ms": round(self.speed_min_ms, 2),
            "speed_max_ms": round(self.speed_max_ms, 2),
            "speed_cruise_ms": round(self.speed_cruise_ms, 2),
            "payload_kg": self.payload_kg,
            "configuration": self.configuration,
            "configuration_label": CONFIG_LABELS.get(self.configuration, self.configuration),
            "engine": self.engine,
            "wing_area_m2": self.wing_area_m2,
            "aspect_ratio": self.aspect_ratio,
            "taper_ratio": self.taper_ratio,
            "sweep_deg": self.sweep_deg,
            "dihedral_deg": self.dihedral_deg,
            "airfoil_root": self.airfoil_root,
            "clmax": self.clmax,
            "static_margin": self.static_margin,
            "tail_dihedral_deg": self.tail_dihedral_deg,
            "tail_volume_h": self.tail_volume_h,
            "tail_volume_v": self.tail_volume_v,
            "twin_vtail": self.twin_vtail,
            "max_span_m": self.max_span_m,
            "stall_speed_max_ms": self.stall_speed_max_ms,
            "min_operating_speed_ms": self.min_operating_speed_ms,
            "airfoil_locked": self.airfoil_locked,
            "trim_roots_to_fuselage": self.trim_roots_to_fuselage,
            "canard_area_scale": self.canard_area_scale,
            "flying_wing_vertical_tail": self.flying_wing_vertical_tail,
            "requirement_set": self.requirement_set,
            "assumptions": list(self.assumptions),
            "raw_prompt": self.raw_prompt,
        }
        return data


CONFIG_LABELS = {
    "conventional": "常规尾翼布局",
    "canard": "鸭翼布局",
    "tandem": "串列翼布局",
    "vtail": "V 尾布局",
    "flying_wing": "飞翼布局",
}
ENGINE_LABELS = {
    "electric": "电动（锂电池 + 螺旋桨）",
    "EJ115": "EJ115 涡喷发动机",
    "TWP50": "TWP50 涡桨发动机",
    "turboprop": "涡桨发动机",
    "turbojet": "涡喷发动机",
}


def _find_first(pattern: str, text: str, flags: int = re.IGNORECASE) -> re.Match | None:
    return re.search(pattern, text, flags)


def _float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_requirements(prompt: str) -> DesignSpec:
    """规则解析自然语言设计需求（中英文混合）。"""
    text = (prompt or "").strip()
    lowered = text.lower()
    spec = DesignSpec(raw_prompt=text)
    found: dict[str, str] = {}

    def set_value(attr: str, value: Any, source: str) -> None:
        setattr(spec, attr, value)
        found[attr] = source

    # ---- 配置形式 --------------------------------------------------------
    configuration = "conventional"
    for config_name, keywords in _CONFIG_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            configuration = config_name
            found["configuration"] = f"关键词「{keywords[0]}」"
            break
    set_value("configuration", configuration, found.get("configuration", "默认常规布局"))

    # 平尾+双垂尾：配置仍是常规布局，但垂尾数量是独立选择。
    lower_text = text.lower()
    if any(keyword in lower_text for keyword in _TWIN_VTAIL_KEYWORDS):
        spec.twin_vtail = True
        found["twin_vtail"] = "双垂尾关键词"
        spec.assumptions.append("识别到双垂尾需求，将生成左右两片垂直尾翼")

    # ---- 发动机 ----------------------------------------------------------
    engine = "electric"
    engine_found = False
    for engine_name, keywords in _ENGINE_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            engine = engine_name
            found["engine"] = f"关键词「{keywords[0]}」"
            engine_found = True
            break
    set_value("engine", engine, found.get("engine", "默认电动动力"))
    if not engine_found:
        spec.assumptions.append("未明确动力形式，按电动无人机估算")

    # ---- 起飞重量 --------------------------------------------------------
    mtow = None
    for pattern in (
        r"(?:最大)?起飞重量[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)",
        r"mtow[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)",
        r"(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)[^。；,，\n]{0,10}(?:级|无人机|uav|飞机)",
        r"全重[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)",
    ):
        match = _find_first(pattern, text)
        if match:
            mtow = _float(match.group(1))
            found["mtow_kg"] = f"匹配「{match.group(0)}」"
            break
    if mtow is None:
        match = _find_first(r"(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)", text)
        if match:
            mtow = _float(match.group(1))
            found["mtow_kg"] = f"首个重量「{match.group(0)}」"
    if mtow and mtow > 0:
        set_value("mtow_kg", mtow, found.get("mtow_kg", ""))
    else:
        spec.assumptions.append("未识别起飞重量，按 50 kg 级电动无人机估算")

    # ---- 高度 ------------------------------------------------------------
    altitude = None
    for pattern in (
        r"(?:巡航|飞行|工作|设计)?(?:海拔|高度)[^0-9]{0,10}(\d{2,5}(?:\.\d+)?)\s*(?:m|米|km|千米)",
        r"(\d{2,5}(?:\.\d+)?)\s*(?:m|米)\s*(?:海拔|高度)",
    ):
        match = _find_first(pattern, text)
        if match:
            altitude = _float(match.group(1))
            raw = match.group(0).lower()
            if "km" in raw or "千米" in raw:
                altitude = (altitude or 0) * 1000.0
            found["altitude_m"] = f"匹配「{match.group(0)}」"
            break
    if altitude:
        set_value("altitude_m", altitude, found.get("altitude_m", ""))
    else:
        spec.assumptions.append("未识别飞行高度，按 2000 m 估算")

    # ---- 速度范围 --------------------------------------------------------
    speed_min = speed_max = None
    match = _find_first(
        r"(\d+(?:\.\d+)?)\s*(?:[-~～—至到]|到)\s*(\d+(?:\.\d+)?)\s*(?:m/s|米/秒|mps)",
        text)
    if match:
        speed_min, speed_max = _float(match.group(1)), _float(match.group(2))
        found["speed_range"] = f"匹配「{match.group(0)}」"
    if speed_min and speed_max and speed_min > 0 and speed_max > speed_min:
        set_value("speed_min_ms", speed_min, found.get("speed_range", ""))
        set_value("speed_max_ms", speed_max, found.get("speed_range", ""))
    else:
        spec.assumptions.append("未识别速度范围，按 25~45 m/s 估算")
    match = _find_first(r"巡航速度[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:m/s|米/秒|mps)", text)
    if match and _float(match.group(1)):
        set_value("speed_cruise_ms", _float(match.group(1)), "巡航速度")
    else:
        spec.speed_cruise_ms = 0.6 * spec.speed_min_ms + 0.4 * spec.speed_max_ms
        spec.assumptions.append("未给出巡航速度，按速度范围 40% 位置估算")

    # ---- 翼展限制 --------------------------------------------------------
    match = _find_first(
        r"翼展[^0-9]{0,8}(?:不超过|不大于|小于等于|≤|最大)?\s*(\d+(?:\.\d+)?)\s*(?:m|米)", text)
    if match and _float(match.group(1)):
        set_value("max_span_m", _float(match.group(1)), f"匹配「{match.group(0)}」")

    # ---- 机翼面积 / 展弦比 ----------------------------------------------
    match = _find_first(r"(?:机翼|参考)?面积[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:m2|m²|平方米|平米)", text)
    if match and _float(match.group(1)):
        set_value("wing_area_m2", _float(match.group(1)), f"匹配「{match.group(0)}」")
    match = _find_first(r"展弦比[^0-9]{0,8}(\d+(?:\.\d+)?)", text)
    if match and _float(match.group(1)):
        set_value("aspect_ratio", _float(match.group(1)), f"匹配「{match.group(0)}」")

    # ---- 修改意图：后掠角 / 上反角 / 机身长度（配合案例基线使用） --------
    match = _find_first(
        r"后掠角[^0-9]{0,10}(?:改为|调整为|增加到|增大到|减小到|为)?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:°|度)", text)
    if match and _float(match.group(1)) is not None:
        set_value("sweep_deg", _float(match.group(1)), f"匹配「{match.group(0)}」")
    match = _find_first(
        r"(?:机翼)?上反角[^0-9]{0,10}(?:改为|调整为|增加到|增大到|减小到|为)?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:°|度)", text)
    if match and _float(match.group(1)) is not None:
        prefix = text[max(0, match.start() - 8):match.start()]
        if not re.search(r"[Vv]\s*尾", prefix):
            set_value("dihedral_deg", _float(match.group(1)),
                      f"匹配「{match.group(0)}」")
    match = _find_first(
        r"机身(?:长度|长)[^0-9]{0,10}(?:改为|调整为|增加到|加长到|为)?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:m|米)", text)
    if match and _float(match.group(1)):
        set_value("fuselage_length_m", _float(match.group(1)),
                  f"匹配「{match.group(0)}」")
    match = _find_first(r"机身(?:加长|延长|增加)\s*(\d+(?:\.\d+)?)\s*(?:%|％)", text)
    if match and _float(match.group(1)) is not None:
        set_value("fuselage_length_scale", 1.0 + _float(match.group(1)) / 100.0,
                  f"匹配「{match.group(0)}」")

    # ---- 翼型 ------------------------------------------------------------
    match = _find_first(r"(?:翼型[^A-Za-z0-9]{0,6})?(NACA\s*\d{4,5})", text)
    if match:
        airfoil = "NACA" + re.sub(r"\D", "", match.group(1))
        set_value("airfoil_root", airfoil, f"匹配「{match.group(0)}」")
    else:
        spec.airfoil_root = "NACA4412"
        spec.assumptions.append("未指定翼型，采用 NACA 4412")

    # ---- 静稳定裕度 ------------------------------------------------------
    match = _find_first(
        r"(?:静稳定|静稳定裕度|稳定裕度|static margin|sm)[^0-9]{0,10}(\d+(?:\.\d+)?)\s*%", text)
    if match and _float(match.group(1)):
        set_value("static_margin", _float(match.group(1)) / 100.0,
                  f"匹配「{match.group(0)}」")

    # ---- 尾容量 / 尾翼参数 ----------------------------------------------
    if spec.configuration == "vtail":
        match = _find_first(r"(?:V\s*尾|v\s*尾)[^0-9]{0,10}(?:上反角|倾角|dihedral)?[^0-9]{0,8}"
                            r"(\d{2})\s*(?:°|度)", text)
        if match and 20 <= float(match.group(1)) <= 65:
            set_value("tail_dihedral_deg", _float(match.group(1)), f"匹配「{match.group(0)}」")

    # ---- 载荷 ------------------------------------------------------------
    match = _find_first(r"(?:任务)?载荷[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)", text)
    if match and _float(match.group(1)):
        set_value("payload_kg", _float(match.group(1)), f"匹配「{match.group(0)}」")

    # ---- 名称 ------------------------------------------------------------
    if spec.configuration == "vtail" and spec.mtow_kg >= 100:
        spec.name = f"{spec.mtow_kg:.0f} kg 级 V 尾无人机"
    elif spec.engine == "electric":
        spec.name = f"{spec.mtow_kg:.0f} kg 级电动无人机"
    else:
        spec.name = f"{spec.mtow_kg:.0f} kg 级无人机"
    # ---- schema 2.0 类型化需求覆盖（保留显式约束） -------------------------
    try:
        from .requirements import parse_requirement_set
        reqset = parse_requirement_set(text)
        spec.requirement_set = reqset.to_dict()
        for item in reqset.constraints:
            q = item.quantity
            v = item.value_si
            if q == "mtow":
                spec.mtow_kg = float(v)
            elif q == "altitude":
                spec.altitude_m = float(v)  # 0 是合法输入
            elif q == "cruise_speed":
                spec.speed_cruise_ms = float(v)
            elif q == "minimum_speed":
                spec.speed_min_ms = float(v)
            elif q == "minimum_operating_speed":
                spec.min_operating_speed_ms = float(v)
            elif q == "maximum_speed":
                spec.speed_max_ms = float(v)
            elif q == "stall_speed_max":
                spec.stall_speed_max_ms = float(v)
            elif q == "payload_mass":
                spec.payload_kg = float(v)
            elif q == "span_max":
                spec.max_span_m = float(v)
            elif q == "wing_area":
                spec.wing_area_m2 = float(v)
            elif q == "aspect_ratio":
                spec.aspect_ratio = float(v)
            elif q == "wing_sweep":
                spec.sweep_deg = float(v)
            elif q == "wing_dihedral":
                spec.dihedral_deg = float(v)
        if reqset.explicit_airfoil:
            spec.airfoil_root = reqset.explicit_airfoil
            spec.airfoil_locked = True
        if reqset.conflicts:
            spec.assumptions.append("需求一致性冲突: " + "; ".join(
                str(c.get("message", c)) for c in reqset.conflicts))
        for item in reqset.missing_required_data:
            if item not in spec.assumptions:
                spec.assumptions.append(f"缺少必要输入: {item}")
        found["requirement_schema"] = "requirements.py/schema-2.0"
    except Exception as exc:
        spec.assumptions.append(f"类型化需求解析失败（保留旧解析）: {exc}")

    spec.sources = found
    return spec


# --------------------------------------------------------------------------- #
# 参数 sizing（解析法）
# --------------------------------------------------------------------------- #
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _tail_area(component: dict[str, Any] | None) -> float:
    if not component:
        return 0.0
    area = component.get("area_m2")
    if isinstance(area, (int, float)) and area > 0:
        return float(area)
    root = float(component.get("root_chord_m", 0.0) or 0.0)
    tip = float(component.get("tip_chord_m", 0.0) or 0.0)
    span = float(component.get("span_m", 0.0) or 0.0)
    return 0.5 * (root + tip) * span


def _vtail_area_total(geometry: dict[str, Any]) -> float:
    """平尾+单/双垂尾：返回全部垂尾面积（V 尾按总面积字段）。"""
    total = _tail_area(geometry.get("vtail"))
    total += _tail_area(geometry.get("vtail_conventional"))
    total += _tail_area(geometry.get("vtail_right"))
    return total


def _trim_root_panels(geometry: dict[str, Any], spec: Any,
                      clearance_m: float = 0.006) -> None:
    """按机身局部宽度把机翼/平尾根部外移，并记录暴露气动面积。

    完整几何参数（span/root_chord/area）保持不变，只增加 exposed_* 字段；
    OpenVSP 建模和 VSPAERO 使用暴露后的根位置与根弦。
    """
    fuselage = geometry.get("fuselage")
    if not fuselage or not getattr(spec, "trim_roots_to_fuselage", True):
        return
    try:
        from .fuselage_rules import radius_at_x
    except Exception:
        return

    def trim_one(component: dict[str, Any] | None, label: str) -> None:
        if not component:
            return
        span = float(component.get("span_m", 0.0))
        half = span / 2.0
        root_chord = float(component.get("root_chord_m", 0.0))
        tip_chord = float(component.get("tip_chord_m", 0.0))
        x_le = float(component.get("x_le_m", 0.0))
        z = float(component.get("z_m", 0.0))
        if span <= 0 or root_chord <= 0 or half <= 0:
            return
        radius = radius_at_x(fuselage, x_le + 0.25 * root_chord)
        root_y = min(float(radius) + clearance_m, 0.75 * half)
        if root_y <= 0.01:
            return
        exposed_half = max(half - root_y, 0.02)
        exposed_root = root_chord - (root_chord - tip_chord) * (root_y / half)
        exposed_area = 0.5 * (exposed_root + tip_chord) * (2.0 * exposed_half)
        full_area = 0.5 * (root_chord + tip_chord) * span
        sweep = math.radians(float(component.get("sweep_deg", 0.0) or 0.0))
        dihedral = math.radians(float(component.get("dihedral_deg", 0.0) or 0.0))
        exposed_x = x_le + math.tan(sweep) * root_y
        exposed_z = z + math.tan(dihedral) * root_y
        component.update({
            "root_y_m": round(root_y, 5),
            "exposed_root_chord_m": round(exposed_root, 5),
            "exposed_x_le_m": round(exposed_x, 5),
            "exposed_z_m": round(exposed_z, 5),
            "exposed_span_m": round(2.0 * exposed_half, 5),
            "exposed_area_m2": round(exposed_area, 5),
            "full_area_m2": round(full_area, 5),
            "full_aspect_ratio": round(span * span / full_area, 4) if full_area else None,
            "exposed_aspect_ratio": (round((2.0 * exposed_half) ** 2 / exposed_area, 4)
                                     if exposed_area else None),
            "trimmed_to_fuselage": True,
        })
        geometry.setdefault("component_trim", {})[label] = {
            "root_y_m": round(root_y, 5),
            "exposed_root_chord_m": round(exposed_root, 5),
            "full_area_m2": round(full_area, 5),
            "exposed_area_m2": round(exposed_area, 5),
            "full_span_m": round(span, 5),
            "exposed_span_m": round(2.0 * exposed_half, 5),
        }

    trim_one(geometry.get("wing"), "wing")
    trim_one(geometry.get("htail"), "htail")
    trim_one(geometry.get("canard"), "canard")
    trim_one(geometry.get("tandem_wing"), "tandem_wing")
    sizing = geometry.setdefault("sizing", {})
    wing = geometry.get("wing") or {}
    htail = geometry.get("htail") or {}
    if wing.get("trimmed_to_fuselage"):
        sizing["exposed_wing_area_m2"] = wing["exposed_area_m2"]
        sizing["exposed_wing_span_m"] = wing["exposed_span_m"]
        sizing["exposed_wing_root_chord_m"] = wing["exposed_root_chord_m"]
        sizing["full_wing_area_m2"] = wing["full_area_m2"]
    if htail.get("trimmed_to_fuselage"):
        sizing["exposed_htail_area_m2"] = htail["exposed_area_m2"]
        sizing["full_htail_area_m2"] = htail["full_area_m2"]
    canard = geometry.get("canard") or {}
    tandem = geometry.get("tandem_wing") or {}
    if canard.get("trimmed_to_fuselage"):
        sizing["exposed_canard_area_m2"] = canard["exposed_area_m2"]
        sizing["full_canard_area_m2"] = canard["full_area_m2"]
    if tandem.get("trimmed_to_fuselage"):
        sizing["exposed_tandem_area_m2"] = tandem["exposed_area_m2"]
        sizing["full_tandem_area_m2"] = tandem["full_area_m2"]
    if wing.get("trimmed_to_fuselage") or htail.get("trimmed_to_fuselage"):
        sizing["trim_roots_to_fuselage"] = True


def size_aircraft(spec: DesignSpec, optimization: dict[str, Any] | None = None) -> dict[str, Any]:
    """由需求解析出全机几何参数（SI），返回可直接交给 VSPBridge 的几何字典。

    ``optimization`` 可覆盖 aspect_ratio / wing_area_m2 / taper_ratio。
    """
    atmosphere = standard_atmosphere(spec.altitude_m)
    rho = atmosphere["density_kgm3"]
    weight = spec.mtow_kg * GRAVITY

    # ---- 机翼面积：由低速端失速约束确定 --------------------------------
    clmax_eff = max(spec.clmax * 0.92, 0.7)          # 3D / 配平修正
    explicit_stall = getattr(spec, "stall_speed_max_ms", None)
    v_stall = (float(explicit_stall) if explicit_stall else
               spec.speed_min_ms / max(spec.stall_margin, 1.0))
    area_required = 2.0 * weight / (rho * v_stall ** 2 * clmax_eff)
    area = float(spec.wing_area_m2) if spec.wing_area_m2 else area_required
    optimization = optimization or {}
    if optimization.get("wing_area_m2"):
        area = float(optimization["wing_area_m2"])
    area = _clamp(area, 0.25, 200.0)

    # ---- 展弦比 ----------------------------------------------------------
    if optimization.get("aspect_ratio"):
        ar = float(optimization["aspect_ratio"])
    elif spec.aspect_ratio:
        ar = float(spec.aspect_ratio)
    else:
        ar_default = 9.0 if spec.mtow_kg < 100 else 11.0
        if spec.configuration == "flying_wing":
            ar_default = 7.0
        ar = ar_default
    span = math.sqrt(max(ar, 1.0) * area)
    if spec.max_span_m:
        span = min(span, float(spec.max_span_m))
    ar = span ** 2 / area
    ar = _clamp(ar, 3.0, max(spec.max_ar, 3.0))

    taper = float(optimization.get("taper_ratio") or spec.taper_ratio)
    taper = _clamp(taper, 0.25, 0.9)
    sweep_deg = float(optimization.get("sweep_deg", spec.sweep_deg))
    dihedral_deg = float(optimization.get("dihedral_deg", spec.dihedral_deg))
    tail_scale = _clamp(float(optimization.get("tail_scale", 1.0) or 1.0), 0.3, 2.5)
    root_chord = 2.0 * area / (span * (1.0 + taper))
    tip_chord = root_chord * taper
    mac = (2.0 / 3.0) * root_chord * (1 + taper + taper ** 2) / (1 + taper)

    # ---- 机身 ------------------------------------------------------------
    from .fuselage_rules import build_fuselage_rules, radius_at_x
    fuselage_rules = build_fuselage_rules(spec, span, mac, root_chord, tip_chord)
    fuselage_length = float(fuselage_rules["length_m"])
    fuselage_diameter = float(fuselage_rules["max_diameter_m"])

    # ---- 纵向位置 --------------------------------------------------------
    x_wing_le = _clamp(float(fuselage_rules.get("wing_x_le_m", 0.28 * fuselage_length)),
                       0.10, max(0.10, fuselage_length - root_chord))
    if spec.configuration == "canard":
        # 鸭翼在前，主翼后移
        x_wing_le = _clamp(0.50 * fuselage_length, 0.15,
                           max(0.15, fuselage_length - root_chord))
    elif spec.configuration == "tandem":
        # 串列翼：前翼略靠前，后翼在尾段
        x_wing_le = _clamp(0.28 * fuselage_length, 0.10,
                           max(0.10, fuselage_length - root_chord))
    z_wing = -0.02 * fuselage_diameter
    tail_arm_guess = _clamp(float(fuselage_rules.get("tail_arm_m", 0.48 * fuselage_length + 0.25 * mac)),
                            0.55 * mac + 0.35, fuselage_length)
    x_tail_guess = x_wing_le + tail_arm_guess

    wing = {
        "name": "MainWing",
        "span_m": round(span, 4),
        "root_chord_m": round(root_chord, 4),
        "tip_chord_m": round(tip_chord, 4),
        "sweep_deg": float(sweep_deg),
        "dihedral_deg": float(dihedral_deg),
        "twist_deg": 0.0,
        "x_le_m": round(x_wing_le, 4),
        "z_m": round(z_wing, 4),
        "airfoil": spec.airfoil_root,
        "symmetry": True,
    }

    geometry: dict[str, Any] = {
        "configuration": spec.configuration,
        "fuselage": {
            "name": "Fuselage",
            "length_m": round(fuselage_length, 4),
            "max_diameter_m": round(fuselage_diameter, 4),
            "x_m": 0.0,
            "z_m": 0.0,
            "width_ratio": fuselage_rules["width_ratio"],
            "style": fuselage_rules["style"],
            "style_label": fuselage_rules["style_label"],
            "rationale": fuselage_rules["rationale"],
            "radius_m": fuselage_rules.get("radius_m"),
            "length_diameter_ratio": fuselage_rules["length_diameter_ratio"],
            "tail_arm_m": fuselage_rules.get("tail_arm_m"),
            "nose_m": fuselage_rules.get("nose_m"),
            "wing_x_le_m": fuselage_rules.get("wing_x_le_m"),
            "xcg_m": fuselage_rules.get("xcg_m"),
            "np_target_m": fuselage_rules.get("np_target_m"),
            "section": fuselage_rules.get("section", "ellipse"),
            "section_exponent": fuselage_rules.get("section_exponent", 2.0),
            "stations": fuselage_rules["stations"],
        },
        "wing": wing,
        "control_surfaces": [],
        "sizing": {
            "atmosphere": {key: round(value, 6) for key, value in atmosphere.items()},
            "weight_n": round(weight, 3),
            "area_m2": round(area, 4),
            "span_m": round(span, 4),
            "aspect_ratio": round(ar, 3),
            "mac_m": round(mac, 4),
            "wing_loading_kgm2": round(spec.mtow_kg / area, 3),
            "area_required_m2": round(area_required, 4),
            "clmax_effective": round(clmax_eff, 4),
            "stall_speed_ms": round(math.sqrt(2.0 * weight / (rho * area * clmax_eff)), 3),
            "tail_arm_guess_m": round(tail_arm_guess, 4),
        },
    }

    # 飞翼默认无独立机身；只有显式 fuselage_style 时才生成中央体。
    if spec.configuration == "flying_wing" and not getattr(spec, "fuselage_style", None):
        geometry["fuselage"] = None
        geometry["wing"]["x_le_m"] = 0.02
        geometry["sizing"]["fuselage_note"] = "飞翼构型无独立机身（翼身融合）"

    if spec.configuration == "flying_wing":
        geometry["control_surfaces"] = [
            {"component": "wing", "name": "ElevonL", "u_start": 0.45, "u_end": 0.95,
             "chord_fraction": 0.22},
            {"component": "wing", "name": "ElevonR", "u_start": -0.95, "u_end": -0.45,
             "chord_fraction": 0.22},
        ]
        geometry["sizing"]["tail_note"] = "飞翼布局无独立尾翼"
        geometry["sizing"]["aspect_ratio"] = round(ar, 3)
        geometry["sizing"]["xcg_initial_m"] = round(0.25 * mac, 4)
        if bool(getattr(spec, "flying_wing_vertical_tail", False)):
            vv = float(getattr(spec, "tail_volume_v", 0.035) or 0.035)
            tail_arm = max(0.55 * fuselage_length, 0.8 * mac)
            sv = max(vv * area * span / tail_arm, 0.005)
            v_span = math.sqrt(2.2 * sv)
            v_taper = 0.40
            v_root = 2.0 * sv / (v_span * (1.0 + v_taper))
            v_tip = v_root * v_taper
            x_v = max(x_wing_le + 0.55 * fuselage_length,
                      fuselage_length - v_root - 0.03 * fuselage_length)
            x_v = min(x_v, fuselage_length - v_root - 0.01 * fuselage_length)
            geometry["vtail_conventional"] = {
                "name": "WingFin",
                "span_m": round(v_span, 4),
                "root_chord_m": round(v_root, 4),
                "tip_chord_m": round(v_tip, 4),
                "x_le_m": round(x_v, 4),
                "z_m": round(0.5 * fuselage_diameter + 0.02, 4),
                "sweep_deg": 25.0,
                "airfoil": "NACA0012",
            }
            geometry["control_surfaces"].append(
                {"component": "vtail", "name": "Rudder", "u_start": 0.0,
                 "u_end": 1.0, "chord_fraction": 0.30})
            geometry["sizing"]["vtail_volume_target"] = round(vv, 5)
        return geometry

    # ---- 尾容量 → 尾翼尺寸 ----------------------------------------------
    tail_arm = _clamp(tail_arm_guess, 0.3 * fuselage_length, 0.95 * fuselage_length)
    vh = spec.tail_volume_h
    vv = spec.tail_volume_v
    htail_equivalent_area = vh * area * mac / tail_arm * tail_scale * spec.tail_scale_h
    vtail_equivalent_area = vv * area * span / tail_arm * tail_scale * spec.tail_scale_v
    tail_x_le_max = fuselage_length * 0.985

    if spec.configuration == "vtail":
        gamma = math.radians(_clamp(spec.tail_dihedral_deg, 20.0, 65.0))
        panel_area = max(
            htail_equivalent_area / (2.0 * max(math.cos(gamma), 0.2)),
            vtail_equivalent_area / (2.0 * max(math.sin(gamma), 0.2)),
        )
        panel_ar = 2.8
        panel_span = math.sqrt(panel_ar * panel_area)
        panel_taper = 0.50
        panel_root = 2.0 * panel_area / (panel_span * (1.0 + panel_taper))
        panel_tip = panel_root * panel_taper
        tail_root_chord = panel_root
        x_tail_le = max(x_wing_le + 0.45 * fuselage_length,
                        fuselage_length - tail_root_chord - 0.015 * fuselage_length)
        x_tail_le = min(x_tail_le, tail_x_le_max - tail_root_chord)
        geometry["vtail"] = {
            "name": "VTail",
            "panel_span_m": round(panel_span, 4),
            "root_chord_m": round(panel_root, 4),
            "tip_chord_m": round(panel_tip, 4),
            "dihedral_deg": round(math.degrees(gamma), 2),
            "sweep_deg": 20.0,
            "x_le_m": round(max(x_tail_le, x_wing_le + mac), 4),
            "z_m": round(max(
                radius_at_x(geometry["fuselage"], x_tail_le),
                radius_at_x(geometry["fuselage"], x_tail_le + panel_root),
                radius_at_x(geometry["fuselage"], x_tail_le + 0.5 * panel_root)) + 0.006, 4),
            "airfoil": "NACA0012",
        }
        geometry["control_surfaces"] = [
            {"component": "wing", "name": "Aileron", "u_start": 0.55, "u_end": 0.95,
             "chord_fraction": 0.22},
            {"component": "vtail", "name": "Ruddervator", "u_start": 0.05, "u_end": 0.95,
             "chord_fraction": 0.30},
        ]
        tail_area_total = 2.0 * panel_area
    else:
        htail_ar = 4.0
        htail_span = math.sqrt(htail_ar * htail_equivalent_area)
        htail_taper = 0.45
        htail_root = 2.0 * htail_equivalent_area / (htail_span * (1.0 + htail_taper))
        htail_tip = htail_root * htail_taper
        vtail_ar = 2.2
        twin_vtail = bool(getattr(spec, "twin_vtail", False))
        vtail_area_each = vtail_equivalent_area / (2.0 if twin_vtail else 1.0)
        vtail_span = math.sqrt(vtail_ar * vtail_area_each)
        vtail_taper = 0.40
        vtail_root = 2.0 * vtail_area_each / (vtail_span * (1.0 + vtail_taper))
        vtail_tip = vtail_root * vtail_taper
        x_htail_le = min(max(x_wing_le + 0.55 * fuselage_length,
                             fuselage_length - htail_root - 0.02 * fuselage_length),
                         tail_x_le_max - htail_root)
        x_vtail_le = min(max(x_wing_le + 0.60 * fuselage_length,
                             fuselage_length - vtail_root - 0.02 * fuselage_length),
                         tail_x_le_max - vtail_root)
        geometry["htail"] = {
            "name": "HTail",
            "span_m": round(htail_span, 4),
            "root_chord_m": round(htail_root, 4),
            "tip_chord_m": round(htail_tip, 4),
            "x_le_m": round(x_htail_le, 4),
            "z_m": 0.0,
            "sweep_deg": 12.0,
            "airfoil": "NACA0012",
        }
        vtail_z = round(max(
            radius_at_x(geometry["fuselage"], x_vtail_le),
            radius_at_x(geometry["fuselage"], x_vtail_le + vtail_root),
            radius_at_x(geometry["fuselage"], x_vtail_le + 0.5 * vtail_root)) + 0.006, 4)
        vtail_name = "VTailL" if twin_vtail else "VTail"
        vtail_left_y = 0.0
        if twin_vtail:
            # 双垂尾装在平尾两端，左右各一片；每片面积为目标垂尾总面积的一半。
            vtail_left_y = -0.5 * htail_span
            vtail_z = 0.0
        geometry["vtail_conventional"] = {
            "name": vtail_name,
            "span_m": round(vtail_span, 4),
            "root_chord_m": round(vtail_root, 4),
            "tip_chord_m": round(vtail_tip, 4),
            "x_le_m": round(x_vtail_le, 4),
            "z_m": vtail_z,
            "y_offset_m": round(vtail_left_y, 4),
            "sweep_deg": 30.0,
            "airfoil": "NACA0012",
        }
        if twin_vtail:
            geometry["vtail_right"] = {
                "name": "VTailR",
                "span_m": round(vtail_span, 4),
                "root_chord_m": round(vtail_root, 4),
                "tip_chord_m": round(vtail_tip, 4),
                "x_le_m": round(x_vtail_le, 4),
                "z_m": vtail_z,
                "y_offset_m": round(0.5 * htail_span, 4),
                "sweep_deg": 30.0,
                "airfoil": "NACA0012",
            }
        # ---- 垂尾容量校核：按实际尾臂投影 Vv 修正到目标值 ----
        vtc = geometry.get("vtail_conventional")
        if vtc:
            vt_right = geometry.get("vtail_right")
            vtail_parts = [vtc] + ([vt_right] if vt_right else [])
            area_v_each = 0.5 * (float(vtc["root_chord_m"]) + float(vtc["tip_chord_m"])) * float(vtc["span_m"])
            for part in vtail_parts:
                part["area_m2"] = round(0.5 * (float(part["root_chord_m"]) + float(part["tip_chord_m"])) * float(part["span_m"]), 5)
            lv = max((float(vtc["x_le_m"]) + 0.25 * float(vtc["root_chord_m"])) -
                     (x_wing_le + 0.25 * mac), 0.05)
            actual_vv = area_v_each * len(vtail_parts) * lv / max(area * span, 1e-9)
            target_vv = vv * tail_scale * spec.tail_scale_v
            if actual_vv > 0 and target_vv > 0 and (actual_vv / target_vv > 1.08 or actual_vv / target_vv < 0.92):
                scale = _clamp(math.sqrt(target_vv / actual_vv), 0.75, 1.25)
                for part in vtail_parts:
                    part["span_m"] = round(float(part["span_m"]) * scale, 4)
                    part["root_chord_m"] = round(float(part["root_chord_m"]) * scale, 4)
                    part["tip_chord_m"] = round(float(part["tip_chord_m"]) * scale, 4)
                    part["area_m2"] = round(0.5 * (float(part["root_chord_m"]) + float(part["tip_chord_m"])) * float(part["span_m"]), 5)
                    part["z_m"] = round(max(
                        radius_at_x(geometry["fuselage"], float(part["x_le_m"])),
                        radius_at_x(geometry["fuselage"], float(part["x_le_m"]) + float(part["root_chord_m"]))) + 0.006, 4) if not twin_vtail else 0.0
                area_v_each = float(vtc.get("area_m2", area_v_each))
            total_area_v = area_v_each * len(vtail_parts)
            total_volume_coeff = total_area_v * lv / max(area * span, 1e-9)
            for part in vtail_parts:
                part["single_volume_coeff"] = round(area_v_each * lv / max(area * span, 1e-9), 5)
                part["volume_coeff"] = round(total_volume_coeff, 5)
                part["volume_target"] = round(target_vv, 5)
            geometry["sizing"]["vtail_volume_coeff"] = round(total_volume_coeff, 5)
            geometry["sizing"]["vtail_volume_target"] = round(target_vv, 5)
            geometry["sizing"]["vtail_total_area_m2"] = round(total_area_v, 5)
            geometry["sizing"]["twin_vtail"] = twin_vtail

        geometry["control_surfaces"] = [
            {"component": "wing", "name": "Aileron", "u_start": 0.55, "u_end": 0.95,
             "chord_fraction": 0.22},
            {"component": "htail", "name": "Elevator", "u_start": 0.0, "u_end": 1.0,
             "chord_fraction": 0.30},
        ]
        if twin_vtail:
            geometry["control_surfaces"].extend([
                {"component": "VTailL", "name": "RudderL", "u_start": 0.0, "u_end": 1.0,
                 "chord_fraction": 0.30},
                {"component": "VTailR", "name": "RudderR", "u_start": 0.0, "u_end": 1.0,
                 "chord_fraction": 0.30},
            ])
        else:
            geometry["control_surfaces"].append(
                {"component": "vtail", "name": "Rudder", "u_start": 0.0, "u_end": 1.0,
                 "chord_fraction": 0.30})
        tail_area_total = htail_equivalent_area + vtail_equivalent_area

    # ---- 重心初值（稳定性求解后再按目标裕度移动） -----------------------
    # ---- 鸭翼 / 串列翼布局转换 -------------------------------------------------
    if spec.configuration == "canard":
        htail_old = geometry.get("htail")
        canard_area = max(0.08 * area * float(getattr(spec, "canard_area_scale", 1.0) or 1.0), 0.03)
        canard_taper = 0.50
        canard_ar = 4.0
        canard_span = math.sqrt(canard_ar * canard_area)
        canard_root = 2.0 * canard_area / (canard_span * (1.0 + canard_taper))
        canard_tip = canard_root * canard_taper
        canard_le = _clamp(x_wing_le - 0.48 * fuselage_length, 0.04 * fuselage_length,
                           x_wing_le - canard_root - 0.05)
        canard_z = round(0.08 * fuselage_diameter, 4)
        geometry["canard"] = {
            "name": "Canard",
            "span_m": round(canard_span, 4),
            "root_chord_m": round(canard_root, 4),
            "tip_chord_m": round(canard_tip, 4),
            "sweep_deg": 8.0,
            "dihedral_deg": 0.0,
            "twist_deg": 0.0,
            "x_le_m": round(canard_le, 4),
            "z_m": canard_z,
            "airfoil": "NACA0012",
            "symmetry": True,
            "volume_coeff": 0.35,
        }
        geometry.pop("htail", None)
        geometry["control_surfaces"] = [
            {"component": "wing", "name": "Aileron", "u_start": 0.55, "u_end": 0.95,
             "chord_fraction": 0.22},
            {"component": "canard", "name": "Elevator", "u_start": 0.0, "u_end": 1.0,
             "chord_fraction": 0.30},
            {"component": "vtail", "name": "Rudder", "u_start": 0.0, "u_end": 1.0,
             "chord_fraction": 0.30},
        ]
        vtail_area = _vtail_area_total(geometry)
        tail_area_total = canard_area + vtail_area
        geometry["sizing"].update({
            "canard_area_m2": round(canard_area, 4),
            "canard_volume_target": 0.35,
            "tail_area_total_m2": round(tail_area_total, 4),
            "layout_note": "鸭翼在前、主翼在后、垂尾在尾部",
        })
    elif spec.configuration == "tandem":
        # 后翼由原平尾位置重建为第二主翼
        rear_area = 0.85 * area
        rear_span = 0.92 * span
        rear_taper = 0.55
        rear_root = 2.0 * rear_area / (rear_span * (1.0 + rear_taper))
        rear_tip = rear_root * rear_taper
        x_rear = _clamp(max(x_wing_le + 0.42 * fuselage_length, 0.62 * fuselage_length),
                        0.35 * fuselage_length,
                        max(0.35 * fuselage_length, fuselage_length - rear_root - 0.04 * fuselage_length))
        geometry["tandem_wing"] = {
            "name": "TandemWing",
            "span_m": round(rear_span, 4),
            "root_chord_m": round(rear_root, 4),
            "tip_chord_m": round(rear_tip, 4),
            "sweep_deg": 4.0,
            "dihedral_deg": 2.0,
            "twist_deg": 0.0,
            "x_le_m": round(x_rear, 4),
            "z_m": 0.0,
            "airfoil": "NACA0012",
            "symmetry": True,
        }
        geometry.pop("htail", None)
        geometry["control_surfaces"] = [
            {"component": "wing", "name": "Aileron", "u_start": 0.55, "u_end": 0.95,
             "chord_fraction": 0.22},
            {"component": "tandem_wing", "name": "Elevator", "u_start": 0.0, "u_end": 1.0,
             "chord_fraction": 0.30},
            {"component": "vtail", "name": "Rudder", "u_start": 0.0, "u_end": 1.0,
             "chord_fraction": 0.30},
        ]
        vtail_area = _vtail_area_total(geometry)
        tail_area_total = rear_area + vtail_area
        geometry["sizing"].update({
            "tandem_area_m2": round(rear_area, 4),
            "tandem_total_area_m2": round(area + rear_area, 4),
            "tail_area_total_m2": round(tail_area_total, 4),
            "layout_note": "前翼 + 后翼串列、垂尾在尾部",
        })

    _trim_root_panels(geometry, spec)

    xcg_initial = x_wing_le + 0.25 * mac
    geometry["sizing"].update({
        "tail_arm_m": round(tail_arm, 4),
        "tail_area_total_m2": round(tail_area_total, 4),
        "htail_equivalent_area_m2": round(htail_equivalent_area, 4),
        "vtail_equivalent_area_m2": round(vtail_equivalent_area, 4),
        "xcg_initial_m": round(xcg_initial, 4),
        "mac_m": round(mac, 4),
        "area_m2": round(area, 4),
        "aspect_ratio": round(ar, 3),
    })
    return geometry


# --------------------------------------------------------------------------- #
# 案例基线（上传 .vsp3 作为设计/优化基线）
# --------------------------------------------------------------------------- #
def _case_tail_area_ratio(case_parameters: dict[str, Any]) -> float | None:
    """案例尾翼总面积 / 主翼面积（用于保持案例尾翼规模比例）。"""
    wings = case_parameters.get("wings")
    if not isinstance(wings, list) or not wings:
        return None
    main = case_parameters.get("wing") if isinstance(case_parameters.get("wing"),
                                                     dict) else {}
    main_area = float(main.get("area_m2") or 0.0)
    if main_area <= 0:
        return None
    tail_total = 0.0
    for item in wings:
        if not isinstance(item, dict):
            continue
        area = float(item.get("area_m2") or 0.0)
        if abs(area - main_area) > 1e-9:
            tail_total += area
    return tail_total / main_area if tail_total > 0 else None


def apply_case_baseline(spec: DesignSpec, case_parameters: dict[str, Any],
                        span_allowance: float = 1.15) -> dict[str, Any]:
    """把已有 .vsp3 案例参数并入 spec 作为设计基线（案例优先于文本 sizing 默认值）。

    规则：
    - prompt/附件中**显式给出**的参数优先（``spec.sources`` 记录来源）；
    - 未显式修改的参数采用案例值（主翼 S/AR/taper/sweep/dihedral、机身长/径）；
    - 翼展：prompt 未给出时给案例翼展 ``span_allowance`` 倍余量作为约束上限；
    - 尾翼：按案例“尾翼/机翼面积比”对整体尾翼面积做缩放。

    返回实际采用的参数字典（供日志与 result.json 记录）。
    """
    if not isinstance(case_parameters, dict) or not case_parameters.get("ok"):
        return {}
    wing = case_parameters.get("wing") if isinstance(case_parameters.get("wing"),
                                                    dict) else {}
    fuselage = (case_parameters.get("fuselage")
                if isinstance(case_parameters.get("fuselage"), dict) else {})
    sources = spec.sources or {}
    applied: dict[str, Any] = {}

    def prompt_has(field: str) -> bool:
        return field in sources

    def use(field: str, value: Any, transform=None) -> None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if transform is not None:
            number = transform(number)
        setattr(spec, field, number)
        applied[field] = round(number, 4)

    if wing.get("area_m2") is not None and not prompt_has("wing_area_m2"):
        use("wing_area_m2", wing["area_m2"])
    if wing.get("aspect_ratio") is not None and not prompt_has("aspect_ratio"):
        use("aspect_ratio", wing["aspect_ratio"])
    if wing.get("taper_ratio") is not None and not prompt_has("taper_ratio"):
        use("taper_ratio", wing["taper_ratio"],
            transform=lambda value: _clamp(value, 0.25, 0.9))
    if wing.get("sweep_deg") is not None and not prompt_has("sweep_deg"):
        use("sweep_deg", wing["sweep_deg"])
    if wing.get("dihedral_deg") is not None and not prompt_has("dihedral_deg"):
        use("dihedral_deg", wing["dihedral_deg"])
    if fuselage.get("length_m") is not None and not prompt_has("fuselage_length_m"):
        use("fuselage_length_m", fuselage["length_m"])
    if (fuselage.get("max_diameter_m") is not None
            and not prompt_has("fuselage_diameter_m")):
        use("fuselage_diameter_m", fuselage["max_diameter_m"])
    if wing.get("span_m") is not None and not prompt_has("max_span_m"):
        case_span = float(wing["span_m"])
        existing = float(spec.max_span_m) if spec.max_span_m else 0.0
        spec.max_span_m = max(existing, case_span * float(span_allowance))
        applied["max_span_m"] = round(spec.max_span_m, 4)
    tail_ratio = _case_tail_area_ratio(case_parameters)
    if tail_ratio:
        try:
            baseline_geometry = size_aircraft(spec)
            sizing = baseline_geometry.get("sizing") or {}
            base_area = float(sizing.get("area_m2") or 0.0)
            base_tail = float(sizing.get("tail_area_total_m2") or 0.0)
            if base_area > 0 and base_tail > 0:
                scale = _clamp(tail_ratio * base_area / base_tail, 0.5, 2.0)
                spec.tail_scale_h = scale
                spec.tail_scale_v = scale
                applied["tail_area_scale"] = round(scale, 4)
        except Exception:  # pragma: no cover - 尾翼比例仅近似
            pass
    for field_name in applied:
        spec.sources[field_name] = "case_model"
    return applied


# --------------------------------------------------------------------------- #
# 设计引擎（状态机 + offline 流程）
# --------------------------------------------------------------------------- #
class Designer:
    """串联需求 → 几何 → 分析 → 优化 → 导出 → 报告。"""

    def __init__(self, config: AIConfig, logger: Logger, bridge: VSPBridge | None = None):
        self.config = config
        self.logger = logger
        self.bridge = bridge or VSPBridge(
            openvsp_root=config.vsp.get("openvsp_root"),
            vspaero_exe=config.vsp.get("vspaero_exe"),
            logger=logger,
            timeout_s=float(config.vsp.get("timeout_s", 300)),
        )
        self.output_dir = config.ensure_output_dir()
        self.spec: DesignSpec | None = None
        self.geometry: dict[str, Any] | None = None
        self.analysis: dict[str, Any] | None = None
        self.metrics: dict[str, Any] | None = None
        self.model_path: str | None = None
        self.report_path: str | None = None
        self.exports: dict[str, str] = {}
        self.optimization: dict[str, Any] = {}
        # 上传案例 .vsp3（作为设计/优化基线）
        self.case_model: str | None = (
            str(getattr(config, "case_model", "") or "").strip() or None)
        self.case_parameters: dict[str, Any] | None = None
        self.case_summary: dict[str, Any] | None = None
        self.case_applied: dict[str, Any] = {}
        self.based_on_case: bool = False
        self._analysis_pass = 0
        self._started = time.time()
        # 翼型是否由用户/LLM 明确指定；否则按任务/Re/Ma 从翼型库自动选择。
        self.airfoil_locked = False
        self.airfoil_selection: dict[str, Any] | None = None
        self.stability_iterations: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ 需求
    def _apply_case_baseline(self, spec: DesignSpec) -> dict[str, Any]:
        """读取上传案例并作为基线并入 spec（失败仅 warning，不致命）。"""
        if not self.case_model:
            self.based_on_case = False
            return {}
        if self.case_parameters is None:
            try:
                params = self.bridge.read_model_parameters(self.case_model)
            except Exception as exc:  # pragma: no cover - 环境异常兜底
                self.logger.warn(f"读取案例 .vsp3 失败（按需求重新设计）: {exc}")
                return {}
            if not params.get("ok"):
                self.logger.warn(
                    f"读取案例 .vsp3 失败（按需求重新设计）: {params.get('error')}")
                return {}
            self.case_parameters = params
            self.case_summary = summarize_case_parameters(params)
            wing = params.get("wing") if isinstance(params.get("wing"), dict) else {}
            fuselage = (params.get("fuselage")
                        if isinstance(params.get("fuselage"), dict) else {})
            self.logger.plan(f"读取上传案例作为设计基线: {self.case_model}")
            self.logger.status(
                f"案例参数: {Path(self.case_model).name} | "
                f"部件 {params.get('component_count')} | "
                f"主翼 S={wing.get('area_m2')} m², AR={wing.get('aspect_ratio')}, "
                f"λ={wing.get('taper_ratio')}, 后掠={wing.get('sweep_deg')}°, "
                f"上反={wing.get('dihedral_deg')}° | "
                f"机身 L={fuselage.get('length_m')} m, D={fuselage.get('max_diameter_m')} m")
        applied = apply_case_baseline(spec, self.case_parameters)
        self.case_applied = applied
        self.based_on_case = bool(applied)
        if applied:
            spec.assumptions.append(
                f"基于上传案例 {Path(self.case_model).name} 修改设计（采用 "
                f"{len(applied)} 项案例参数；需求中显式修改的参数优先）")
            self.logger.status(
                f"基于案例修改: 采用 {len(applied)} 项案例参数 "
                f"({', '.join(sorted(applied))})，其余按需求修改")
        return applied

    def get_case_info(self) -> dict[str, Any]:
        """案例相关信息（供 result.json / 日志）。"""
        return {
            "case_model": self.case_model,
            "based_on_case": self.based_on_case,
            "case_parameters": self.case_summary,
            "case_applied": self.case_applied,
        }

    def parse_requirements(self, prompt: str | None = None) -> DesignSpec:
        prompt = prompt if prompt is not None else self.config.prompt
        self.logger.plan("解析自然语言需求 → 结构化设计 spec")
        spec = parse_requirements(prompt)
        self.spec = spec
        self._apply_case_baseline(spec)
        self.logger.status(
            f"需求解析完成: {spec.name} | MTOW {spec.mtow_kg:.0f} kg | "
            f"高度 {spec.altitude_m:.0f} m | 速度 {spec.speed_min_ms:.0f}~"
            f"{spec.speed_max_ms:.0f} m/s | {CONFIG_LABELS.get(spec.configuration)} | "
            f"{ENGINE_LABELS.get(spec.engine, spec.engine)}"
            + (f" | 基于案例: {Path(self.case_model).name}"
               if self.based_on_case and self.case_model else ""))
        for item in spec.assumptions:
            self.logger.warn(f"需求假设: {item}")
        return spec

    # ------------------------------------------------------------------ 失效
    def _invalidate_downstream(self) -> None:
        """任何几何/参数变更前使旧分析、指标、导出和报告失效。"""
        self.metrics = None
        self.analysis = None
        self.report_path = None
        self.exports = {}
        self.model_path = None
        self._analysis_pass = 0

    # ------------------------------------------------------------------ 几何
    def _geometry_summary(self) -> dict[str, Any]:
        geometry = self.geometry or {}
        wing = geometry.get("wing", {})
        summary = {
            "configuration": geometry.get("configuration"),
            "wing": wing,
            "fuselage": geometry.get("fuselage", {}),
            "vtail": geometry.get("vtail"),
            "htail": geometry.get("htail"),
            "vtail_conventional": geometry.get("vtail_conventional"),
            "vtail_right": geometry.get("vtail_right"),
            "sizing": geometry.get("sizing", {}),
            "control_surfaces": geometry.get("control_surfaces", []),
            "model_path": self.model_path,
            "openvsp_version": self.bridge.version(),
        }
        return summary

    def size_aircraft(self, optimization: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.spec is None:
            raise VSPError("尚未解析设计需求")
        self.logger.plan("按需求进行机翼/尾翼/机身参数 sizing")
        geometry = size_aircraft(self.spec, optimization)
        self.geometry = geometry
        if not self.airfoil_locked and self.spec is not None and not getattr(
                self.spec, "airfoil_locked", False):
            try:
                from .airfoils import select_airfoil
                selection = select_airfoil(self.spec, geometry, self.logger)
                if selection:
                    self.airfoil_selection = selection
                    self.spec.airfoil_root = selection["name"]
                    geometry.setdefault("wing", {})["airfoil"] = selection["name"]
            except Exception as exc:
                self.logger.warn(f"自动翼型选择失败，保留 {self.spec.airfoil_root}: {exc}")
        wing = geometry["wing"]
        sizing = geometry["sizing"]
        fuse = geometry.get("fuselage")
        fuse_len_text = f"机身长 {fuse['length_m']:.2f} m" if fuse else "无独立机身"
        self.logger.status(
            f"参数 sizing: S={sizing.get('area_m2', 0):.3f} m², "
            f"b={wing['span_m']:.2f} m, AR={sizing.get('aspect_ratio', 0):.2f}, "
            f"Cr={wing['root_chord_m']:.3f} m, MAC={sizing.get('mac_m', 0):.3f} m, "
            f"{fuse_len_text}")
        return geometry

    def build_aircraft(self, optimization: dict[str, Any] | None = None,
                       model_name: str = "design_model") -> dict[str, Any]:
        self._invalidate_downstream()
        self.logger.plan("调用 OpenVSP Python API 建立三维几何")
        geometry = self.size_aircraft(optimization)
        built = self.bridge.build_geometry(geometry)
        self.logger.status(f"OpenVSP 建模完成: {built['totals']['component_count']} 个部件, "
                           f"全机展长 {built['totals'].get('span_m', 0):.2f} m")
        model_path = self.output_dir / f"{model_name}.vsp3"
        self.model_path = self.bridge.save_vsp3(model_path)
        geometry["component_count"] = built["totals"]["component_count"]
        geometry["components"] = built["components"]
        self._analysis_pass = 0
        return {"ok": True, "geometry": self._geometry_summary(),
                "model_path": self.model_path}

    # ------------------------------------------------------------------ 分析
    def _reference(self) -> dict[str, float]:
        geometry = self.geometry or {}
        wing = geometry.get("wing", {})
        sizing = geometry.get("sizing", {})
        cref = float(sizing.get("mac_m") or wing.get("root_chord_m", 0.2))
        xcg = float(sizing.get("xcg_initial_m", 0.0))
        return {
            "sref_m2": float(sizing.get("area_m2") or 0.0),
            "cref_m": cref,
            "bref_m": float(wing.get("span_m", 1.0)),
            "xcg_m": xcg,
            "y_m": 0.0,
            "z_m": 0.0,
        }

    def _flow(self) -> dict[str, Any]:
        spec = self.spec
        if spec is None:
            raise VSPError("尚未解析设计需求")
        atmosphere = standard_atmosphere(spec.altitude_m)
        velocity = float(self.config.design.get("velocity_ms")
                         or spec.speed_cruise_ms)
        density = float(atmosphere["density_kgm3"])
        mach = velocity / max(atmosphere["sound_speed_ms"], 1e-6)
        return {
            "velocity_ms": velocity,
            "altitude_m": spec.altitude_m,
            "density_kgm3": density,
            "sound_speed_ms": atmosphere["sound_speed_ms"],
            "temperature_k": atmosphere["temperature_k"],
            "mach": mach,
        }

    def _aoa_list(self) -> list[float]:
        start = float(self.config.design.get("aoa_start", -4.0))
        end = float(self.config.design.get("aoa_end", 10.0))
        npts = max(3, int(self.config.design.get("aoa_npts", 8)))
        if end <= start:
            end = start + 10.0
        return [round(start + index * (end - start) / (npts - 1), 3)
                for index in range(npts)]

    def run_analysis(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.model_path is None:
            raise VSPError("尚未建立几何模型，无法运行分析")
        params = params or {}
        spec = self.spec
        assert spec is not None
        self._analysis_pass += 1
        self.logger.plan(f"运行 VSPAERO 气动分析（第 {self._analysis_pass} 轮）")
        from .run_store import RunStore, fingerprint, sha256_file
        run_store = RunStore(self.output_dir)
        run_id, run_dir = run_store.new_run("vspaero")
        ref = self._reference()
        flow = self._flow()
        aoa_list = params.get("aoa_list") or self._aoa_list()
        betas = [float(b) for b in (params.get("betas")
                                    or self.config.design.get("beta_list") or [0.0])]
        stability = bool(params.get("stability",
                                    self.config.design.get("stability", True)))
        stability_alpha = params.get("stability_alpha_deg")
        if stability_alpha is None:
            stability_alpha = float(self.config.design.get("stability_alpha_deg", 2.0))
        wake_iters = int(params.get("wake_iters", self.config.design.get("wake_iters", 3)))
        n_cpu = int(params.get("n_cpu", self.config.design.get("n_cpu", 4)))
        input_payload = {
            "run_id": run_id,
            "model_path": str(self.model_path),
            "model_sha256": sha256_file(self.model_path) if self.model_path else None,
            "ref": ref,
            "flow": flow,
            "aoa_list": aoa_list,
            "betas": betas,
            "stability": stability,
            "geometry": self.geometry or {},
            "analysis_pass": self._analysis_pass,
        }
        input_fingerprint = fingerprint(input_payload)
        run_store.write_json(run_dir, "input.json", input_payload)
        run_store.write_json(run_dir, "fingerprint.json", {
            "input_fingerprint": input_fingerprint,
            "method": "VSPAERO_THIN",
            "openvsp_version": self.bridge.version(),
        })
        self.logger.status(
            f"VSPAERO 设置: Sref={ref['sref_m2']:.3f} m², Cref={ref['cref_m']:.3f} m, "
            f"Bref={ref['bref_m']:.2f} m, Xcg={ref['xcg_m']:.3f} m, "
            f"V={flow['velocity_ms']:.1f} m/s, Mach={flow['mach']:.3f}, "
            f"AoA={aoa_list[0]:.1f}~{aoa_list[-1]:.1f}°")
        try:
            analysis = self.bridge.run_vspaero(
                self.model_path, run_dir, ref, flow,
                aoa_list=aoa_list, betas=betas, stability=stability,
                stability_alpha_deg=stability_alpha,
                wake_iters=wake_iters, n_cpu=n_cpu)
        except VSPError as exc:
            self.logger.error(f"VSPAERO 分析失败: {exc}")
            run_store.write_manifest(run_dir, {
                "run_id": run_id, "status": "failed", "kind": "vspaero",
                "input_fingerprint": input_fingerprint,
                "error": str(exc), "ref": ref, "flow": flow,
                "files": run_store.file_records([]),
            })
            raise
        analysis["ref"] = ref
        analysis["flow"] = flow
        analysis["aoa_list"] = aoa_list
        analysis["betas"] = betas
        analysis["pass"] = self._analysis_pass
        analysis["run_id"] = run_id
        analysis["run_dir"] = str(run_dir)
        analysis["input_fingerprint"] = input_fingerprint
        output_files = [analysis.get("polar_path"), analysis.get("stab_path")]
        run_store.write_manifest(run_dir, {
            "run_id": run_id, "status": "completed", "kind": "vspaero",
            "input_fingerprint": input_fingerprint,
            "model_path": str(self.model_path),
            "model_sha256": sha256_file(self.model_path) if self.model_path else None,
            "ref": ref, "flow": flow, "aoa_list": aoa_list, "betas": betas,
            "method": "VSPAERO_THIN",
            "openvsp_version": self.bridge.version(),
            "files": run_store.file_records([p for p in output_files if p]),
        })
        self.analysis = analysis
        polar = analysis.get("polar") or {}
        rows = polar.get("rows") or []
        valid = [row for row in rows
                 if abs(float(row.get("Beta", 0.0))) < 1e-6
                 and not math.isnan(float(row.get("CLtot", 0.0)))]
        self.logger.status(
            f"VSPAERO 完成: {len(rows)} 个算例（β=0 有效 {len(valid)} 个），"
            f"耗时 {analysis.get('total_elapsed_s', 0):.1f} s"
            + ("，含稳定性导数" if analysis.get("stab") else "，稳定性导数缺失"))
        summary = summarize_polar(polar)
        if summary.get("ok"):
            self.logger.status(
                f"气动初步结果: CLmax={summary.get('cl_max', float('nan')):.3f} "
                f"@α={summary.get('alpha_at_cl_max_deg', float('nan')):.1f}°, "
                f"CLα={summary.get('cl_alpha_per_rad', float('nan')):.2f}/rad, "
                f"(L/D)max={summary.get('ld_max', float('nan')):.2f} "
                f"@α={summary.get('alpha_at_ld_max_deg', float('nan')):.1f}°")
        self.metrics = self._compute_metrics()
        return {"ok": True, "metrics": self.metrics}

    # ------------------------------------------------------------------ 指标
    def _x_np_m(self) -> float | None:
        stab = (self.analysis or {}).get("stab") or {}
        x_np_ft = stab.get("x_np")
        if x_np_ft is None or x_np_ft in (float("nan"), 0):
            return None
        return float(x_np_ft) * vsp_bridge.FT_TO_M

    def _compute_metrics(self) -> dict[str, Any]:
        spec = self.spec
        geometry = self.geometry or {}
        analysis = self.analysis or {}
        assert spec is not None
        ref = analysis.get("ref") or self._reference()
        flow = analysis.get("flow") or self._flow()
        polar = analysis.get("polar") or {}
        stab = analysis.get("stab") or {}

        # 只取 β=0 的纵向算例
        rows = [row for row in (polar.get("rows") or [])
                if abs(float(row.get("Beta", 0.0))) < 1e-6]
        polar_zero = dict(polar)
        polar_zero["rows"] = rows
        xcg_initial = float(ref.get("xcg_m", 0.0))
        cref = float(ref.get("cref_m", 0.2))

        # 目标静稳定裕度 → 设计重心位置（优先 .stab 的 X_np，缺失时用极曲线斜率降级）
        x_np = self._x_np_m()
        sm_raw = stab.get("sm")
        summary_initial = summarize_polar(polar_zero)
        if x_np is None:
            sm_polar = summary_initial.get("sm_polar")
            if sm_polar is not None and not math.isnan(float(sm_polar)):
                x_np = xcg_initial + float(sm_polar) * cref
                if sm_raw is None:
                    sm_raw = float(sm_polar)
        if x_np is not None:
            xcg_target = x_np - spec.static_margin * cref
            wing = geometry.get("wing", {})
            x_wing_le = float(wing.get("x_le_m", 0.0))
            wing_chord = float(wing.get("root_chord_m", cref))
            fuselage = geometry.get("fuselage") or {}
            chord_fraction = 0.95 if spec.configuration == "flying_wing" else 0.60
            if fuselage:
                fuselage_length = float(fuselage.get("length_m", 99.0))
                if spec.configuration == "canard":
                    # 鸭翼布局的重心可以在主翼之前，范围向前放宽
                    xcg_low = max(0.06 * fuselage_length,
                                  x_wing_le - 0.75 * fuselage_length)
                    xcg_high = min(x_wing_le + 0.55 * wing_chord,
                                   0.95 * fuselage_length)
                elif spec.configuration == "tandem":
                    # 串列翼的重心可在前后翼之间移动，范围向后放宽
                    xcg_low = max(x_wing_le + 0.02 * wing_chord,
                                  0.04 * fuselage_length)
                    xcg_high = min(x_wing_le + 0.72 * fuselage_length,
                                   0.95 * fuselage_length)
                else:
                    xcg_low = max(x_wing_le + 0.05 * wing_chord, 0.02 * fuselage_length)
                    xcg_high = min(x_wing_le + chord_fraction * wing_chord,
                                   0.95 * fuselage_length)
            else:
                # 飞翼没有独立机身时，重心范围只能由机翼根弦决定。
                xcg_low = x_wing_le + 0.05 * wing_chord
                xcg_high = x_wing_le + chord_fraction * wing_chord
            xcg_final = _clamp(xcg_target, xcg_low, xcg_high)
            if abs(xcg_final - xcg_target) > 1e-6:
                self.logger.warn(
                    f"重心限位: 目标 Xcg={xcg_target:.3f} m 超出几何可用范围 "
                    f"[{xcg_low:.3f}, {xcg_high:.3f}] m，实际采用 {xcg_final:.3f} m；"
                    f"静稳定裕度将偏离目标 {spec.static_margin*100:.0f}% MAC")
            sm_final = (x_np - xcg_final) / max(cref, 1e-9)
        else:
            xcg_final = xcg_initial
            sm_final = None

        # 力矩参考点平移到设计重心
        polar_final = VSPBridge.shift_moments(polar_zero, xcg_initial, xcg_final, cref)
        summary = summarize_polar(polar_final)
        summary["sm_from_stab"] = sm_raw
        summary["sm_design"] = sm_final
        summary["x_np_m"] = x_np
        summary["xcg_initial_m"] = xcg_initial
        summary["xcg_design_m"] = xcg_final
        summary["static_margin_target"] = spec.static_margin
        summary["static_margin_error"] = (sm_final - spec.static_margin
                                          if sm_final is not None else None)
        summary["stable"] = (sm_final > 0.02) if sm_final is not None else None

        # 稳定性导数
        derivatives = derive_stability_derivatives(stab) if stab else {}
        derivatives["sm_stab"] = sm_raw

        # ---- 巡航性能 ----------------------------------------------------
        rho = float(flow.get("density_kgm3", 1.0))
        velocity = float(flow.get("velocity_ms", 30.0))
        weight = spec.mtow_kg * GRAVITY
        area = float(ref.get("sref_m2", 1.0))
        cl_cruise = weight / max(0.5 * rho * velocity ** 2 * area, 1e-9)
        cd_cruise = _interpolate_cd(polar_final.get("rows", []), cl_cruise)
        if cd_cruise is None:
            cd_cruise = summary.get("cd_min")
        ld_cruise = (cl_cruise / cd_cruise) if cd_cruise else None
        power_required = (weight * velocity / ld_cruise) if ld_cruise else None
        cruise = {
            "velocity_ms": round(velocity, 2),
            "cl_cruise": round(cl_cruise, 4),
            "cd_cruise": round(cd_cruise, 5) if cd_cruise else None,
            "ld_cruise": round(ld_cruise, 2) if ld_cruise else None,
            "power_required_w": round(power_required, 1) if power_required else None,
            "power_required_kw": round(power_required / 1000.0, 2) if power_required else None,
        }
        if spec.engine == "electric" and power_required:
            cruise["electric_power_kw"] = round(power_required / 0.72 / 1000.0, 2)
            cruise["propulsion_efficiency"] = 0.72

        # ---- 质量 / 配平估算 --------------------------------------------
        mass = _mass_breakdown(spec)
        design_public = spec.to_dict()
        wing_geom = geometry.get("wing", {})
        sizing_geom = geometry.get("sizing", {})
        actual_area = float(sizing_geom.get("area_m2") or 0.0)
        actual_span = float(wing_geom.get("span_m") or 0.0)
        actual_ar = (actual_span ** 2 / actual_area) if actual_area > 0 else None
        design_public["actual_wing_area_m2"] = round(actual_area, 4)
        design_public["actual_span_m"] = round(actual_span, 4)
        design_public["actual_aspect_ratio"] = round(actual_ar, 3) if actual_ar else None
        if self.airfoil_selection:
            design_public["airfoil_selection"] = self.airfoil_selection
        design_public.setdefault("warnings", [])
        if (spec.aspect_ratio and actual_ar
                and abs(actual_ar - spec.aspect_ratio) / spec.aspect_ratio > 0.05):
            warning = (f"需求展弦比 {spec.aspect_ratio:.2f} 与实际几何 "
                       f"{actual_ar:.2f} 不一致（受翼展/面积约束），请检查需求冲突")
            design_public["warnings"].append(warning)
            self.logger.warn(warning)
        metrics = {
            "design": design_public,
            "geometry": self._geometry_summary(),
            "reference": {key: round(float(value), 5) for key, value in ref.items()},
            "flow": {key: round(float(value), 5) for key, value in flow.items()},
            "aero": summary,
            "stability": {
                "static_margin": round(sm_final, 4) if sm_final is not None else None,
                "static_margin_target": spec.static_margin,
                "neutral_point_m": round(x_np, 4) if x_np is not None else None,
                "xcg_m": round(xcg_final, 4),
                "xcg_initial_m": round(xcg_initial, 4),
                "stable": summary.get("stable"),
                "derivatives": {key: (round(value, 6) if isinstance(value, float) else value)
                                for key, value in derivatives.items()},
                "sm_method": "VSPAERO .stab（SM/X_np）" if sm_raw is not None
                             else "polar CM-CL 斜率降级估计",
            },
            "cruise": cruise,
            "mass": mass,
            "polar": {
                "columns": polar_final.get("columns", []),
                "rows": [_round_row(row) for row in polar_final.get("rows", [])],
                "beta_rows_count": len(polar.get("rows", [])),
            },
            "vspaero": {
                "pass": analysis.get("pass", self._analysis_pass),
                "run_id": analysis.get("run_id"),
                "run_dir": analysis.get("run_dir"),
                "input_fingerprint": analysis.get("input_fingerprint"),
                "elapsed_s": analysis.get("total_elapsed_s"),
                "runs": [{key: run.get(key) for key in
                          ("kind", "returncode", "elapsed_s", "base")}
                         for run in analysis.get("runs", [])],
                "polar_path": analysis.get("polar_path"),
                "stab_path": analysis.get("stab_path"),
            },
            "analysis_passes": self._analysis_pass,
            "assumptions": list(spec.assumptions),
        }
        if not stab:
            self.logger.warn("未获得 .stab 稳定性结果，静稳定裕度使用 polar 斜率法估计")
        try:
            from .criteria import evaluate_design
            metrics["design_criteria"] = evaluate_design(spec, geometry, metrics)
            # 把当前经验准则映射为可靠设计状态契约。
            # 这些检查尚未绑定 run_id/原始文件证据，因此 evidence 只能是不完整，
            # recommendation 只能是 diagnostic_only；不能冒充已验证可行。
            from .contracts import ConstraintResult, aggregate_constraints
            constraints = []
            for item in (metrics["design_criteria"].get("checks") or []):
                raw_status = str(item.get("status") or "unknown")
                status = "pass" if raw_status == "pass" else (
                    "fail" if raw_status == "fail" else "unknown")
                constraints.append(ConstraintResult(
                    constraint_id=str(item.get("id") or "criterion"),
                    quantity=str(item.get("name") or item.get("id") or "criterion"),
                    status=status,
                    value=item.get("value") if isinstance(item.get("value"), (int, float)) else None,
                    limit=(item.get("high") if isinstance(item.get("high"), (int, float)) else None),
                    unit=str(item.get("unit") or ""),
                    hard=True,
                    reason_code=None if status != "unknown" else "NO_EVIDENCE_OR_INCOMPLETE_DATA",
                    note=str(item.get("note") or ""),
                ))
            metrics["design_decision"] = aggregate_constraints(
                constraints, task_status="completed", approved_profile=False)
        except Exception as exc:
            self.logger.warn(f"设计准则评估失败（忽略）: {exc}")
        return metrics

    # ------------------------------------------------------------------ 优化
    def optimize(self, objective: str = "cruise_ld", rebuild_threshold: float = 0.02,
                 max_span_m: float | None = None) -> dict[str, Any]:
        """基于实测极曲线的解析优化（翼面积 / 展弦比 / 尾容量）。

        用第一轮 VSPAERO 结果标定 CD0 与 Oswald 效率，再在约束内网格搜索展弦比，
        若改善超过阈值则给出重建几何的候选参数（由 offline 流程决定是否再算一轮）。
        """
        if self.spec is None or self.metrics is None:
            raise VSPError("优化前需要先完成需求解析与气动分析")
        spec = self.spec
        aero = self.metrics.get("aero", {})
        geometry = self.geometry or {}
        sizing = geometry.get("sizing", {})
        area0 = float(sizing.get("area_m2") or 1.0)
        ar0 = float(sizing.get("aspect_ratio") or 8.0)
        span0 = float(geometry.get("wing", {}).get("span_m") or 1.0)
        cd_min = aero.get("cd_min")
        if cd_min is None or not isinstance(cd_min, float) or math.isnan(cd_min):
            return {"ok": False, "message": "缺少可用 CDmin，跳过优化"}
        rho = float(self.metrics.get("flow", {}).get("density_kgm3", 1.2))
        velocity = float(self.metrics.get("flow", {}).get("velocity_ms", 30.0))
        weight = spec.mtow_kg * GRAVITY
        span_limit = float(max_span_m or spec.max_span_m or 1e9)
        # Oswald 效率：用极曲线中 CDo/CDi 差分标定
        oswald = _estimate_oswald(self.metrics.get("polar", {}).get("rows", []), ar0)
        oswald = _clamp(oswald, 0.55, 0.98)
        clmax_eff = max(spec.clmax * 0.92, 0.7)
        v_stall = spec.speed_min_ms / max(spec.stall_margin, 1.0)

        def ld_at(area: float, ar: float) -> dict[str, float]:
            """代理模型：由实测 CDmin / Oswald 效率估算巡航升阻比。"""
            span = math.sqrt(area * ar)
            cl_cruise = weight / max(0.5 * rho * velocity ** 2 * area, 1e-9)
            cd = (cd_min + cl_cruise ** 2 / (math.pi * ar * oswald)
                  * (area0 / area) ** 0.5)
            ld = cl_cruise / cd if cd > 0 else 0.0
            return {"area_m2": round(area, 4), "aspect_ratio": round(ar, 3),
                    "span_m": round(span, 3), "cl_cruise": round(cl_cruise, 4),
                    "cd_cruise": round(cd, 5), "ld_cruise": round(ld, 2)}

        def evaluate(area: float, ar: float) -> dict[str, float] | None:
            span = math.sqrt(area * ar)
            if span > span_limit * 1.001:
                return None
            if 0.5 * rho * v_stall ** 2 * area * clmax_eff < weight * 0.998:
                return None
            candidate = ld_at(area, ar)
            if candidate["cl_cruise"] > clmax_eff:
                return None
            return candidate

        candidates: list[dict[str, float]] = []
        for area_factor in (0.94, 0.97, 1.0, 1.03):
            area = area0 * area_factor
            for ar in [ar0 * factor for factor in
                       (0.75, 0.9, 1.0, 1.1, 1.2, 1.35)]:
                candidate = evaluate(area, ar)
                if candidate:
                    candidates.append(candidate)
        if not candidates:
            return {"ok": False, "message": "无满足约束的候选方案"}
        baseline = ld_at(area0, ar0)
        candidates.sort(key=lambda item: item["ld_cruise"], reverse=True)
        best = candidates[0]
        improvement = ((best["ld_cruise"] - baseline["ld_cruise"])
                       / max(baseline["ld_cruise"], 1e-6))
        rebuild = improvement > rebuild_threshold
        result = {
            "ok": True,
            "objective": objective,
            "method": "基于第一轮 VSPAERO 极曲线标定 CD0 / Oswald 效率的解析网格搜索",
            "oswald_efficiency": round(oswald, 4),
            "cd_min": round(cd_min, 5),
            "baseline": {**{key: round(value, 4) for key, value in baseline.items()},
                         "area_m2": round(area0, 4), "aspect_ratio": round(ar0, 3),
                         "span_m": round(span0, 3)},
            "candidates_evaluated": len(candidates),
            "top_candidates": candidates[:5],
            "best": best,
            "improvement_ratio": round(improvement, 4),
            "rebuild": bool(rebuild),
            "warnings": [],
        }
        if span_limit < 1e8 and best["span_m"] > span_limit * 0.999:
            result["warnings"].append("最优方案受翼展上限约束")
        self.optimization = result
        if rebuild:
            self.logger.status(
                f"解析优化: AR {ar0:.1f}→{best['aspect_ratio']:.1f}, "
                f"巡航 L/D {baseline['ld_cruise']:.2f}→{best['ld_cruise']:.2f} "
                f"(+{improvement * 100:.1f}%)；若启用 rebuild_on_optimize 将重建几何复算")
        else:
            self.logger.status(
                f"解析优化: 当前方案已接近最优（候选 {len(candidates)} 个，"
                f"提升 {improvement * 100:.1f}% < {rebuild_threshold * 100:.0f}%），保留原几何")
        return result

    # ------------------------------------------------------------------ 收尾
    def finalize(self) -> dict[str, Any]:
        """导出 STL / DegenGeom CSV，并生成最终指标与文件清单。"""
        import os

        self.logger.plan("导出几何文件并汇总最终设计指标")
        exports: dict[str, str] = {}
        if self.model_path:
            exports["vsp3"] = self.model_path
        try:
            stl_path = self.output_dir / "design_model.stl"
            self.bridge.export(stl_path, "stl")
            if stl_path.is_file():
                exports["stl"] = str(stl_path)
                self.logger.status(f"导出 STL: {stl_path.name} "
                                   f"({stl_path.stat().st_size / 1024:.0f} KB)")
        except Exception as exc:
            self.logger.warn(f"STL 导出失败（忽略）: {exc}")
        try:
            csv_path = self.output_dir / "design_model_degen.csv"
            previous_cwd = os.getcwd()
            try:
                os.chdir(self.output_dir)
                result = self.bridge.compute_degen_geometry(csv_path)
                exports["csv"] = str(result.get("csv_path") or "")
            finally:
                os.chdir(previous_cwd)
            if exports.get("csv"):
                self.logger.status(f"导出 DegenGeom CSV: {Path(exports['csv']).name}")
        except Exception as exc:
            self.logger.warn(f"DegenGeom CSV 导出失败（忽略）: {exc}")
        self.exports = {key: value for key, value in exports.items() if value}
        if self.metrics is not None:
            self.metrics["files"] = dict(self.exports)
            self.metrics["files"]["polar"] = (self.analysis or {}).get("polar_path")
            self.metrics["files"]["stab"] = (self.analysis or {}).get("stab_path")
            self.metrics["elapsed_s"] = round(time.time() - self._started, 1)
        return {"ok": True, "exports": self.exports}

    def generate_report(self, title: str | None = None) -> str:
        from .report import generate_report as _generate_report
        if self.metrics is None:
            raise VSPError("生成报告前需要先完成气动分析")
        self.logger.plan("按内置模板生成中文 Word 设计报告")
        path = _generate_report(
            output_dir=self.output_dir,
            template=self.config.template,
            title=title or self.config.design_title,
            spec=self.spec,
            metrics=self.metrics,
            geometry=self.geometry,
            logger=self.logger,
            bridge=self.bridge,
        )
        self.report_path = path
        self.logger.status(f"设计报告已生成: {path}")
        return path

    # ------------------------------------------------------------------ 离线
    def _refine_static_stability(self, max_rounds: int = 3,
                                 min_rounds: int = 2) -> list[dict[str, Any]]:
        """首轮后按静稳定性做多轮优化。

        每轮读取 VSPAERO 稳定性导数；若俯仰或航向静不稳定，调整参数并
        真实重建/复算。至少执行两轮（首轮 + 稳定性校核轮）；仍不稳定则
        最多执行 max_rounds 轮，并在结果中保留未解决状态。
        """
        from .stability_guard import (adjustment_plan, apply_changes,
                                      assess_static_stability)
        history: list[dict[str, Any]] = []
        if not self.spec or not self.metrics:
            return history
        for round_index in range(max_rounds):
            assessment = assess_static_stability(self.metrics)
            pitch = assessment.get("pitch", {})
            yaw = assessment.get("yaw", {})
            history.append({
                "round": round_index + 1,
                "pitch": pitch.get("status"),
                "yaw": yaw.get("status"),
                "sm": pitch.get("sm"),
                "cma": pitch.get("cma"),
                "cnb": yaw.get("cnb"),
                "note": "；".join(x for x in (pitch.get("reason"), yaw.get("reason")) if x),
                "changes": [],
            })
            self.logger.status(
                f"静稳定检查第 {round_index + 1} 轮: 俯仰={pitch.get('status')}，"
                f"航向={yaw.get('status')}；SM={pitch.get('sm')}，"
                f"Cnβ={yaw.get('cnb')}")
            if round_index + 1 >= max_rounds:
                break
            if assessment.get("stable") and round_index + 1 >= min_rounds:
                break
            if assessment.get("stable") and round_index == 0:
                # 稳定也执行一次独立校核轮，满足“每个算例多跑几轮”
                self.logger.plan("稳定性已通过，执行第二轮独立校核")
                self.run_analysis()
                continue
            plan = adjustment_plan(self.spec, assessment)
            applied = apply_changes(self.spec, plan)
            if not applied:
                self.logger.warn("静稳定未通过，但没有可自动调整的参数；保留诊断状态")
                break
            history[-1]["changes"] = applied
            self.logger.plan("静稳定优化: " + "；".join(plan.get("notes") or applied))
            self.build_aircraft()
            self.run_analysis()
        self.stability_iterations = history
        self.metrics["stability_iterations"] = history
        return history

    def _refine_design_criteria(self, max_rounds: int = 2) -> list[dict[str, Any]]:
        """按准则检查结果调整总体参数，并真实重算 VSPAERO 若干轮。"""
        history: list[dict[str, Any]] = []
        if not self.spec or not self.metrics:
            return history
        for round_index in range(max_rounds + 1):
            criteria = self.metrics.get("design_criteria") or {}
            checks = {c.get("id"): c for c in criteria.get("checks", [])}
            history.append({
                "round": round_index,
                "overall": criteria.get("overall"),
                "summary": criteria.get("summary"),
                "area_m2": (self.geometry or {}).get("sizing", {}).get("area_m2"),
                "span_m": (self.geometry or {}).get("wing", {}).get("span_m"),
            })
            if round_index >= max_rounds or criteria.get("overall") == "pass":
                break
            changed: list[str] = []
            stall = checks.get("stall_speed") or {}
            if stall.get("status") in ("warn", "fail"):
                limit = float(stall.get("high") or 0.0)
                value = float(stall.get("value") or 0.0)
                if limit > 0 and value > limit:
                    old_area = float((self.geometry or {}).get("sizing", {}).get("area_m2") or 0.0)
                    if old_area > 0:
                        scale = min(max((value / limit) ** 2 * 1.02, 1.0), 2.2)
                        new_area = old_area * scale
                        ar = float((self.geometry or {}).get("sizing", {}).get("aspect_ratio") or 8.0)
                        max_span = float(self.spec.max_span_m or 1e9)
                        if math.sqrt(new_area * ar) > max_span * 0.98:
                            new_area = (max_span * 0.98) ** 2 / max(ar, 1e-6)
                        if new_area > old_area * 1.005:
                            self.spec.wing_area_m2 = new_area
                            changed.append(f"增大机翼面积至 {new_area:.3f} m²（失速约束）")
            for ident, attr in (("vtail_h", "tail_volume_h"), ("vtail_v", "tail_volume_v")):
                item = checks.get(ident) or {}
                if item.get("status") in ("warn", "fail"):
                    value = float(item.get("value") or 0.0)
                    target = (float(self.spec.tail_volume_h)
                              if ident == "vtail_h" else float(self.spec.tail_volume_v))
                    if value > 1e-9 and target > 1e-9:
                        new_value = max(0.2, min(2.5, target * (target / value)))
                        if abs(new_value - target) / target > 0.02:
                            setattr(self.spec, attr, new_value)
                            changed.append(f"调整 {attr}={new_value:.4f}")
            dihedral_check = checks.get("wing_dihedral") or {}
            if dihedral_check.get("status") != "pass" and dihedral_check.get("value") is not None:
                current = float(self.spec.dihedral_deg)
                self.spec.dihedral_deg = _clamp(current + (2.5 if current < 2.5 else -1.0), 0.0, 5.0)
                changed.append(f"调整上反角至 {self.spec.dihedral_deg:.2f}°")
            if not changed:
                self.logger.warn("准则检查存在警告，但没有可自动调整的总体参数")
                break
            self.logger.plan("准则迭代第 " + str(round_index + 1) + " 轮：" + "；".join(changed))
            self.build_aircraft()
            self.run_analysis()
        self.metrics["design_iterations"] = history
        if len(history) > 1:
            self.logger.status(f"准则迭代完成，共 {len(history)} 轮；最终 {history[-1].get('overall')}")
        return history

    def run_offline(self, prompt: str | None = None) -> dict[str, Any]:
        """确定性离线流程：无网络、无 LLM 亦可完成完整设计 + 分析 + 报告。

        默认只做一轮 VSPAERO 实算（扫掠 polar + 稳定性 .stab），随后用标定后的
        极曲线做解析优化建议；``config.design.rebuild_on_optimize=true`` 时若解析
        优化改善超过阈值则重建几何复算一轮（耗时约翻倍）。
        """
        self._started = time.time()
        self.logger.plan("离线确定性流程: 需求解析 → sizing → OpenVSP 建模 → "
                         "VSPAERO 分析 → 解析优化 → 导出 → 报告")
        spec = self.parse_requirements(prompt)
        rebuild_enabled = bool(self.config.design.get("rebuild_on_optimize", False))
        max_passes = 2 if rebuild_enabled else 1
        optimization_params: dict[str, Any] | None = None
        first_metrics: dict[str, Any] | None = None
        rebuilds = 0
        for attempt in range(max_passes):
            self.build_aircraft(optimization_params)
            self.run_analysis()
            if attempt == 0 and not self.based_on_case and bool(
                    self.config.design.get("stability_refine", True)):
                self._refine_static_stability(max_rounds=3, min_rounds=2)
            if attempt == 0 and not self.based_on_case and bool(
                    self.config.design.get("criteria_refine", True)):
                self._refine_design_criteria(max_rounds=2)
            if self.stability_iterations and self.metrics:
                # 准则迭代可能再次改变几何；对最终几何做一次静稳定复核。
                from .stability_guard import assess_static_stability
                final_assessment = assess_static_stability(self.metrics)
                final_pitch = final_assessment.get("pitch", {})
                final_yaw = final_assessment.get("yaw", {})
                self.stability_iterations.append({
                    "round": "final",
                    "pitch": final_pitch.get("status"),
                    "yaw": final_yaw.get("status"),
                    "sm": final_pitch.get("sm"),
                    "cma": final_pitch.get("cma"),
                    "cnb": final_yaw.get("cnb"),
                    "note": "最终几何复核：" + "；".join(
                        x for x in (final_pitch.get("reason"), final_yaw.get("reason")) if x),
                    "changes": [],
                })
                self.metrics["stability_iterations"] = self.stability_iterations
                if not final_assessment.get("stable"):
                    self.logger.warn("最终几何静稳定仍未全部通过；报告保留诊断状态")
            if first_metrics is None:
                first_metrics = json.loads(json.dumps(self.metrics, default=str))
            opt = self.optimize()
            if attempt == 0 and rebuild_enabled and opt.get("rebuild"):
                best = opt["best"]
                optimization_params = {
                    "aspect_ratio": best["aspect_ratio"],
                    "wing_area_m2": best["area_m2"],
                }
                # 机翼面积变化时同步更新 spec（报告口径一致）
                spec.wing_area_m2 = best["area_m2"]
                spec.aspect_ratio = best["aspect_ratio"]
                rebuilds += 1
                continue
            break

        self.metrics["optimization"] = self.optimization
        self.metrics["baseline_metrics"] = {
            "aero": (first_metrics or {}).get("aero"),
            "stability": (first_metrics or {}).get("stability"),
            "cruise": (first_metrics or {}).get("cruise"),
        }
        self.metrics["rebuilds"] = rebuilds
        self.metrics["offline"] = True
        self.finalize()
        report_path = self.generate_report()
        elapsed = round(time.time() - self._started, 1)
        self.logger.status(f"离线流程完成，总耗时 {elapsed:.1f} s")
        return {
            "ok": True,
            "mode": "offline",
            "report_path": report_path,
            "model_path": self.model_path,
            "metrics": self.metrics,
            "exports": self.exports,
            "elapsed_s": elapsed,
        }

    # ------------------------------------------------------------------ 工具用
    def get_geometry(self) -> dict[str, Any]:
        if self.geometry is None:
            return {"ok": False, "error": "尚未建立几何模型"}
        return {"ok": True, "geometry": self._geometry_summary()}

    def modify_parameter(self, component: str, parameter: str, value: float) -> dict[str, Any]:
        """修改几何参数并重建模型（工具 modify_parameter 的后端）。"""
        if self.geometry is None:
            raise VSPError("尚未建立几何模型")
        geometry = self.geometry
        component_key = str(component).strip().lower()
        parameter = str(parameter).strip()
        value = float(value)
        target: dict[str, Any] | None = None
        role = ""
        if component_key in ("wing", "mainwing", "机翼"):
            target, role = geometry.get("wing"), "wing"
        elif component_key in ("fuselage", "机身"):
            target, role = geometry.get("fuselage"), "fuselage"
        elif component_key in ("vtail", "v尾", "v-tail"):
            target = geometry.get("vtail")
            role = "vtail"
            if target is None:
                target, role = geometry.get("vtail_conventional"), "vtail"
        elif component_key in ("htail", "平尾", "水平尾翼"):
            target, role = geometry.get("htail"), "htail"
        if target is None:
            raise VSPError(f"未找到部件: {component}")

        alias = {
            "span": "span_m", "span_m": "span_m", "展长": "span_m",
            "root_chord": "root_chord_m", "root_chord_m": "root_chord_m",
            "tip_chord": "tip_chord_m", "tip_chord_m": "tip_chord_m",
            "sweep": "sweep_deg", "sweep_deg": "sweep_deg",
            "dihedral": "dihedral_deg", "dihedral_deg": "dihedral_deg",
            "x_le": "x_le_m", "x_le_m": "x_le_m", "z": "z_m", "z_m": "z_m",
            "length": "length_m", "length_m": "length_m",
            "max_diameter": "max_diameter_m", "max_diameter_m": "max_diameter_m",
            "panel_span": "panel_span_m", "panel_span_m": "panel_span_m",
        }
        key = alias.get(parameter.lower(), parameter)
        if key not in target:
            raise VSPError(f"部件 {component} 不支持参数 {parameter}；可选: "
                           f"{', '.join(sorted(target.keys()))}")
        old_value = target[key]
        target[key] = value
        # 机翼面积/展弦比联动
        if role == "wing" and key in ("span_m", "root_chord_m", "tip_chord_m"):
            span = float(target["span_m"])
            root = float(target["root_chord_m"])
            tip = float(target["tip_chord_m"])
            area = 0.5 * (root + tip) * span
            target["area_m2"] = area
            target["taper_ratio"] = tip / root if root else 0.0
            target["aspect_ratio"] = span ** 2 / area if area else 0.0
            sizing = geometry.setdefault("sizing", {})
            sizing["area_m2"] = round(area, 4)
            sizing["aspect_ratio"] = round(target["aspect_ratio"], 3)
            if span:
                sizing["mac_m"] = round(
                    (2.0 / 3.0) * root
                    * (1 + target["taper_ratio"] + target["taper_ratio"] ** 2)
                    / (1 + target["taper_ratio"]), 4)
        self._invalidate_downstream()
        self.logger.status(f"参数修改: {role}.{key} {old_value} → {value}，重建 OpenVSP 模型")
        # 直接使用修改后的 geometry 重建，避免 build_aircraft()/size_aircraft()
        # 用 spec 重新 sizing 覆盖用户修改（历史上导致后掠角等修改不生效）。
        built = self.bridge.build_geometry(geometry)
        model_path = self.output_dir / "design_model.vsp3"
        self.model_path = self.bridge.save_vsp3(model_path)
        geometry["component_count"] = built["totals"]["component_count"]
        geometry["components"] = built["components"]
        self.geometry = geometry
        self._analysis_pass = 0
        self.run_analysis()
        return {"ok": True, "component": role, "parameter": key,
                "old_value": old_value, "value": value,
                "model_path": self.model_path, "metrics": self.metrics}


# --------------------------------------------------------------------------- #
# 数值辅助
# --------------------------------------------------------------------------- #
def _interpolate_cd(rows: list[dict[str, Any]], cl_target: float) -> float | None:
    """在极曲线中按 CL 线性插值 CD。"""
    points: list[tuple[float, float]] = []
    for row in rows:
        cl = row.get("CLtot", row.get("CL"))
        cd = row.get("CDtot", row.get("CD"))
        if cl is None or cd is None or math.isnan(cl) or math.isnan(cd):
            continue
        points.append((float(cl), float(cd)))
    if not points:
        return None
    points.sort(key=lambda item: item[0])
    if cl_target <= points[0][0]:
        return points[0][1]
    if cl_target >= points[-1][0]:
        return points[-1][1]
    for (cl_a, cd_a), (cl_b, cd_b) in zip(points, points[1:]):
        if cl_a <= cl_target <= cl_b and abs(cl_b - cl_a) > 1e-9:
            ratio = (cl_target - cl_a) / (cl_b - cl_a)
            return cd_a + ratio * (cd_b - cd_a)
    return points[-1][1]


def _estimate_oswald(rows: list[dict[str, Any]], aspect_ratio: float) -> float:
    """由极曲线 CDo/CDi 列估计 Oswald 效率。"""
    samples: list[float] = []
    for row in rows:
        cl = row.get("CLtot", row.get("CL"))
        cdi = row.get("CDi")
        if cl is None or cdi is None or math.isnan(cl) or math.isnan(cdi) or cdi <= 1e-7:
            continue
        if abs(cl) < 0.05:
            continue
        efficiency = cl ** 2 / (math.pi * aspect_ratio * cdi)
        if 0.3 <= efficiency <= 1.2:
            samples.append(efficiency)
    if not samples:
        return 0.85
    samples.sort()
    return samples[len(samples) // 2]


def _mass_breakdown(spec: DesignSpec) -> dict[str, Any]:
    """按经验比例给出质量分解（报告用，明确标注为估算）。"""
    fractions = {
        "机翼结构": 0.16,
        "机身结构": 0.20,
        "尾翼结构": 0.06 if spec.configuration != "flying_wing" else 0.0,
        "动力/能源": 0.20 if spec.engine == "electric" else 0.16,
        "航电与系统": 0.10,
        "任务载荷": 0.14,
        "电池/燃油": 0.14 if spec.engine == "electric" else 0.28,
    }
    total_fraction = sum(fractions.values())
    if total_fraction <= 0:
        total_fraction = 1.0
    items = []
    for name, fraction in fractions.items():
        mass = spec.mtow_kg * fraction / total_fraction
        items.append({"item": name, "mass_kg": round(mass, 2),
                      "fraction": round(mass / spec.mtow_kg, 4) if spec.mtow_kg else 0})
    return {
        "mtow_kg": spec.mtow_kg,
        "items": items,
        "note": "按同类无人机经验比例估算，仅用于报告质量分配，非结构分析结果",
    }


def _round_row(row: dict[str, Any], digits: int = 6) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, float):
            result[key] = None if math.isnan(value) else round(value, digits)
        else:
            result[key] = value
    return result

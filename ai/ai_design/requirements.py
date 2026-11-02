# -*- coding: utf-8 -*-
"""类型化需求解析（schema 2.0 的首批落地）。

仅做原文识别、单位归一化和基础一致性检查；不替代用户确认。
输出 RequirementSet 供 Designer 继承旧 DesignSpec 兼容字段，同时保留
stall_speed_max 与 minimum_operating_speed 等独立语义。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "2.0"


@dataclass
class RequirementConstraint:
    id: str
    quantity: str
    operator: str
    value_si: float
    unit_si: str
    hard: bool = True
    source_quote: str = ""
    confirmed: bool = False
    unit_original: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "quantity": self.quantity,
            "operator": self.operator,
            "value_si": self.value_si,
            "unit_si": self.unit_si,
            "hard": self.hard,
            "source": {"kind": "user_text", "quote": self.source_quote},
            "confirmed": self.confirmed,
            "unit_original": self.unit_original,
        }


@dataclass
class RequirementSet:
    raw_text: str
    constraints: list[RequirementConstraint] = field(default_factory=list)
    missing_required_data: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    explicit_airfoil: str | None = None

    def get(self, quantity: str) -> RequirementConstraint | None:
        for item in self.constraints:
            if item.quantity == quantity:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "requirements_id": "mission-001",
            "raw_text": self.raw_text,
            "constraints": [c.to_dict() for c in self.constraints],
            "missing_required_data": list(self.missing_required_data),
            "conflicts": list(self.conflicts),
            "assumptions": list(self.assumptions),
            "explicit_airfoil": self.explicit_airfoil,
        }


_NUMBER = r"([-+]?\d+(?:\.\d+)?)"
_UNIT_SPEED = r"(m/s|mps|公里/小时|km/h)"


def _speed_to_ms(value: float, unit: str) -> float:
    if unit.lower() in ("km/h", "公里/小时"):
        return value / 3.6
    return value


def _num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _match(pattern: str, text: str, flags: int = re.IGNORECASE) -> re.Match | None:
    return re.search(pattern, text, flags)


def _add(items: list[RequirementConstraint], cid: str, quantity: str,
         operator: str, value: float, unit: str, quote: str,
         unit_original: str = "") -> None:
    if not math.isfinite(value):
        return
    items.append(RequirementConstraint(
        id=cid, quantity=quantity, operator=operator,
        value_si=float(value), unit_si=unit, source_quote=quote.strip(),
        unit_original=unit_original or unit,
    ))


def parse_requirement_set(prompt: str) -> RequirementSet:
    text = prompt or ""
    result = RequirementSet(raw_text=text)

    # MTOW
    m = _match(r"(?:最大起飞重量|最大起飞质量|最大重量|MTOW)[^0-9]{0,12}" + _NUMBER + r"\s*(kg|公斤|千克)", text)
    if m:
        value = _num(m.group(1))
        if value is not None and value > 0:
            _add(result.constraints, "req-mtow", "mtow", "le", value, "kg", m.group(0), m.group(2))
    else:
        result.missing_required_data.append("mtow")

    # altitude; 0 is valid
    m = _match(r"(?:海拔|高度)[^0-9]{0,10}" + _NUMBER + r"\s*(m|米|km|千米)", text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            unit = m.group(2)
            if unit in ("km", "千米"):
                value *= 1000.0
            _add(result.constraints, "req-altitude", "altitude", "eq", value, "m", m.group(0), unit)
    else:
        result.missing_required_data.append("altitude")

    # speed range
    m = _match(_NUMBER + r"\s*(?:[-~至到]|—|－)\s*" + _NUMBER + r"\s*" + _UNIT_SPEED, text)
    if m:
        lo = _num(m.group(1)); hi = _num(m.group(2))
        unit = m.group(3)
        if lo is not None and hi is not None and lo > 0 and hi > lo:
            lo = _speed_to_ms(lo, unit); hi = _speed_to_ms(hi, unit)
            _add(result.constraints, "req-speed-min", "minimum_speed", "ge", lo, "m/s", m.group(0), unit)
            _add(result.constraints, "req-speed-max", "maximum_speed", "ge", hi, "m/s", m.group(0), unit)

    # cruise
    m = _match(r"巡航(?:速度)?[^0-9]{0,8}" + _NUMBER + r"\s*" + _UNIT_SPEED, text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            value = _speed_to_ms(value, m.group(2))
            _add(result.constraints, "req-cruise", "cruise_speed", "eq", value, "m/s", m.group(0), m.group(2))

    # stall upper limit; must not be conflated with minimum operating speed
    m = _match(r"失速速度?[^0-9]{0,12}(?:不超过|不大于|低于|≤|<|小于)?\s*" + _NUMBER + r"\s*" + _UNIT_SPEED, text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            value = _speed_to_ms(value, m.group(2))
            _add(result.constraints, "req-stall", "stall_speed_max", "le", value, "m/s", m.group(0), m.group(2))
    else:
        result.missing_required_data.append("stall_speed_max")

    # minimum operating speed, explicitly distinct from stall
    m = _match(r"(?:最低任务速度|最小飞行速度|最低飞行速度|最小使用速度)[^0-9]{0,10}" + _NUMBER + r"\s*" + _UNIT_SPEED, text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            value = _speed_to_ms(value, m.group(2))
            _add(result.constraints, "req-min-op", "minimum_operating_speed", "ge", value, "m/s", m.group(0), m.group(2))

    # payload
    m = _match(r"(?:有效载荷|任务载荷|载荷重量|载荷)[^0-9]{0,10}" + _NUMBER + r"\s*(kg|公斤|千克)", text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            _add(result.constraints, "req-payload", "payload_mass", "eq", value, "kg", m.group(0), m.group(2))
    else:
        result.missing_required_data.append("payload_mass")

    # span upper bound
    m = _match(r"翼展[^0-9]{0,10}(?:不超过|不大于|≤|<|小于)?\s*" + _NUMBER + r"\s*(m|米)", text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            _add(result.constraints, "req-span", "span_max", "le", value, "m", m.group(0), m.group(2))

    # wing area
    m = _match(r"(?:机翼面积|翼面积)[^0-9]{0,10}" + _NUMBER + r"\s*(m2|m²|平方米)", text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            _add(result.constraints, "req-area", "wing_area", "eq", value, "m2", m.group(0), m.group(2))

    # aspect ratio, including ranges
    m = _match(r"展弦比[^0-9]{0,10}" + _NUMBER + r"(?:\s*[~至到-]\s*" + _NUMBER + r")?", text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            _add(result.constraints, "req-ar", "aspect_ratio", "ge", value, "1", m.group(0))

    # signed angles; do not silently take absolute value
    m = _match(r"后掠角[^0-9-]{0,10}" + _NUMBER + r"\s*(?:°|度)", text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            _add(result.constraints, "req-sweep", "wing_sweep", "eq", value, "deg", m.group(0))
    m = _match(r"(?:机翼)?上反角[^0-9-]{0,10}" + _NUMBER + r"\s*(?:°|度)", text)
    if m:
        value = _num(m.group(1))
        if value is not None:
            _add(result.constraints, "req-dihedral", "wing_dihedral", "eq", value, "deg", m.group(0))

    # explicit airfoil; lock marker is retained
    m = _match(r"(NACA\s*\d{4,5}|[A-Za-z][A-Za-z0-9() ,_.-]{2,40})", text)
    if re.search(r"NACA\s*\d{4,5}", text):
        naca = re.search(r"NACA\s*\d{4,5}", text, re.IGNORECASE)
        if naca:
            name = "NACA" + re.sub(r"\D", "", naca.group(0))
            result.constraints.append(RequirementConstraint(
                id="req-airfoil", quantity="airfoil", operator="eq",
                value_si=0.0, unit_si="name", source_quote=naca.group(0),
                confirmed=True,
            ))
            result.explicit_airfoil = name
            result.assumptions.append(f"显式翼型 {name} 已锁定")

    # minimum consistency check: b^2/S should match AR when all explicit
    span = result.get("span_max")
    area = result.get("wing_area")
    ar = result.get("aspect_ratio")
    if span and area and ar:
        implied = span.value_si ** 2 / max(area.value_si, 1e-12)
        if abs(implied - ar.value_si) / max(abs(ar.value_si), 1e-9) > 0.02:
            result.conflicts.append({
                "quantity": "span_area_ar",
                "message": "翼展、面积与展弦比不一致",
                "span_m": span.value_si,
                "area_m2": area.value_si,
                "implied_ar": round(implied, 4),
                "stated_ar": ar.value_si,
            })

    return result


__all__ = ["SCHEMA_VERSION", "RequirementConstraint", "RequirementSet",
           "parse_requirement_set"]

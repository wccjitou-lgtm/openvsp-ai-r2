"""鸭翼与串列翼布局的几何/裁剪回归测试。"""
from __future__ import annotations

from ai_design.designer import DesignSpec, size_aircraft


def _layout(configuration: str):
    return size_aircraft(DesignSpec(mtow_kg=25, altitude_m=1000,
                                    speed_cruise_ms=25, speed_min_ms=20,
                                    speed_max_ms=40, payload_kg=5,
                                    wing_area_m2=2.25, aspect_ratio=9,
                                    configuration=configuration))


def test_canard_root_is_trimmed():
    geometry = _layout("canard")
    assert geometry["canard"]["trimmed_to_fuselage"] is True
    assert geometry["canard"]["root_y_m"] > 0
    assert geometry["canard"]["exposed_area_m2"] < geometry["canard"]["full_area_m2"]
    assert geometry["canard"]["x_le_m"] < geometry["wing"]["x_le_m"]


def test_tandem_rear_wing_is_trimmed():
    geometry = _layout("tandem")
    assert geometry["tandem_wing"]["trimmed_to_fuselage"] is True
    assert geometry["tandem_wing"]["root_y_m"] > 0
    assert geometry["tandem_wing"]["exposed_area_m2"] < geometry["tandem_wing"]["full_area_m2"]
    assert geometry["tandem_wing"]["x_le_m"] > geometry["wing"]["x_le_m"]

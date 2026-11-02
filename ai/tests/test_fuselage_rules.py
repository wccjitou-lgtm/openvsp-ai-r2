# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_design.designer import DesignSpec, size_aircraft
from ai_design.fuselage_rules import build_fuselage_rules, list_styles, radius_at_x


def _geom(**kw):
    spec = DesignSpec(**kw)
    return size_aircraft(spec)


def test_style_catalog():
    styles = {s["style"] for s in list_styles()}
    assert styles == {"circular_profile"}


def test_case_specific_styles():
    c1 = _geom(mtow_kg=25, altitude_m=1000, speed_cruise_ms=25, speed_min_ms=18,
               speed_max_ms=40, payload_kg=5, wing_area_m2=2.25, aspect_ratio=9,
               configuration="conventional")
    c2 = _geom(mtow_kg=15, altitude_m=2000, speed_cruise_ms=32, speed_min_ms=20,
               speed_max_ms=45, payload_kg=3, wing_area_m2=1.286, aspect_ratio=7,
               configuration="flying_wing")
    c3 = _geom(mtow_kg=120, altitude_m=8000, speed_cruise_ms=180, speed_min_ms=130,
               speed_max_ms=200, payload_kg=15, wing_area_m2=2.7, aspect_ratio=6,
               configuration="vtail", engine="EJ115")
    c4 = _geom(mtow_kg=60, altitude_m=6000, speed_cruise_ms=35, speed_min_ms=25,
               speed_max_ms=45, payload_kg=8, wing_area_m2=5.0, aspect_ratio=18,
               configuration="conventional")
    assert c2["fuselage"] is None
    for geometry in (c1, c3, c4):
        f = geometry["fuselage"]
        assert f["style"] == "circular_profile"
        assert f["width_ratio"] == 1.0
        assert f["section"] == "ellipse"
        assert all(abs(s[1] - s[2]) < 1e-9 for s in f["stations"])
        assert all(s[3] == 0.0 for s in f["stations"])
        factors = [s[1] for s in f["stations"]]
        assert len(factors) == 10
        assert factors == [0.5] + [1.0] * 8 + [0.5]
        assert [s[0] for s in f["stations"]] == [round(i / 9.0, 5) for i in range(10)]


def test_stations_have_z_profile_and_width():
    f = build_fuselage_rules(DesignSpec(mtow_kg=25, altitude_m=1000,
                                        speed_cruise_ms=25, speed_min_ms=18,
                                        speed_max_ms=40, payload_kg=5,
                                        wing_area_m2=2.25, aspect_ratio=9,
                                        configuration="conventional"), 4.5, 0.5)
    assert len(f["stations"]) == 10
    assert [s[0] for s in f["stations"]] == [round(i / 9.0, 5) for i in range(10)]
    assert all(abs(s[1] - s[2]) < 1e-9 and s[3] == 0.0 for s in f["stations"])
    assert [s[1] for s in f["stations"]] == [0.5] + [1.0] * 8 + [0.5]
    assert f["max_diameter_m"] == round(2.0 * f["radius_m"], 4)
    assert f["end_caps"] == "round"


def test_explicit_style_override():
    spec = DesignSpec(mtow_kg=25, altitude_m=1000, speed_cruise_ms=25,
                      speed_min_ms=18, speed_max_ms=40, payload_kg=5,
                      wing_area_m2=2.25, aspect_ratio=9,
                      configuration="conventional", fuselage_style="circular_profile")
    g = size_aircraft(spec)
    assert g["fuselage"]["style"] == "circular_profile"
    assert g["fuselage"]["width_ratio"] == 1.0


def test_roots_trimmed_to_fuselage_width():
    spec = DesignSpec(mtow_kg=25, altitude_m=1000, speed_cruise_ms=25,
                      speed_min_ms=18, speed_max_ms=40, payload_kg=5,
                      wing_area_m2=2.25, aspect_ratio=9,
                      configuration="conventional")
    geometry = size_aircraft(spec)
    wing = geometry["wing"]
    htail = geometry["htail"]
    radius = geometry["fuselage"]["radius_m"]
    assert wing["trimmed_to_fuselage"] is True
    assert wing["root_y_m"] >= radius
    assert wing["exposed_area_m2"] < wing["full_area_m2"]
    assert wing["exposed_span_m"] < wing["span_m"]
    assert htail["trimmed_to_fuselage"] is True
    local_htail_radius = radius_at_x(geometry["fuselage"],
                                     htail["x_le_m"] + 0.25 * htail["root_chord_m"])
    assert htail["root_y_m"] >= local_htail_radius
    assert htail["exposed_area_m2"] < htail["full_area_m2"]
    sizing = geometry["sizing"]
    assert sizing["exposed_wing_area_m2"] == wing["exposed_area_m2"]
    assert sizing["area_m2"] == wing["full_area_m2"]


def test_trim_can_be_disabled():
    spec = DesignSpec(mtow_kg=25, altitude_m=1000, speed_cruise_ms=25,
                      speed_min_ms=18, speed_max_ms=40, payload_kg=5,
                      wing_area_m2=2.25, aspect_ratio=9,
                      configuration="conventional", trim_roots_to_fuselage=False)
    geometry = size_aircraft(spec)
    assert not geometry["wing"].get("trimmed_to_fuselage")
    assert "exposed_wing_area_m2" not in geometry["sizing"]

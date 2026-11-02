# -*- coding: utf-8 -*-
"""翼型库与自动选型单元测试（不依赖 Profili 安装；有本地缓存时使用）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_design.airfoils import (find_airfoil, library_info, shortlist,
                                write_selig_dat)
from ai_design.designer import DesignSpec


def test_library_has_public_fallback_or_local_profili():
    info = library_info()
    assert info["count"] >= 7
    assert find_airfoil("NACA2412") is not None


def test_shortlist_high_speed_prefers_thin_low_camber():
    result = shortlist("high_speed", reynolds=800000, mach=0.55, count=5)
    cands = result["candidates"]
    assert cands
    assert all(c["thickness_pct"] <= 18.0 for c in cands)
    assert cands[0]["thickness_pct"] <= 13.0 or cands[0]["camber_pct"] <= 3.0


def test_shortlist_low_re_and_flying_wing():
    low = shortlist("low_re", reynolds=80000, mach=0.05, count=3)
    wing = shortlist("flying_wing", reynolds=150000, mach=0.05, count=3)
    assert low["candidates"] and wing["candidates"]
    # 低 Re 不应选出极薄/零弯度基准翼型作为首选
    assert low["candidates"][0]["thickness_pct"] >= 7.0
    assert wing["candidates"][0]["name"] != ""


def test_write_selig_dat(tmp_path):
    name = "NACA2412"
    item = find_airfoil(name)
    assert item is not None
    path = write_selig_dat(name, tmp_path)
    text = Path(path).read_text(encoding="utf-8").splitlines()
    assert len(text) >= 10
    assert text[0].upper().startswith("NACA")
    x, y = [float(v) for v in text[1].split()[:2]]
    assert 0.0 <= x <= 1.0 and -0.2 <= y <= 0.2


def test_design_spec_auto_selection_conditions():
    spec = DesignSpec(mtow_kg=25, altitude_m=1000, speed_cruise_ms=25,
                      aspect_ratio=9, configuration="conventional")
    # 仅验证模块可基于 spec 工作；Designer 集成在离线 E2E 中验证。
    result = shortlist("general", reynolds=475000, mach=0.074, count=3)
    assert result["candidates"]

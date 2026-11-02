"""vsp_bridge 解析与数值工具单元测试（不启动 OpenVSP/VSPAERO）。"""

from __future__ import annotations

import math

import pytest

from ai_design.vsp_bridge import (
    VSPBridge,
    derive_stability_derivatives,
    standard_atmosphere,
    summarize_polar,
)

POLAR_FIXTURE = """
                                                                      Surface Integration Forces and Moments -->
      Beta             Mach             AoA             Re/1e6             CLo             CLi            CLtot              CDo              CDi             CDtot               L/D                 E               CMy               CMytot             CS              CStot
  0.000000000000   0.099239000000  -4.000000000000   0.829333400000   0.000649342351  12.845136810338  12.845786152689   0.010188287878  -0.686394836806  -0.676206548928 -18.996837834628  -8.502695050278   0.000702425036  -0.651774000000   0.000000000000   0.000000000000
  0.000000000000   0.099239000000   2.000000000000   0.829333400000  -0.0003  0.5309  0.5309  0.0105  0.0095  0.0200  26.5  0.9  -0.0350 -0.0350  0.0  0.0
""".strip()


def test_parse_polar():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "test.polar"
        path.write_text(POLAR_FIXTURE, encoding="utf-8")
        polar = VSPBridge.parse_polar(path)
    assert polar["columns"][:5] == ["Beta", "Mach", "AoA", "Re_1e6", "CLo"]
    assert polar["count"] == 2
    assert polar["rows"][1]["AoA"] == 2.0
    assert polar["rows"][1]["CLtot"] == pytest.approx(0.5309)
    assert polar["rows"][1]["CMy"] == pytest.approx(-0.035, abs=1e-6)


STAB_FIXTURE = """
# Name                   Value      Units
Sref_                   3.4767430 Lunit^2
Cref_                   0.6299210 Lunit
Bref_                  13.1233600 Lunit
Xcg_                    0.0000000 Lunit
Mach_                   0.0882000 no_unit
AoA_                   -4.0000000 deg
#
Case                   Delta    Units         CFx          CFy          CFz          CMx          CMy          CMz          CL           CD           CS           CMl          CMm          CMn
#
Base_Aero              +0.000 n/a         -0.0359004   -0.0000000   -0.8555181   -0.0000000    0.2222083    0.0000000   -0.8559384    0.0238650   -0.0000000    0.0000000    0.2222083   -0.0000000
Alpha                  +0.010 deg         -0.0356351   -0.0000000   -0.8533912   -0.0000000    0.2216559    0.0000000   -0.8538023    0.0238323   -0.0000000    0.0000000    0.2216559   -0.0000000
Beta                   +0.010 deg         -0.0359004    0.0000000   -0.8555181   -0.0000011    0.2222083   -0.0000003   -0.8559384    0.0238650    0.0000042    0.0000011    0.2222083    0.0000003
#
Coef          Total        Alpha        Beta          p            q            r           Mach         U
#              -           per          per          per          per          per          per          per
#              -           rad          rad          rad          rad          rad          rad          M            u
#
CL         -0.8559384    11.3784342    0.0774632    0.0151553   84.0244034    0.1121572    0.3308070    0.0291772
CMy         0.2222083    -2.9532259    0.0001179    0.0449889  -22.0978655    0.0182716   -0.0842619   -0.0074319
#
# Result                 Value      Units
SM                      0.2464312 no_unit
X_np                    0.1552322 Lunit
"""


def test_parse_stab():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "test.stab"
        path.write_text(STAB_FIXTURE, encoding="utf-8")
        stab = VSPBridge.parse_stab(path)
    assert stab["case_count"] == 1
    assert stab["sm"] == pytest.approx(0.2464312)
    assert stab["x_np"] == pytest.approx(0.1552322)
    assert stab["reference"]["Sref"] == pytest.approx(3.4767430)
    derivatives = stab["derivatives"]
    assert derivatives["CL"]["alpha"] == pytest.approx(11.3784342)
    assert derivatives["CMy"]["alpha"] == pytest.approx(-2.9532259)
    summary = derive_stability_derivatives(stab)
    assert summary["cl_alpha_per_rad"] == pytest.approx(11.3784342)
    assert summary["sm_from_derivatives"] == pytest.approx(
        2.9532259 / 11.3784342, rel=1e-6)


def test_summarize_polar_linear_fit():
    rows = [
        {"AoA": 0.0, "CLtot": 0.30, "CDtot": 0.012, "CMytot": 0.02, "CDi": 0.001},
        {"AoA": 2.0, "CLtot": 0.49, "CDtot": 0.018, "CMytot": 0.00, "CDi": 0.004},
        {"AoA": 4.0, "CLtot": 0.68, "CDtot": 0.028, "CMytot": -0.02, "CDi": 0.009},
        {"AoA": 6.0, "CLtot": 0.86, "CDtot": 0.042, "CMytot": -0.04, "CDi": 0.016},
    ]
    summary = summarize_polar({"rows": rows})
    assert summary["ok"] is True
    assert summary["cl_max"] == pytest.approx(0.86)
    assert summary["alpha_at_cl_max_deg"] == 6.0
    expected_slope = 0.095  # 每度 ≈ 0.095 → 5.44/rad
    assert summary["cl_alpha_per_deg"] == pytest.approx(expected_slope, rel=0.02)
    assert summary["cl_alpha_per_rad"] == pytest.approx(
        expected_slope * 180.0 / math.pi, rel=0.02)
    assert summary["sm_polar"] == pytest.approx(0.1, abs=0.01)
    assert summary["ld_max"] > 25.0


def test_standard_atmosphere():
    sea = standard_atmosphere(0.0)
    assert sea["density_kgm3"] == pytest.approx(1.225, rel=1e-3)
    assert sea["pressure_pa"] == pytest.approx(101325, rel=1e-3)
    high = standard_atmosphere(5000.0)
    assert high["density_kgm3"] == pytest.approx(0.7364, rel=2e-2)
    assert high["sound_speed_ms"] == pytest.approx(320.5, rel=1e-2)


def test_shift_moments():
    polar = {"rows": [
        {"AoA": 0.0, "CLtot": 0.5, "CMy": -0.05},
        {"AoA": 4.0, "CLtot": 1.0, "CMy": -0.10},
    ]}
    shifted = VSPBridge.shift_moments(polar, 0.0, 0.1, 0.5)
    # Δxcg/Cref = 0.2 → CMy 增加 0.2*CL
    assert shifted["rows"][0]["CMy"] == pytest.approx(-0.05 + 0.2 * 0.5)
    assert shifted["rows"][1]["CMy"] == pytest.approx(-0.10 + 0.2 * 1.0)
    # 原数据不被修改
    assert polar["rows"][0]["CMy"] == pytest.approx(-0.05)

"""需求保留与三态/证据契约的回归测试。"""
from __future__ import annotations

from itertools import product

from ai_design.contracts import (
    ConstraintResult, EvidenceRef, aggregate_constraints,
    STATUS_PASS, STATUS_FAIL, STATUS_UNKNOWN,
    FEAS_INFEASIBLE, FEAS_UNKNOWN, FEAS_MODEL, FEAS_APPROVED,
    REC_DIAGNOSTIC, REC_ANALYSIS, REC_ENGINEERING,
)
from ai_design.designer import parse_requirements
from ai_design.requirements import parse_requirement_set


def test_parse_preserves_stall_min_speed_zero_altitude_and_signed_angles():
    prompt = ("电动无人机，最大起飞重量25 kg，海拔0 m，巡航90 km/h，"
              "失速速度不超过20 m/s，最低任务速度22 m/s，任务载荷5 kg，"
              "翼展不超过3.2 m，展弦比8，后掠角-5度，上反角-2度，"
              "采用NACA4412翼型")
    req = parse_requirement_set(prompt)
    assert req.get("altitude").value_si == 0.0
    assert req.get("cruise_speed").value_si == 25.0
    assert req.get("stall_speed_max").value_si == 20.0
    assert req.get("minimum_operating_speed").value_si == 22.0
    assert req.get("wing_sweep").value_si == -5.0
    assert req.get("wing_dihedral").value_si == -2.0
    assert req.explicit_airfoil == "NACA4412"

    spec = parse_requirements(prompt)
    assert spec.altitude_m == 0.0
    assert spec.speed_cruise_ms == 25.0
    assert spec.stall_speed_max_ms == 20.0
    assert spec.min_operating_speed_ms == 22.0
    assert spec.sweep_deg == -5.0
    assert spec.dihedral_deg == -2.0
    assert spec.airfoil_root == "NACA4412"
    assert spec.airfoil_locked is True


def test_conflicting_span_area_ar_is_reported():
    req = parse_requirement_set("翼展1 m，机翼面积2 m²，展弦比8")
    assert req.conflicts
    assert req.conflicts[0]["implied_ar"] == 0.5


def test_three_state_aggregation_truth_table():
    for statuses in product((STATUS_PASS, STATUS_FAIL, STATUS_UNKNOWN), repeat=3):
        constraints = []
        for i, status in enumerate(statuses):
            evidence = [EvidenceRef(metric_id=f"m{i}", run_id=f"r{i}")]
            constraints.append(ConstraintResult(
                constraint_id=f"c{i}", quantity="demo", status=status,
                hard=True, evidence=evidence))
        result = aggregate_constraints(constraints)
        if STATUS_FAIL in statuses:
            assert result["feasibility_status"] == FEAS_INFEASIBLE
            assert result["recommendation_status"] == REC_DIAGNOSTIC
        elif STATUS_UNKNOWN in statuses:
            assert result["feasibility_status"] == FEAS_UNKNOWN
            assert result["recommendation_status"] == REC_DIAGNOSTIC
        else:
            assert result["feasibility_status"] == FEAS_MODEL
            assert result["recommendation_status"] == REC_ANALYSIS


def test_complete_evidence_and_approved_profile_can_enter_engineering_review():
    constraints = [ConstraintResult(
        constraint_id="sm", quantity="static_margin", status=STATUS_PASS,
        value=0.10, limit=0.10, hard=True,
        evidence=[EvidenceRef(metric_id="sm", run_id="run-1", file="stab")],
    )]
    result = aggregate_constraints(constraints, approved_profile=True)
    assert result["feasibility_status"] == FEAS_APPROVED
    assert result["recommendation_status"] == REC_ENGINEERING
    assert result["evidence_status"] == "complete"


def test_empty_constraints_are_not_a_pass():
    result = aggregate_constraints([])
    assert result["feasibility_status"] == FEAS_UNKNOWN
    assert result["recommendation_status"] == REC_DIAGNOSTIC

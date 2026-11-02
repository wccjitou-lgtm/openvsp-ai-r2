"""Advisory formula registry and non-hard-rule decision behavior."""
from __future__ import annotations

from ai_design.aero_rules import RULES, list_rules
from ai_design.contracts import ConstraintResult, aggregate_constraints
from ai_design.tools import normalize_tool_name, tool_schemas


def test_rule_registry_has_prioritized_research_items():
    assert len(RULES) >= 20
    result = list_rules()
    assert result["authority"] == "advisory"
    assert result["counts"]["drag"] >= 3
    p0 = list_rules(level="P0")
    assert p0["rules"]
    assert any("尾容量" in r["name"] for r in p0["rules"])


def test_design_rules_tool_is_exposed():
    names = {item["function"]["name"] for item in tool_schemas()}
    assert "openvsp_list_design_rules" in names
    assert normalize_tool_name("list_design_rules") == "openvsp_list_design_rules"


def test_advisory_failure_does_not_hard_block_model_feasibility():
    checks = [
        ConstraintResult("wing_loading", "翼载", "pass", hard=False),
        ConstraintResult("tail_volume", "经验尾容量", "fail", hard=False,
                         reason_code="ADVISORY_RULE"),
    ]
    result = aggregate_constraints(checks, task_status="completed")
    assert result["feasibility_status"] == "feasible_under_model"
    assert result["recommendation_status"] == "analysis_only"
    assert result["summary"]["fail"] == 1

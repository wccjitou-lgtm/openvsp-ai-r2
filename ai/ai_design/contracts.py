# -*- coding: utf-8 -*-
"""可靠设计的状态、约束与证据契约（schema 2.0 的首批落地）。

本模块只负责“如何表达结论”，不计算气动/质量/配平指标。
任何指标必须由已注册计算服务产生，LLM 不得直接写 status。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

SCHEMA_VERSION = "2.0"

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"
VALID_CONSTRAINT_STATUS = (STATUS_PASS, STATUS_FAIL, STATUS_UNKNOWN)

TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

FEAS_INFEASIBLE = "infeasible_for_evaluated_constraints"
FEAS_UNKNOWN = "unknown"
FEAS_MODEL = "feasible_under_model"
FEAS_APPROVED = "feasible_under_approved_profile"

EVIDENCE_COMPLETE = "complete"
EVIDENCE_INCOMPLETE = "incomplete"
EVIDENCE_INVALID = "invalid"

REC_DIAGNOSTIC = "diagnostic_only"
REC_ANALYSIS = "analysis_only"
REC_ENGINEERING = "engineering_review"
REC_VERIFIED = "verified"

VALID_TASK = (TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED)
VALID_FEASIBILITY = (FEAS_INFEASIBLE, FEAS_UNKNOWN, FEAS_MODEL, FEAS_APPROVED)
VALID_EVIDENCE = (EVIDENCE_COMPLETE, EVIDENCE_INCOMPLETE, EVIDENCE_INVALID)
VALID_RECOMMENDATION = (REC_DIAGNOSTIC, REC_ANALYSIS, REC_ENGINEERING, REC_VERIFIED)


@dataclass
class EvidenceRef:
    metric_id: str
    run_id: str | None = None
    file: str | None = None
    sha256: str | None = None
    method: str | None = None
    applicability: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ConstraintResult:
    constraint_id: str
    quantity: str
    status: str
    value: float | None = None
    limit: float | None = None
    unit: str | None = None
    hard: bool = True
    reason_code: str | None = None
    note: str | None = None
    evidence: list[EvidenceRef] = field(default_factory=list)
    approved_threshold_ref: str | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_CONSTRAINT_STATUS:
            raise ValueError(f"invalid constraint status: {self.status}")
        if self.status == "warn":
            raise ValueError("warn is not a hard-constraint status")
        if self.hard and self.status == STATUS_FAIL and not self.reason_code:
            self.reason_code = "HARD_CONSTRAINT_VIOLATED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "quantity": self.quantity,
            "status": self.status,
            "value": self.value,
            "limit": self.limit,
            "unit": self.unit,
            "hard": self.hard,
            "reason_code": self.reason_code,
            "note": self.note,
            "evidence": [e.to_dict() for e in self.evidence],
            "approved_threshold_ref": self.approved_threshold_ref,
        }


def aggregate_constraints(
    constraints: Iterable[ConstraintResult],
    *,
    task_status: str = TASK_COMPLETED,
    evidence_status: str | None = None,
    approved_profile: bool = False,
) -> dict[str, Any]:
    """按开发方案的硬约束合取规则计算总体状态。

    有已证实的 fail -> infeasible；
    无 fail 但有 unknown -> unknown；
    全部 pass 但无批准 profile -> feasible_under_model；
    全部 pass 且有批准 profile -> feasible_under_approved_profile。
    空约束集合不得视为完整通过，返回 unknown/incomplete。
    """
    if task_status not in VALID_TASK:
        raise ValueError(f"invalid task status: {task_status}")
    items = list(constraints)
    statuses = [c.status for c in items]
    hard_fail = any(c.hard and c.status == STATUS_FAIL for c in items)
    hard_unknown = any(c.hard and c.status == STATUS_UNKNOWN for c in items)
    all_pass = bool(items) and all(c.status == STATUS_PASS for c in items)

    if evidence_status is None:
        if not items:
            evidence_status = EVIDENCE_INCOMPLETE
        elif hard_unknown:
            evidence_status = EVIDENCE_INCOMPLETE
        elif all_pass and all(c.evidence for c in items if c.hard):
            evidence_status = EVIDENCE_COMPLETE
        else:
            evidence_status = EVIDENCE_INCOMPLETE
    if evidence_status not in VALID_EVIDENCE:
        raise ValueError(f"invalid evidence status: {evidence_status}")

    if task_status != TASK_COMPLETED:
        feasibility = FEAS_UNKNOWN
        recommendation = REC_DIAGNOSTIC
    elif hard_fail:
        feasibility = FEAS_INFEASIBLE
        recommendation = REC_DIAGNOSTIC
    elif hard_unknown or not items:
        feasibility = FEAS_UNKNOWN
        recommendation = REC_DIAGNOSTIC
    elif all_pass and evidence_status == EVIDENCE_COMPLETE and approved_profile:
        feasibility = FEAS_APPROVED
        recommendation = REC_ENGINEERING
    elif all_pass:
        feasibility = FEAS_MODEL
        recommendation = REC_ANALYSIS
    elif items and not hard_unknown:
        # 只有非硬约束（经验/建议）未通过时，仍保留“模型内可行”的提案，
        # 但证据不完整、推荐为分析级；不能把经验规则当成硬判决。
        feasibility = FEAS_MODEL
        recommendation = REC_ANALYSIS
    else:
        feasibility = FEAS_UNKNOWN
        recommendation = REC_DIAGNOSTIC

    return {
        "schema_version": SCHEMA_VERSION,
        "task_status": task_status,
        "feasibility_status": feasibility,
        "evidence_status": evidence_status,
        "recommendation_status": recommendation,
        "constraints": [c.to_dict() for c in items],
        "summary": {
            "total": len(items),
            "pass": statuses.count(STATUS_PASS),
            "fail": statuses.count(STATUS_FAIL),
            "unknown": statuses.count(STATUS_UNKNOWN),
            "hard_fail": hard_fail,
            "hard_unknown": hard_unknown,
        },
    }


def constraint_from_dict(data: dict[str, Any]) -> ConstraintResult:
    evidence = [EvidenceRef(**e) for e in data.get("evidence", [])]
    return ConstraintResult(
        constraint_id=str(data["constraint_id"]),
        quantity=str(data["quantity"]),
        status=str(data["status"]),
        value=data.get("value"),
        limit=data.get("limit"),
        unit=data.get("unit"),
        hard=bool(data.get("hard", True)),
        reason_code=data.get("reason_code"),
        note=data.get("note"),
        evidence=evidence,
        approved_threshold_ref=data.get("approved_threshold_ref"),
    )


def assert_valid_result(result: dict[str, Any]) -> None:
    """供测试/契约层校验状态字段，不接受旧版 ok 作为合格替代。"""
    for key, valid in (
        ("task_status", VALID_TASK),
        ("feasibility_status", VALID_FEASIBILITY),
        ("evidence_status", VALID_EVIDENCE),
        ("recommendation_status", VALID_RECOMMENDATION),
    ):
        value = result.get(key)
        if value not in valid:
            raise ValueError(f"{key} invalid: {value!r}")
    if result["recommendation_status"] == REC_VERIFIED:
        if result.get("feasibility_status") != FEAS_APPROVED:
            raise ValueError("verified recommendation requires approved-profile feasibility")
        if result.get("evidence_status") != EVIDENCE_COMPLETE:
            raise ValueError("verified recommendation requires complete evidence")


__all__ = [
    "SCHEMA_VERSION", "STATUS_PASS", "STATUS_FAIL", "STATUS_UNKNOWN",
    "TASK_COMPLETED", "TASK_FAILED", "TASK_CANCELLED",
    "FEAS_INFEASIBLE", "FEAS_UNKNOWN", "FEAS_MODEL", "FEAS_APPROVED",
    "EVIDENCE_COMPLETE", "EVIDENCE_INCOMPLETE", "EVIDENCE_INVALID",
    "REC_DIAGNOSTIC", "REC_ANALYSIS", "REC_ENGINEERING", "REC_VERIFIED",
    "EvidenceRef", "ConstraintResult", "aggregate_constraints",
    "constraint_from_dict", "assert_valid_result",
]

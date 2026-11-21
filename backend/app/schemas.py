from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Scenario(StrEnum):
    HEALTHY = "healthy"
    OPERATIONAL_FAILURE = "operational_failure"
    INCORRECT_JOIN = "incorrect_join"


class IncidentKind(StrEnum):
    OPERATIONAL = "operational_failure"
    SILENT_DATA = "silent_logical_failure"


class RiskLevel(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class QualityMetric(BaseModel):
    dimension: str
    score: float = Field(ge=0, le=1)
    passed: bool
    observed: str
    expected: str
    detail: str


class Evidence(BaseModel):
    evidence_id: str
    kind: str
    claim: str
    observed: str
    source: str
    collected_at: datetime


class Hypothesis(BaseModel):
    cause: str
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[str]


class ActionProposal(BaseModel):
    proposal_id: str
    incident_id: str
    title: str
    sql: str
    artifact_type: str = "dbt_sql"
    risk: RiskLevel
    blast_radius: str
    reversibility: str
    expected_benefit: str
    confidence: float = Field(ge=0, le=1)
    required_approval: bool
    rollback_plan: str
    status: str
    sql_sha256: str


class ValidationResult(BaseModel):
    validation_id: str
    proposal_id: str
    passed: bool
    before_score: float
    after_score: float
    row_count_before: int
    row_count_after: int
    checks: dict[str, bool]
    message: str


class RCAResult(BaseModel):
    incident_id: str
    probable_root_cause: str
    confidence: float = Field(ge=0, le=1)
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    recommended_action: ActionProposal | None = None
    agent_mode: str = "deterministic-mock"


class DemoRequest(BaseModel):
    scenario: Scenario = Scenario.INCORRECT_JOIN


class ApprovalRequest(BaseModel):
    decision: ApprovalStatus
    reviewer: str | None = Field(default=None, min_length=2, max_length=80, description="Deprecated display hint; reviewer identity is derived from the verified credential.")
    reason: str = Field(min_length=3, max_length=500)


class AgentDefinition(BaseModel):
    name: str
    responsibility: str
    inputs: list[str]
    outputs: list[str]
    tools: list[str]
    permission_boundary: str
    failure_behavior: str


class WorkflowResult(BaseModel):
    run_id: str
    scenario: Scenario
    pipeline_status: str
    quality_score: float | None
    incident_id: str | None
    incident_kind: IncidentKind | None
    rca: RCAResult | None
    validation: ValidationResult | None
    approval_status: ApprovalStatus | None
    workflow_states: list[str]
    created_at: datetime


class ExecuteResponse(BaseModel):
    execution_id: str
    proposal_id: str
    status: str
    message: str
    verified_quality_score: float


class Overview(BaseModel):
    pipeline_runs: int
    successful_runs: int
    operational_failures: int
    silent_failures: int
    open_incidents: int
    pending_approvals: int
    latest_quality_score: float | None
    rca_accuracy: float
    anomaly_recall: float
    false_positive_rate: float
    quality_improvement: float
    mttd_seconds: float
    mttr_seconds: float
    llm_mode: str
    recent_runs: list[dict[str, Any]]

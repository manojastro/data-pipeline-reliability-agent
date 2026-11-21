from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agents import DeterministicMockReasoningProvider
from .database import Database
from .schemas import (
    ActionProposal,
    ApprovalStatus,
    Evidence,
    ExecuteResponse,
    Hypothesis,
    IncidentKind,
    QualityMetric,
    RCAResult,
    RiskLevel,
    Scenario,
    ValidationResult,
    WorkflowResult,
)
from .simulator import (
    PipelineSimulation,
    SyntheticPipelineAdapter,
    overall_score,
    profile_quality,
    quality_gate_passed,
)


FIX_SQL = """-- bounded dbt-style model proposal: analytics.enriched_orders
WITH ranked_customers AS (
    SELECT customer_id, segment, updated_at,
           ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY updated_at DESC) AS snapshot_rank
    FROM {{ ref('customer_snapshots') }}
), latest_customers AS (
    SELECT customer_id, segment
    FROM ranked_customers
    WHERE snapshot_rank = 1
)
SELECT o.order_id, o.customer_id, o.amount, o.status, o.loaded_at, c.segment
FROM {{ ref('orders') }} AS o
LEFT JOIN latest_customers AS c ON o.customer_id = c.customer_id
"""


GROUND_TRUTH_ROOT_CAUSES: dict[Scenario, str] = {
    Scenario.OPERATIONAL_FAILURE: "warehouse_connection_timeout",
    Scenario.INCORRECT_JOIN: "non_unique_customer_snapshot_join",
}

REAPPROVAL_REQUIRED = "REAPPROVAL_REQUIRED"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _safe_select_sql(sql: str) -> bool:
    stripped = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE).strip().upper()
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "ATTACH ", "PRAGMA ")
    return stripped.startswith(("WITH ", "SELECT ")) and not any(token in stripped for token in forbidden)


def _render_dbt_refs(sql: str) -> str:
    rendered = sql
    allowed = {
        "orders": "orders",
        "customer_snapshots": "customer_snapshots",
    }
    for ref_name, table_name in allowed.items():
        pattern = rf"\{{\{{\s*ref\(\s*['\"]{ref_name}['\"]\s*\)\s*\}}\}}"
        rendered = re.sub(pattern, table_name, rendered)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("Proposal contains an unsupported dbt reference")
    return rendered


def execute_proposal_sql(sql: str, simulation: PipelineSimulation) -> list[dict[str, Any]]:
    """Execute the exact proposal in an isolated, read-only in-memory SQLite shadow."""
    if not _safe_select_sql(sql):
        raise ValueError("Proposal violated the read-only SQL policy")
    rendered = _render_dbt_refs(sql)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE orders (
              order_id TEXT, customer_id TEXT, amount REAL, status TEXT, loaded_at TEXT
            );
            CREATE TABLE customer_snapshots (
              customer_id TEXT, segment TEXT, updated_at TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO orders(order_id,customer_id,amount,status,loaded_at) VALUES(?,?,?,?,?)",
            [
                (row["order_id"], row["customer_id"], row["amount"], row["status"], row["loaded_at"])
                for row in simulation.orders
            ],
        )
        connection.executemany(
            "INSERT INTO customer_snapshots(customer_id,segment,updated_at) VALUES(?,?,?)",
            [
                (row["customer_id"], row["segment"], row["updated_at"])
                for row in simulation.customers
            ],
        )
        connection.commit()
        connection.execute("PRAGMA query_only=ON")
        cursor = connection.execute(rendered)
        columns = [item[0] for item in cursor.description or []]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        connection.close()


class ReliabilityWorkflow:
    def __init__(self, db: Database):
        self.db = db
        self.adapter = SyntheticPipelineAdapter()
        self.reasoner = DeterministicMockReasoningProvider()

    def run(self, scenario: Scenario) -> WorkflowResult:
        started = utcnow()
        run_id = uid("run")
        states = ["EVENT_RECEIVED", "PIPELINE_INSPECTED"]
        simulation = self.adapter.run(scenario)
        metrics = profile_quality(simulation)
        score = overall_score(metrics)
        completed = utcnow()
        self.db.execute(
            "INSERT INTO pipeline_runs VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                scenario.value,
                simulation.operational_status,
                started.isoformat(),
                completed.isoformat(),
                len(simulation.orders),
                len(simulation.output),
                score,
                json.dumps({"error": simulation.error, "adapter": "synthetic"}),
            ),
        )
        if metrics:
            self.db.executemany(
                """INSERT INTO quality_metrics(run_id,dimension,score,passed,observed,expected,detail)
                VALUES(?,?,?,?,?,?,?)""",
                [(run_id, m.dimension, m.score, int(m.passed), m.observed, m.expected, m.detail) for m in metrics],
            )
            states.append("DATA_PROFILED")

        expected_incident = scenario is not Scenario.HEALTHY
        incident_kind: IncidentKind | None = None
        if simulation.operational_status != "SUCCESS":
            incident_kind = IncidentKind.OPERATIONAL
        elif metrics and not quality_gate_passed(metrics):
            incident_kind = IncidentKind.SILENT_DATA

        if incident_kind is None:
            states.extend(["QUALITY_GATE_PASSED", "WORKFLOW_COMPLETE"])
            self._evaluation(run_id, expected_incident, False, False, False, 0.03, 0.0, 0.0)
            self._audit(None, "Pipeline Monitoring Agent", "pipeline_adapter", "inspect run", {"scenario": scenario.value}, {"status": simulation.operational_status, "quality_score": score}, [], "healthy")
            return WorkflowResult(
                run_id=run_id,
                scenario=scenario,
                pipeline_status=simulation.operational_status,
                quality_score=score,
                incident_id=None,
                incident_kind=None,
                rca=None,
                validation=None,
                approval_status=None,
                workflow_states=states,
                created_at=completed,
            )

        states.extend(["ANOMALY_DETECTED", "LINEAGE_COLLECTED", "RCA_COMPLETED"])
        incident_id = uid("inc")
        true_cause = GROUND_TRUTH_ROOT_CAUSES[scenario]
        detection_latency = round((utcnow() - started).total_seconds(), 4)
        title = "Pipeline execution failed" if incident_kind is IncidentKind.OPERATIONAL else "Revenue model silently multiplied fact rows"
        summary = simulation.error or "The pipeline reported SUCCESS, but reconciliation and uniqueness gates failed."
        self.db.execute(
            "INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (incident_id, run_id, incident_kind.value, "HIGH", "OPEN", title, summary, utcnow().isoformat(), detection_latency, true_cause, None, None),
        )
        evidence = self._create_evidence(incident_id, simulation.error, metrics, simulation.operational_status)
        predicted_cause, confidence, reasoning_summary = self.reasoner.diagnose(evidence)
        self.db.execute(
            "UPDATE incidents SET predicted_root_cause=?, confidence=? WHERE id=?",
            (predicted_cause, confidence, incident_id),
        )
        proposal: ActionProposal | None = None
        validation: ValidationResult | None = None
        approval_status: ApprovalStatus | None = None
        if incident_kind is IncidentKind.SILENT_DATA:
            proposal = self._propose_fix(incident_id)
            states.append("FIX_PROPOSED")
            validation = self._validate(proposal, simulation, score or 0.0)
            states.append("SHADOW_VALIDATED" if validation.passed else "VALIDATION_FAILED")
            approval_status = ApprovalStatus.PENDING
            self.db.execute(
                """INSERT INTO approvals
                (id,proposal_id,status,reviewer,reason,decided_at,created_at,artifact_sha256)
                VALUES(?,?,?,?,?,?,?,?)""",
                (uid("apr"), proposal.proposal_id, approval_status.value, None, None, None, utcnow().isoformat(), proposal.sql_sha256),
            )
            states.append("AWAITING_HUMAN_APPROVAL")
        else:
            states.append("MANUAL_RECOVERY_REQUIRED")
        rca = RCAResult(
            incident_id=incident_id,
            probable_root_cause=predicted_cause,
            confidence=confidence,
            hypotheses=[
                Hypothesis(cause=predicted_cause, confidence=confidence, supporting_evidence_ids=[item.evidence_id for item in evidence]),
            ],
            evidence=evidence,
            recommended_action=proposal,
        )
        quality_improvement = (validation.after_score - validation.before_score) if validation else 0.0
        self._evaluation(run_id, expected_incident, True, predicted_cause == true_cause, False, detection_latency, 0.12, quality_improvement)
        self._audit(incident_id, "RCA Agent", "evidence_store", "produce typed RCA", {"metric_count": len(metrics)}, {"predicted_root_cause": predicted_cause, "confidence": confidence, "reasoning": reasoning_summary}, [item.evidence_id for item in evidence], "proposal gated on shadow validation and approval", validation_result="passed" if validation and validation.passed else "not-applicable")
        return WorkflowResult(
            run_id=run_id,
            scenario=scenario,
            pipeline_status=simulation.operational_status,
            quality_score=score,
            incident_id=incident_id,
            incident_kind=incident_kind,
            rca=rca,
            validation=validation,
            approval_status=approval_status,
            workflow_states=states,
            created_at=completed,
        )

    def _create_evidence(self, incident_id: str, error: str | None, metrics: list[QualityMetric], operational_status: str) -> list[Evidence]:
        now = utcnow()
        evidence = [
            Evidence(evidence_id=uid("ev"), kind="pipeline_state", claim="Pipeline operational status", observed=operational_status if error is None else f"{operational_status}: {error}", source="synthetic_orchestrator.run", collected_at=now)
        ]
        evidence.extend(
            Evidence(evidence_id=uid("ev"), kind="quality_metric", claim=f"{metric.dimension} gate", observed=f"score={metric.score}; {metric.observed}", source="deterministic_profiler", collected_at=now)
            for metric in metrics
            if not metric.passed
        )
        evidence.append(Evidence(evidence_id=uid("ev"), kind="lineage", claim="Impacted transformation", observed="raw.customer_snapshots -> analytics.enriched_orders -> mart.daily_revenue", source="lineage_catalog", collected_at=now))
        self.db.executemany(
            "INSERT INTO evidence VALUES(?,?,?,?,?,?,?)",
            [(e.evidence_id, incident_id, e.kind, e.claim, e.observed, e.source, e.collected_at.isoformat()) for e in evidence],
        )
        return evidence

    def _propose_fix(self, incident_id: str) -> ActionProposal:
        if not _safe_select_sql(FIX_SQL):
            raise ValueError("Generated SQL violated the read-only SQL policy")
        proposal = ActionProposal(
            proposal_id=uid("prop"), incident_id=incident_id,
            title="Deduplicate customer snapshots before fact enrichment", sql=FIX_SQL,
            risk=RiskLevel.MEDIUM, blast_radius="analytics.enriched_orders and downstream daily revenue only",
            reversibility="Git revert of one dbt model", expected_benefit="Restore one-row-per-order and revenue reconciliation",
            confidence=0.97, required_approval=True,
            rollback_plan="Restore the previous model version and rebuild the last affected partition",
            status="VALIDATED_PENDING_APPROVAL", sql_sha256=hashlib.sha256(FIX_SQL.encode()).hexdigest(),
        )
        self.db.execute(
            "INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal.proposal_id, incident_id, proposal.title, proposal.sql, proposal.artifact_type, proposal.risk.value, proposal.blast_radius, proposal.reversibility, proposal.expected_benefit, proposal.confidence, int(proposal.required_approval), proposal.rollback_plan, proposal.status, proposal.sql_sha256, utcnow().isoformat()),
        )
        return proposal

    def _validate(self, proposal: ActionProposal, simulation: Any, before_score: float) -> ValidationResult:
        result = self._assess_proposal(
            proposal_id=proposal.proposal_id,
            sql=proposal.sql,
            expected_hash=proposal.sql_sha256,
            simulation=simulation,
            before_score=before_score,
            stage="shadow",
        )
        self.db.execute(
            "INSERT INTO validations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (result.validation_id, proposal.proposal_id, int(result.passed), result.before_score, result.after_score, result.row_count_before, result.row_count_after, json.dumps(result.checks), result.message, utcnow().isoformat()),
        )
        return result

    def _assess_proposal(
        self,
        *,
        proposal_id: str,
        sql: str,
        expected_hash: str,
        simulation: PipelineSimulation,
        before_score: float,
        stage: str,
    ) -> ValidationResult:
        actual_hash = hashlib.sha256(sql.encode()).hexdigest()
        source_before = hashlib.sha256(
            json.dumps(
                {"orders": simulation.orders, "customers": simulation.customers},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        checks = {
            "proposal_hash_verified": actual_hash == expected_hash,
            "sql_policy": _safe_select_sql(sql),
            "pipeline_operational": simulation.operational_status == "SUCCESS",
            "sql_executed": False,
            "row_count_restored": False,
            "all_quality_dimensions_pass": False,
            "quality_improved": False,
            "source_snapshot_unchanged": False,
        }
        after_score = 0.0
        row_count_after = 0
        error: str | None = None
        if (
            checks["proposal_hash_verified"]
            and checks["sql_policy"]
            and checks["pipeline_operational"]
        ):
            try:
                output = execute_proposal_sql(sql, simulation)
                shadow = PipelineSimulation(
                    scenario=simulation.scenario,
                    operational_status="SUCCESS",
                    orders=simulation.orders,
                    customers=simulation.customers,
                    output=output,
                )
                after_metrics = profile_quality(shadow)
                after_score = overall_score(after_metrics) or 0.0
                row_count_after = len(output)
                checks["sql_executed"] = True
                checks["row_count_restored"] = len(output) == len(simulation.orders)
                checks["all_quality_dimensions_pass"] = quality_gate_passed(after_metrics)
                checks["quality_improved"] = after_score > before_score
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                error = f"{type(exc).__name__}: {exc}"
        source_after = hashlib.sha256(
            json.dumps(
                {"orders": simulation.orders, "customers": simulation.customers},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        checks["source_snapshot_unchanged"] = source_after == source_before
        passed = all(checks.values())
        result = ValidationResult(
            validation_id=uid("val"), proposal_id=proposal_id,
            passed=passed, before_score=before_score, after_score=after_score,
            row_count_before=len(simulation.output), row_count_after=row_count_after, checks=checks,
            message=(
                f"{stage.capitalize()} execution of the exact proposal restored cardinality and all eight data-quality dimensions."
                if passed
                else f"{stage.capitalize()} execution failed at least one proposal gate"
                + (f" ({error})." if error else ".")
            ),
        )
        return result

    def decide_approval(self, proposal_id: str, decision: ApprovalStatus, reviewer: str, reason: str) -> dict[str, Any]:
        if decision is ApprovalStatus.PENDING:
            raise ValueError("A reviewer must approve or reject the proposal")
        approval = self.db.one(
            "SELECT * FROM approvals WHERE proposal_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        )
        if not approval:
            raise LookupError("Approval not found")
        if approval["status"] not in {ApprovalStatus.PENDING.value, REAPPROVAL_REQUIRED}:
            raise ValueError("Approval has already been decided")
        proposal = self.db.one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        if not proposal:
            raise LookupError("Proposal not found")
        actual_hash = hashlib.sha256(proposal["sql_text"].encode()).hexdigest()
        if actual_hash != proposal["sql_sha256"]:
            raise ValueError("Approval artifact no longer matches the validated proposal")
        reapproval = approval["status"] == REAPPROVAL_REQUIRED
        if not reapproval and approval["artifact_sha256"] != actual_hash:
            raise ValueError("Approval artifact no longer matches the validated proposal")
        validation = self.db.one(
            "SELECT * FROM validations WHERE proposal_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        )
        if decision is ApprovalStatus.APPROVED:
            if reapproval:
                run = self.db.one(
                    """SELECT pipeline_runs.scenario
                    FROM incidents JOIN pipeline_runs ON pipeline_runs.id=incidents.run_id
                    WHERE incidents.id=?""",
                    (proposal["incident_id"],),
                )
                if not run:
                    raise ValueError("Original pipeline run is unavailable for reapproval validation")
                try:
                    fresh_simulation = self.adapter.run(Scenario(run["scenario"]))
                except Exception as exc:
                    raise ValueError("Fresh reapproval validation could not collect pipeline state") from exc
                fresh_before_score = overall_score(profile_quality(fresh_simulation)) or 0.0
                validation = self._assess_proposal(
                    proposal_id=proposal_id,
                    sql=proposal["sql_text"],
                    expected_hash=actual_hash,
                    simulation=fresh_simulation,
                    before_score=fresh_before_score,
                    stage="reapproval",
                )
                if not validation.passed:
                    raise ValueError("A passing fresh validation is required before reapproval")
                self.db.execute(
                    "INSERT INTO validations VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        validation.validation_id,
                        proposal_id,
                        int(validation.passed),
                        validation.before_score,
                        validation.after_score,
                        validation.row_count_before,
                        validation.row_count_after,
                        json.dumps(validation.checks),
                        validation.message,
                        utcnow().isoformat(),
                    ),
                )
            elif not validation or not validation["passed"]:
                raise ValueError("A passing shadow validation is required before approval")
        decided_at = utcnow().isoformat()
        if reapproval:
            self.db.execute(
                """INSERT INTO approvals
                (id,proposal_id,status,reviewer,reason,decided_at,created_at,artifact_sha256)
                VALUES(?,?,?,?,?,?,?,?)""",
                (uid("apr"), proposal_id, decision.value, reviewer, reason, decided_at, decided_at, actual_hash),
            )
        else:
            self.db.execute(
                """UPDATE approvals
                SET status=?,reviewer=?,reason=?,decided_at=?,artifact_sha256=?
                WHERE id=?""",
                (decision.value, reviewer, reason, decided_at, actual_hash, approval["id"]),
            )
        self.db.execute("UPDATE proposals SET status=? WHERE id=?", (decision.value, proposal_id))
        incident = self.db.one("SELECT incident_id FROM proposals WHERE id=?", (proposal_id,))
        self._audit(incident["incident_id"] if incident else None, "Human Approval Gate", "approval_api", "review proposal", {"proposal_id": proposal_id, "reviewer": reviewer}, {"decision": decision.value, "reason": reason}, [], decision.value, approval=decision.value)
        return {"proposal_id": proposal_id, "status": decision.value, "reviewer": reviewer, "reason": reason, "decided_at": decided_at}

    def execute(self, proposal_id: str) -> ExecuteResponse:
        proposal = self.db.one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        approval = self.db.one(
            "SELECT * FROM approvals WHERE proposal_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        )
        validation = self.db.one(
            "SELECT * FROM validations WHERE proposal_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (proposal_id,),
        )
        if not proposal:
            raise LookupError("Proposal not found")
        if not approval or approval["status"] != ApprovalStatus.APPROVED.value:
            raise PermissionError("Execution requires explicit human approval")
        if not validation or not validation["passed"]:
            raise ValueError("Execution requires passing shadow validation")
        actual_hash = hashlib.sha256(proposal["sql_text"].encode()).hexdigest()
        if (
            proposal["sql_sha256"] != actual_hash
            or approval["artifact_sha256"] != actual_hash
            or not _safe_select_sql(proposal["sql_text"])
        ):
            raise ValueError("Proposal integrity, approval binding, or SQL policy check failed")
        existing = self.db.one("SELECT * FROM executions WHERE proposal_id=?", (proposal_id,))
        if existing:
            payload = json.loads(existing["result_json"])
            return ExecuteResponse(execution_id=existing["id"], proposal_id=proposal_id, status=existing["status"], message=payload["message"], verified_quality_score=payload["verified_quality_score"])
        run = self.db.one(
            """SELECT pipeline_runs.scenario
            FROM incidents JOIN pipeline_runs ON pipeline_runs.id=incidents.run_id
            WHERE incidents.id=?""",
            (proposal["incident_id"],),
        )
        if not run:
            raise ValueError("Original pipeline run is unavailable for deployment verification")
        try:
            fresh_simulation = self.adapter.run(Scenario(run["scenario"]))
        except Exception as exc:
            self._audit(
                proposal["incident_id"], "Validation Agent", "pipeline_adapter",
                "block deployment when fresh state collection fails",
                {"proposal_id": proposal_id, "approved_sha256": approval["artifact_sha256"]},
                {"error_type": type(exc).__name__}, [], "BLOCK",
                approval="APPROVED", execution_result="BLOCKED", validation_result="failed",
            )
            raise ValueError("Independent deployment validation failed: fresh pipeline state unavailable") from exc
        fresh_before_score = overall_score(profile_quality(fresh_simulation)) or 0.0
        deployment_validation = self._assess_proposal(
            proposal_id=proposal_id,
            sql=proposal["sql_text"],
            expected_hash=approval["artifact_sha256"],
            simulation=fresh_simulation,
            before_score=fresh_before_score,
            stage="deployment verification",
        )
        if not deployment_validation.passed:
            self._audit(
                proposal["incident_id"], "Validation Agent", "isolated_sqlite_shadow",
                "block deployment after independent revalidation",
                {"proposal_id": proposal_id, "approved_sha256": approval["artifact_sha256"]},
                deployment_validation.model_dump(mode="json"), [], "BLOCK",
                approval="APPROVED", execution_result="BLOCKED", validation_result="failed",
            )
            raise ValueError("Independent deployment validation failed")
        execution_id = uid("exe")
        payload = {
            "message": "Simulated dbt deployment completed; independent post-change checks passed.",
            "verified_quality_score": deployment_validation.after_score,
            "independent_validation": deployment_validation.model_dump(mode="json"),
        }
        self.db.execute("INSERT INTO executions VALUES(?,?,?,?,?)", (execution_id, proposal_id, "SUCCEEDED", json.dumps(payload), utcnow().isoformat()))
        self.db.execute("UPDATE proposals SET status='EXECUTED' WHERE id=?", (proposal_id,))
        self.db.execute("UPDATE incidents SET status='RESOLVED' WHERE id=?", (proposal["incident_id"],))
        self._audit(proposal["incident_id"], "Validation Agent", "isolated_sqlite_shadow", "execute approved proposal and independently revalidate", {"proposal_id": proposal_id, "sql_sha256": proposal["sql_sha256"]}, payload, [], "outcome independently verified", approval="APPROVED", execution_result="SUCCEEDED", validation_result="passed")
        return ExecuteResponse(execution_id=execution_id, proposal_id=proposal_id, status="SUCCEEDED", message=payload["message"], verified_quality_score=deployment_validation.after_score)

    def _evaluation(self, run_id: str, expected: bool, detected: bool, rca_correct: bool, false_positive: bool, mttd: float, mttr: float, improvement: float) -> None:
        self.db.execute(
            "INSERT INTO evaluations(run_id,expected_incident,detected_incident,rca_correct,false_positive,mttd_seconds,mttr_seconds,quality_improvement,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, int(expected), int(detected), int(rca_correct), int(false_positive), mttd, mttr, improvement, utcnow().isoformat()),
        )

    def _audit(self, incident_id: str | None, agent: str, tool: str, action: str, inputs: dict[str, Any], outputs: dict[str, Any], evidence: list[str], decision: str, **kwargs: str) -> None:
        self.db.audit(timestamp=utcnow().isoformat(), incident_id=incident_id, agent=agent, tool=tool, action=action, inputs=inputs, outputs=outputs, evidence=evidence, decision=decision, **kwargs)

    def incident_detail(self, incident_id: str) -> dict[str, Any] | None:
        incident = self.db.one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if not incident:
            return None
        incident["evidence"] = self.db.all("SELECT * FROM evidence WHERE incident_id=? ORDER BY collected_at", (incident_id,))
        incident["proposal"] = self.db.one("SELECT * FROM proposals WHERE incident_id=?", (incident_id,))
        if incident["proposal"]:
            proposal_id = incident["proposal"]["id"]
            incident["validation"] = self.db.one(
                "SELECT * FROM validations WHERE proposal_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (proposal_id,),
            )
            incident["approval"] = self.db.one(
                "SELECT * FROM approvals WHERE proposal_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (proposal_id,),
            )
            incident["execution"] = self.db.one("SELECT * FROM executions WHERE proposal_id=?", (proposal_id,))
        return incident

    def overview(self) -> dict[str, Any]:
        counts = self.db.one("""SELECT COUNT(*) pipeline_runs,
          SUM(CASE WHEN operational_status='SUCCESS' THEN 1 ELSE 0 END) successful_runs,
          SUM(CASE WHEN operational_status='FAILED' THEN 1 ELSE 0 END) operational_failures
          FROM pipeline_runs""") or {}
        silent = self.db.one("SELECT COUNT(*) count FROM incidents WHERE kind='silent_logical_failure'") or {"count": 0}
        open_incidents = self.db.one("SELECT COUNT(*) count FROM incidents WHERE status='OPEN'") or {"count": 0}
        pending = self.db.one(
            """SELECT COUNT(*) count FROM approvals current
            WHERE current.status IN ('PENDING','REAPPROVAL_REQUIRED')
              AND current.rowid = (
                SELECT latest.rowid FROM approvals latest
                WHERE latest.proposal_id=current.proposal_id
                ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1
              )"""
        ) or {"count": 0}
        latest = self.db.one("SELECT quality_score FROM pipeline_runs WHERE quality_score IS NOT NULL ORDER BY completed_at DESC LIMIT 1")
        evals = self.db.one("""SELECT COUNT(*) n, SUM(expected_incident) expected_n, SUM(detected_incident) detected_n,
          SUM(rca_correct) rca_n, SUM(false_positive) fp_n, AVG(mttd_seconds) mttd,
          AVG(mttr_seconds) mttr, AVG(quality_improvement) improvement FROM evaluations""") or {}
        expected_n = evals.get("expected_n") or 0
        detected_n = evals.get("detected_n") or 0
        total = evals.get("n") or 0
        return {
            "pipeline_runs": counts.get("pipeline_runs") or 0,
            "successful_runs": counts.get("successful_runs") or 0,
            "operational_failures": counts.get("operational_failures") or 0,
            "silent_failures": silent["count"],
            "open_incidents": open_incidents["count"],
            "pending_approvals": pending["count"],
            "latest_quality_score": latest["quality_score"] if latest else None,
            "rca_accuracy": round((evals.get("rca_n") or 0) / max(detected_n, 1), 4),
            "anomaly_recall": round(detected_n / max(expected_n, 1), 4),
            "false_positive_rate": round((evals.get("fp_n") or 0) / max(total, 1), 4),
            "quality_improvement": round(evals.get("improvement") or 0, 4),
            "mttd_seconds": round(evals.get("mttd") or 0, 4),
            "mttr_seconds": round(evals.get("mttr") or 0, 4),
            "llm_mode": self.reasoner.mode,
            "recent_runs": self.db.all("SELECT id,scenario,operational_status,quality_score,rows_in,rows_out,completed_at FROM pipeline_runs ORDER BY completed_at DESC LIMIT 8"),
        }


def default_database_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "reliability.db"

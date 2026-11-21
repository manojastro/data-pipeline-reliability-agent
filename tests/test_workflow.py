import sqlite3
import hashlib

import pytest

from backend.app.database import Database
from backend.app.schemas import ApprovalStatus, IncidentKind, Scenario
from backend.app.simulator import PipelineSimulation
import backend.app.workflow as workflow_module
from backend.app.workflow import GROUND_TRUTH_ROOT_CAUSES, ReliabilityWorkflow


@pytest.fixture()
def workflow(tmp_path):
    db = Database(tmp_path / "workflow.db")
    db.initialize()
    return ReliabilityWorkflow(db)


def test_silent_failure_produces_grounded_rca_and_passing_shadow_validation(workflow):
    result = workflow.run(Scenario.INCORRECT_JOIN)

    assert result.pipeline_status == "SUCCESS"
    assert result.incident_kind is IncidentKind.SILENT_DATA
    assert result.rca is not None
    assert result.rca.probable_root_cause == "non_unique_customer_snapshot_join"
    assert result.rca.confidence == pytest.approx(0.97)
    assert len(result.rca.evidence) >= 4
    assert all(h.supporting_evidence_ids for h in result.rca.hypotheses[:1])
    assert result.validation is not None and result.validation.passed
    assert result.validation.after_score == 1.0
    assert result.validation.after_score > result.validation.before_score
    assert result.approval_status is ApprovalStatus.PENDING


def test_execution_is_blocked_until_approved_then_is_idempotent(workflow):
    result = workflow.run(Scenario.INCORRECT_JOIN)
    proposal_id = result.rca.recommended_action.proposal_id

    with pytest.raises(PermissionError):
        workflow.execute(proposal_id)

    workflow.decide_approval(proposal_id, ApprovalStatus.APPROVED, "test-operator", "All shadow checks passed")
    first = workflow.execute(proposal_id)
    second = workflow.execute(proposal_id)

    assert first.status == "SUCCEEDED"
    assert first.verified_quality_score == 1.0
    assert second.execution_id == first.execution_id
    incident = workflow.incident_detail(result.incident_id)
    assert incident["status"] == "RESOLVED"


def test_audit_log_is_immutable(workflow):
    workflow.run(Scenario.INCORRECT_JOIN)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        workflow.db.execute("DELETE FROM audit_events")


def test_operational_failure_gets_separate_incident_class(workflow):
    result = workflow.run(Scenario.OPERATIONAL_FAILURE)

    assert result.incident_kind is IncidentKind.OPERATIONAL
    assert result.pipeline_status == "FAILED"
    assert result.validation is None
    assert result.rca.probable_root_cause == "warehouse_connection_timeout"


def test_rca_prediction_is_independent_from_changed_ground_truth(workflow, monkeypatch):
    monkeypatch.setitem(
        GROUND_TRUTH_ROOT_CAUSES,
        Scenario.INCORRECT_JOIN,
        "upstream_schema_drift",
    )
    result = workflow.run(Scenario.INCORRECT_JOIN)
    assert result.rca.probable_root_cause == "non_unique_customer_snapshot_join"
    incident = workflow.db.one("SELECT true_root_cause,predicted_root_cause FROM incidents WHERE id=?", (result.incident_id,))
    assert incident == {
        "true_root_cause": "upstream_schema_drift",
        "predicted_root_cause": "non_unique_customer_snapshot_join",
    }
    evaluation = workflow.db.one("SELECT rca_correct FROM evaluations WHERE run_id=?", (result.run_id,))
    assert evaluation["rca_correct"] == 0


def test_wrong_but_read_only_sql_fails_exact_artifact_shadow_validation(workflow, monkeypatch):
    wrong_sql = """SELECT o.order_id, o.customer_id, o.amount, o.status, o.loaded_at, c.segment
    FROM {{ ref('orders') }} o
    LEFT JOIN {{ ref('customer_snapshots') }} c ON o.customer_id = c.customer_id"""
    monkeypatch.setattr(workflow_module, "FIX_SQL", wrong_sql)
    result = workflow.run(Scenario.INCORRECT_JOIN)
    assert result.validation is not None
    assert result.validation.passed is False
    assert result.validation.checks["sql_executed"] is True
    assert result.validation.checks["all_quality_dimensions_pass"] is False
    with pytest.raises(ValueError, match="passing shadow validation"):
        workflow.decide_approval(
            result.rca.recommended_action.proposal_id,
            ApprovalStatus.APPROVED,
            "verified-approver",
            "Should be blocked",
        )


def test_changing_sql_and_hash_after_approval_is_database_rejected(workflow):
    result = workflow.run(Scenario.INCORRECT_JOIN)
    proposal_id = result.rca.recommended_action.proposal_id
    workflow.decide_approval(proposal_id, ApprovalStatus.APPROVED, "verified-approver", "Exact artifact reviewed")
    wrong_sql = "SELECT order_id, customer_id, amount, status, loaded_at, 'unknown' AS segment FROM {{ ref('orders') }}"
    with pytest.raises(sqlite3.IntegrityError, match="approved proposal artifact is immutable"):
        workflow.db.execute(
            "UPDATE proposals SET sql_text=?,sql_sha256=? WHERE id=?",
            (wrong_sql, hashlib.sha256(wrong_sql.encode()).hexdigest(), proposal_id),
        )


def test_synchronized_triple_tamper_and_approved_binding_mutation_are_database_rejected(workflow):
    result = workflow.run(Scenario.INCORRECT_JOIN)
    proposal_id = result.rca.recommended_action.proposal_id
    workflow.decide_approval(proposal_id, ApprovalStatus.APPROVED, "verified-approver", "Exact artifact reviewed")
    changed_sql = result.rca.recommended_action.sql + "\n-- changed after approval"
    changed_hash = hashlib.sha256(changed_sql.encode()).hexdigest()

    with pytest.raises(sqlite3.IntegrityError, match="approved proposal artifact is immutable"):
        with workflow.db.connect() as connection:
            connection.execute(
                "UPDATE proposals SET sql_text=?,sql_sha256=? WHERE id=?",
                (changed_sql, changed_hash, proposal_id),
            )
            connection.execute(
                "UPDATE approvals SET artifact_sha256=? WHERE proposal_id=? AND status='APPROVED'",
                (changed_hash, proposal_id),
            )

    with pytest.raises(sqlite3.IntegrityError, match="approved approval binding is immutable"):
        workflow.db.execute(
            "UPDATE approvals SET artifact_sha256=? WHERE proposal_id=? AND status='APPROVED'",
            (changed_hash, proposal_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="approved approval binding is immutable"):
        workflow.db.execute(
            "UPDATE approvals SET id='apr_forged' WHERE proposal_id=? AND status='APPROVED'",
            (proposal_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="approved approval binding is immutable"):
        workflow.db.execute(
            "DELETE FROM approvals WHERE proposal_id=? AND status='APPROVED'",
            (proposal_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="approved proposal is immutable"):
        workflow.db.execute("DELETE FROM proposals WHERE id=?", (proposal_id,))
    assert workflow.execute(proposal_id).status == "SUCCEEDED"


def test_deployment_independently_reruns_against_fresh_source_state(workflow):
    result = workflow.run(Scenario.INCORRECT_JOIN)
    proposal_id = result.rca.recommended_action.proposal_id
    workflow.decide_approval(proposal_id, ApprovalStatus.APPROVED, "verified-approver", "Exact artifact reviewed")

    original = workflow.adapter.run(Scenario.INCORRECT_JOIN)
    changed_source = PipelineSimulation(
        scenario=Scenario.INCORRECT_JOIN,
        operational_status="SUCCESS",
        orders=original.orders,
        customers=[row for row in original.customers if row["customer_id"] != "c-001"],
        output=original.output,
    )

    class ChangedSourceAdapter:
        def run(self, scenario):
            return changed_source

    workflow.adapter = ChangedSourceAdapter()
    with pytest.raises(ValueError, match="Independent deployment validation failed"):
        workflow.execute(proposal_id)
    assert workflow.db.one("SELECT COUNT(*) count FROM executions")["count"] == 0
    assert workflow.db.one("SELECT status FROM incidents WHERE id=?", (result.incident_id,))["status"] == "OPEN"


def test_legacy_migration_invalidates_inferred_binding_until_fresh_reapproval(workflow):
    result = workflow.run(Scenario.INCORRECT_JOIN)
    proposal_id = result.rca.recommended_action.proposal_id
    workflow.decide_approval(
        proposal_id,
        ApprovalStatus.APPROVED,
        "legacy-reviewer",
        "Reviewed the original exact artifact",
    )
    changed_sql = result.rca.recommended_action.sql + "\n-- changed after the legacy review\n"
    with workflow.db.connect() as connection:
        connection.execute("DROP TRIGGER approved_proposal_artifact_no_update")
        connection.execute("DROP TRIGGER approved_proposal_no_delete")
        connection.execute("DROP TRIGGER approved_approval_no_update")
        connection.execute("DROP TRIGGER approved_approval_no_delete")
        connection.execute("ALTER TABLE approvals DROP COLUMN artifact_sha256")
        connection.execute(
            "DELETE FROM schema_migrations WHERE version IN "
            "('002_bind_approval_artifact','003_invalidate_legacy_approval_bindings',"
            "'004_immutable_approval_bindings')"
        )
        connection.execute(
            "UPDATE proposals SET sql_text=?,sql_sha256=? WHERE id=?",
            (changed_sql, hashlib.sha256(changed_sql.encode()).hexdigest(), proposal_id),
        )

    workflow.db.initialize()
    migrated = workflow.db.one("SELECT * FROM approvals WHERE proposal_id=?", (proposal_id,))
    assert migrated["status"] == "REAPPROVAL_REQUIRED"
    assert migrated["artifact_sha256"] is None
    with pytest.raises(PermissionError, match="explicit human approval"):
        workflow.execute(proposal_id)

    workflow.decide_approval(
        proposal_id,
        ApprovalStatus.APPROVED,
        "new-verified-reviewer",
        "Reviewed the changed artifact and fresh validation",
    )
    approvals = workflow.db.all(
        "SELECT * FROM approvals WHERE proposal_id=? ORDER BY created_at, rowid",
        (proposal_id,),
    )
    assert len(approvals) == 2
    assert approvals[0]["status"] == "REAPPROVAL_REQUIRED"
    assert approvals[0]["artifact_sha256"] is None
    with pytest.raises(sqlite3.IntegrityError, match="approved approval binding is immutable"):
        workflow.db.execute("DELETE FROM approvals WHERE id=?", (approvals[0]["id"],))
    rebound = approvals[-1]
    assert rebound["status"] == "APPROVED"
    assert rebound["artifact_sha256"] == hashlib.sha256(changed_sql.encode()).hexdigest()
    assert workflow.execute(proposal_id).status == "SUCCEEDED"
    assert workflow.overview()["pending_approvals"] == 0


def test_fresh_operational_failure_blocks_deployment_even_with_quality_rows(workflow):
    result = workflow.run(Scenario.INCORRECT_JOIN)
    proposal_id = result.rca.recommended_action.proposal_id
    workflow.decide_approval(proposal_id, ApprovalStatus.APPROVED, "verified-approver", "Exact artifact reviewed")
    healthy = workflow.adapter.run(Scenario.HEALTHY)
    failed_fresh_state = PipelineSimulation(
        scenario=Scenario.INCORRECT_JOIN,
        operational_status="FAILED",
        orders=healthy.orders,
        customers=healthy.customers,
        output=healthy.output,
        error="Fresh warehouse connection failed",
    )

    class FailedFreshAdapter:
        def run(self, scenario):
            return failed_fresh_state

    workflow.adapter = FailedFreshAdapter()
    with pytest.raises(ValueError, match="Independent deployment validation failed"):
        workflow.execute(proposal_id)
    assert workflow.db.one("SELECT COUNT(*) count FROM executions")["count"] == 0
    assert workflow.db.one("SELECT status FROM incidents WHERE id=?", (result.incident_id,))["status"] == "OPEN"


def test_fresh_adapter_timeout_blocks_deployment_and_is_audited(workflow):
    result = workflow.run(Scenario.INCORRECT_JOIN)
    proposal_id = result.rca.recommended_action.proposal_id
    workflow.decide_approval(proposal_id, ApprovalStatus.APPROVED, "verified-approver", "Exact artifact reviewed")

    class TimedOutFreshAdapter:
        def run(self, scenario):
            raise TimeoutError("Synthetic fresh-state timeout")

    workflow.adapter = TimedOutFreshAdapter()
    with pytest.raises(ValueError, match="fresh pipeline state unavailable"):
        workflow.execute(proposal_id)
    assert workflow.db.one("SELECT COUNT(*) count FROM executions")["count"] == 0
    assert workflow.db.one("SELECT status FROM incidents WHERE id=?", (result.incident_id,))["status"] == "OPEN"
    event = workflow.db.one(
        "SELECT decision,execution_result,validation_result FROM audit_events ORDER BY id DESC LIMIT 1"
    )
    assert event == {"decision": "BLOCK", "execution_result": "BLOCKED", "validation_result": "failed"}

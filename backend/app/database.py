from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_runs (
  id TEXT PRIMARY KEY, scenario TEXT NOT NULL, operational_status TEXT NOT NULL,
  started_at TEXT NOT NULL, completed_at TEXT NOT NULL, rows_in INTEGER NOT NULL,
  rows_out INTEGER NOT NULL, quality_score REAL, metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, dimension TEXT NOT NULL,
  score REAL NOT NULL, passed INTEGER NOT NULL, observed TEXT NOT NULL,
  expected TEXT NOT NULL, detail TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES pipeline_runs(id)
);
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
  status TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
  detected_at TEXT NOT NULL, detection_latency_seconds REAL NOT NULL,
  true_root_cause TEXT, predicted_root_cause TEXT, confidence REAL,
  FOREIGN KEY(run_id) REFERENCES pipeline_runs(id)
);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, kind TEXT NOT NULL, claim TEXT NOT NULL,
  observed TEXT NOT NULL, source TEXT NOT NULL, collected_at TEXT NOT NULL,
  FOREIGN KEY(incident_id) REFERENCES incidents(id)
);
CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT, upstream TEXT NOT NULL, downstream TEXT NOT NULL,
  transformation TEXT NOT NULL, UNIQUE(upstream, downstream)
);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, title TEXT NOT NULL, sql_text TEXT NOT NULL,
  artifact_type TEXT NOT NULL, risk TEXT NOT NULL, blast_radius TEXT NOT NULL,
  reversibility TEXT NOT NULL, expected_benefit TEXT NOT NULL, confidence REAL NOT NULL,
  required_approval INTEGER NOT NULL, rollback_plan TEXT NOT NULL, status TEXT NOT NULL,
  sql_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(incident_id) REFERENCES incidents(id)
);
CREATE TABLE IF NOT EXISTS validations (
  id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, passed INTEGER NOT NULL,
  before_score REAL NOT NULL, after_score REAL NOT NULL,
  row_count_before INTEGER NOT NULL, row_count_after INTEGER NOT NULL,
  checks_json TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(proposal_id) REFERENCES proposals(id)
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, status TEXT NOT NULL,
  reviewer TEXT, reason TEXT, decided_at TEXT, created_at TEXT NOT NULL,
  artifact_sha256 TEXT,
  FOREIGN KEY(proposal_id) REFERENCES proposals(id)
);
CREATE TABLE IF NOT EXISTS executions (
  id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, status TEXT NOT NULL,
  result_json TEXT NOT NULL, executed_at TEXT NOT NULL,
  FOREIGN KEY(proposal_id) REFERENCES proposals(id)
);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, expected_incident INTEGER NOT NULL,
  detected_incident INTEGER NOT NULL, rca_correct INTEGER NOT NULL, false_positive INTEGER NOT NULL,
  mttd_seconds REAL NOT NULL, mttr_seconds REAL NOT NULL, quality_improvement REAL NOT NULL,
  created_at TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES pipeline_runs(id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, project TEXT NOT NULL,
  incident_id TEXT, agent TEXT NOT NULL, tool TEXT NOT NULL, action TEXT NOT NULL,
  input_json TEXT NOT NULL, output_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
  decision TEXT NOT NULL, approval TEXT, execution_result TEXT, validation_result TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
"""


APPROVAL_IMMUTABILITY = """
CREATE TRIGGER IF NOT EXISTS approved_proposal_artifact_no_update
BEFORE UPDATE OF sql_text, sql_sha256 ON proposals
WHEN EXISTS (
  SELECT 1 FROM approvals
  WHERE approvals.proposal_id=OLD.id AND approvals.status='APPROVED'
)
BEGIN SELECT RAISE(ABORT, 'approved proposal artifact is immutable'); END;

CREATE TRIGGER IF NOT EXISTS approved_proposal_no_delete
BEFORE DELETE ON proposals
WHEN EXISTS (
  SELECT 1 FROM approvals
  WHERE approvals.proposal_id=OLD.id AND approvals.status='APPROVED'
)
BEGIN SELECT RAISE(ABORT, 'approved proposal is immutable'); END;

CREATE TRIGGER IF NOT EXISTS approved_approval_no_update
BEFORE UPDATE ON approvals
WHEN OLD.status IN ('APPROVED','REAPPROVAL_REQUIRED')
BEGIN SELECT RAISE(ABORT, 'approved approval binding is immutable'); END;

CREATE TRIGGER IF NOT EXISTS approved_approval_no_delete
BEFORE DELETE ON approvals
WHEN OLD.status IN ('APPROVED','REAPPROVAL_REQUIRED')
BEGIN SELECT RAISE(ABORT, 'approved approval binding is immutable'); END;
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES('001_initial', datetime('now'))"
            )
            approval_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
            }
            if "artifact_sha256" not in approval_columns:
                connection.execute("ALTER TABLE approvals ADD COLUMN artifact_sha256 TEXT")
            applied_migrations = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            if "002_bind_approval_artifact" not in applied_migrations:
                # A legacy approval did not record which bytes the reviewer saw. Never
                # infer that trust from the proposal's current (possibly changed) hash.
                connection.execute(
                    "UPDATE approvals SET status='REAPPROVAL_REQUIRED', artifact_sha256=NULL"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES('002_bind_approval_artifact', datetime('now'))"
                )
            if "003_invalidate_legacy_approval_bindings" not in applied_migrations:
                # Version 002 in early POC databases backfilled the current proposal
                # hash. Invalidate those unverifiable bindings exactly once as well.
                connection.execute(
                    "UPDATE approvals SET status='REAPPROVAL_REQUIRED', artifact_sha256=NULL"
                )
                connection.execute(
                    """UPDATE proposals SET status='REAPPROVAL_REQUIRED'
                    WHERE id IN (SELECT proposal_id FROM approvals
                                 WHERE status='REAPPROVAL_REQUIRED')"""
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES('003_invalidate_legacy_approval_bindings', datetime('now'))"
                )
            # Create immutable approved bindings only after legacy invalidation has
            # completed. Reapproval appends a new decision instead of changing the
            # invalidated historical row.
            connection.executescript(APPROVAL_IMMUTABILITY)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES('004_immutable_approval_bindings', datetime('now'))"
            )
            connection.executemany(
                "INSERT OR IGNORE INTO lineage_edges(upstream, downstream, transformation) VALUES(?,?,?)",
                [
                    ("raw.orders", "analytics.enriched_orders", "join customer dimension"),
                    ("raw.customer_snapshots", "analytics.enriched_orders", "latest-record deduplication"),
                    ("analytics.enriched_orders", "mart.daily_revenue", "daily aggregation"),
                ],
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.connect() as connection:
            connection.execute(sql, params)

    def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> None:
        with self.connect() as connection:
            connection.executemany(sql, params)

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def audit(
        self,
        *,
        timestamp: str,
        incident_id: str | None,
        agent: str,
        tool: str,
        action: str,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        evidence: list[str],
        decision: str,
        approval: str | None = None,
        execution_result: str | None = None,
        validation_result: str | None = None,
    ) -> None:
        self.execute(
            """INSERT INTO audit_events(timestamp,project,incident_id,agent,tool,action,
            input_json,output_json,evidence_json,decision,approval,execution_result,validation_result)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                timestamp,
                "data-pipeline-reliability-agent",
                incident_id,
                agent,
                tool,
                action,
                json.dumps(inputs, sort_keys=True),
                json.dumps(outputs, sort_keys=True),
                json.dumps(evidence),
                decision,
                approval,
                execution_result,
                validation_result,
            ),
        )

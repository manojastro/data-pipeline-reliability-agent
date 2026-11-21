from __future__ import annotations

from typing import Protocol

from .schemas import AgentDefinition, Evidence


AGENTS = [
    AgentDefinition(name="Pipeline Monitoring Agent", responsibility="Inspect operational run state", inputs=["run metadata"], outputs=["operational anomaly"], tools=["pipeline adapter"], permission_boundary="read-only", failure_behavior="records unavailable status and stops downstream execution"),
    AgentDefinition(name="Data Profiling Agent", responsibility="Calculate deterministic dataset profiles", inputs=["source and output rows"], outputs=["typed quality metrics"], tools=["quality profiler"], permission_boundary="read-only shadow data", failure_behavior="marks profile incomplete"),
    AgentDefinition(name="Lineage Agent", responsibility="Identify impacted upstream and downstream datasets", inputs=["dataset identifier"], outputs=["lineage edges"], tools=["lineage catalog"], permission_boundary="catalog read-only", failure_behavior="reports lineage gap as evidence"),
    AgentDefinition(name="RCA Agent", responsibility="Rank evidence-backed root-cause hypotheses", inputs=["metrics", "lineage", "run state"], outputs=["RCAResult"], tools=["evidence store", "mock reasoning provider"], permission_boundary="cannot change data", failure_behavior="returns low-confidence deterministic fallback"),
    AgentDefinition(name="Fix Generation Agent", responsibility="Create a bounded dbt-style SQL proposal", inputs=["RCAResult"], outputs=["ActionProposal"], tools=["allow-listed SQL template library"], permission_boundary="WITH/SELECT proposals only; no execution", failure_behavior="withholds proposal"),
    AgentDefinition(name="Validation Agent", responsibility="Test proposal against an isolated shadow dataset", inputs=["proposal", "synthetic snapshot"], outputs=["ValidationResult"], tools=["shadow transform runner"], permission_boundary="shadow namespace only", failure_behavior="fails validation and blocks approval"),
]


class ReasoningProvider(Protocol):
    mode: str

    def diagnose(self, evidence: list[Evidence]) -> tuple[str, float, str]: ...


class DeterministicMockReasoningProvider:
    mode = "deterministic-mock"

    def diagnose(self, evidence: list[Evidence]) -> tuple[str, float, str]:
        """Predict from collected evidence only; simulator ground truth is not an input."""
        corpus = " ".join(
            f"{item.kind} {item.claim} {item.observed}" for item in evidence
        ).lower()
        if "timed out" in corpus and "failed" in corpus:
            return (
                "warehouse_connection_timeout",
                0.99,
                "The failed pipeline state includes an observed warehouse timeout before materialization.",
            )
        if (
            "uniqueness gate" in corpus
            or "business_rule_compliance gate" in corpus
            or "output total" in corpus
        ):
            return (
                "non_unique_customer_snapshot_join",
                0.97,
                "Persisted uniqueness, reconciliation, and lineage evidence indicates fact-row multiplication at the customer snapshot join.",
            )
        return (
            "insufficient_evidence",
            0.20,
            "The collected evidence does not support a specific root cause.",
        )

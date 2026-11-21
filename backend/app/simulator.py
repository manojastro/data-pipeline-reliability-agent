from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .schemas import QualityMetric, Scenario


@dataclass
class PipelineSimulation:
    scenario: Scenario
    operational_status: str
    orders: list[dict[str, Any]]
    customers: list[dict[str, Any]]
    output: list[dict[str, Any]]
    error: str | None = None


def _source_data() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = datetime.now(timezone.utc).isoformat()
    customers = [
        {"customer_id": "c-001", "segment": "enterprise", "updated_at": "2026-08-20T08:00:00+00:00"},
        {"customer_id": "c-002", "segment": "growth-old", "updated_at": "2026-07-01T08:00:00+00:00"},
        {"customer_id": "c-002", "segment": "growth", "updated_at": "2026-08-22T08:00:00+00:00"},
        {"customer_id": "c-003", "segment": "starter", "updated_at": "2026-08-21T08:00:00+00:00"},
        {"customer_id": "c-004", "segment": "enterprise", "updated_at": "2026-08-21T08:00:00+00:00"},
    ]
    orders = [
        {"order_id": "o-001", "customer_id": "c-001", "amount": 420.0, "status": "paid", "loaded_at": now},
        {"order_id": "o-002", "customer_id": "c-002", "amount": 125.0, "status": "paid", "loaded_at": now},
        {"order_id": "o-003", "customer_id": "c-003", "amount": 80.0, "status": "paid", "loaded_at": now},
        {"order_id": "o-004", "customer_id": "c-004", "amount": 510.0, "status": "paid", "loaded_at": now},
        {"order_id": "o-005", "customer_id": "c-002", "amount": 75.0, "status": "refunded", "loaded_at": now},
        {"order_id": "o-006", "customer_id": "c-003", "amount": 55.0, "status": "paid", "loaded_at": now},
        {"order_id": "o-007", "customer_id": "c-001", "amount": 260.0, "status": "paid", "loaded_at": now},
        {"order_id": "o-008", "customer_id": "c-004", "amount": 330.0, "status": "paid", "loaded_at": now},
    ]
    return orders, customers


def _latest_customers(customers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in customers:
        current = latest.get(row["customer_id"])
        if current is None or row["updated_at"] > current["updated_at"]:
            latest[row["customer_id"]] = row
    return latest


def correct_transform(orders: list[dict[str, Any]], customers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = _latest_customers(customers)
    return [{**order, "segment": latest[order["customer_id"]]["segment"]} for order in orders]


def faulty_transform(orders: list[dict[str, Any]], customers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for order in orders:
        matches = [row for row in customers if row["customer_id"] == order["customer_id"]]
        output.extend({**order, "segment": customer["segment"]} for customer in matches)
    return output


class SyntheticPipelineAdapter:
    """Local substitute for an orchestrator + warehouse; never executes arbitrary code."""

    def run(self, scenario: Scenario) -> PipelineSimulation:
        orders, customers = _source_data()
        if scenario is Scenario.OPERATIONAL_FAILURE:
            return PipelineSimulation(
                scenario=scenario,
                operational_status="FAILED",
                orders=orders,
                customers=customers,
                output=[],
                error="Synthetic warehouse connection timed out before transformation execution",
            )
        transform = faulty_transform if scenario is Scenario.INCORRECT_JOIN else correct_transform
        return PipelineSimulation(
            scenario=scenario,
            operational_status="SUCCESS",
            orders=orders,
            customers=customers,
            output=transform(orders, customers),
        )


def profile_quality(simulation: PipelineSimulation) -> list[QualityMetric]:
    if simulation.operational_status != "SUCCESS":
        return []
    source = simulation.orders
    output = simulation.output
    total_cells = max(len(output) * 6, 1)
    populated = sum(row.get(field) is not None for row in output for field in ("order_id", "customer_id", "amount", "status", "loaded_at", "segment"))
    completeness = populated / total_cells
    valid_rows = sum(
        isinstance(row.get("amount"), (int, float))
        and row.get("amount", -1) >= 0
        and row.get("status") in {"paid", "refunded"}
        for row in output
    )
    validity = valid_rows / max(len(output), 1)
    unique_ids = len({row["order_id"] for row in output})
    uniqueness = unique_ids / max(len(output), 1)
    expected_total = sum(row["amount"] for row in source)
    observed_total = sum(
        row.get("amount", 0) for row in output if isinstance(row.get("amount"), (int, float))
    )
    consistency = min(expected_total, observed_total) / max(expected_total, observed_total, 1)
    now = datetime.now(timezone.utc)
    ages = [
        (now - datetime.fromisoformat(row["loaded_at"])).total_seconds()
        for row in output
        if row.get("loaded_at")
    ]
    max_age = max(ages, default=0)
    freshness = 1.0 if max_age <= 900 else max(0.0, 1 - (max_age - 900) / 3600)
    accuracy = consistency
    customer_ids = {row["customer_id"] for row in simulation.customers}
    referential = sum(row.get("customer_id") in customer_ids for row in output) / max(len(output), 1)
    counts = {row["order_id"]: 0 for row in source}
    for row in output:
        counts[row["order_id"]] = counts.get(row["order_id"], 0) + 1
    business = sum(count == 1 for count in counts.values()) / max(len(counts), 1)
    values = [
        ("completeness", completeness, f"{populated}/{total_cells} required values", "100% populated", "Required fields must be non-null"),
        ("validity", validity, f"{valid_rows}/{len(output)} valid rows", "amount >= 0; known status", "Values must satisfy type and domain rules"),
        ("uniqueness", uniqueness, f"{unique_ids} unique IDs / {len(output)} rows", "one row per order_id", "Primary business keys must be unique"),
        ("consistency", consistency, f"source total={expected_total:.2f}; output total={observed_total:.2f}", "totals conserved", "Transformation must conserve order amount"),
        ("freshness", freshness, f"maximum age={max_age:.1f}s", "<= 900 seconds", "Newest partition must meet freshness SLO"),
        ("accuracy_proxy", accuracy, f"reconciliation ratio={accuracy:.3f}", "1.000", "Source-to-output aggregate reconciliation"),
        ("referential_integrity", referential, f"{referential:.1%} customer keys resolved", "100%", "Every order must reference a known customer"),
        ("business_rule_compliance", business, f"{sum(count == 1 for count in counts.values())}/{len(counts)} orders emitted once", "one output row per source order", "Enrichment cannot multiply fact rows"),
    ]
    return [
        QualityMetric(
            dimension=name,
            score=round(score, 4),
            passed=score >= 0.99,
            observed=observed,
            expected=expected,
            detail=detail,
        )
        for name, score, observed, expected, detail in values
    ]


def overall_score(metrics: list[QualityMetric]) -> float | None:
    if not metrics:
        return None
    return round(sum(metric.score for metric in metrics) / len(metrics), 4)


def quality_gate_passed(metrics: list[QualityMetric]) -> bool:
    """A failed dimension cannot be hidden by a high average score."""
    return bool(metrics) and all(metric.passed for metric in metrics)

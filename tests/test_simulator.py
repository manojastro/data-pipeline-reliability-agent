from backend.app.schemas import Scenario
from backend.app.schemas import QualityMetric
from backend.app.simulator import PipelineSimulation, SyntheticPipelineAdapter, overall_score, profile_quality, quality_gate_passed


def test_healthy_pipeline_passes_all_eight_quality_dimensions():
    simulation = SyntheticPipelineAdapter().run(Scenario.HEALTHY)
    metrics = profile_quality(simulation)

    assert simulation.operational_status == "SUCCESS"
    assert len(metrics) == 8
    assert all(metric.passed for metric in metrics)
    assert overall_score(metrics) == 1.0


def test_silent_incorrect_join_is_detected_despite_operational_success():
    simulation = SyntheticPipelineAdapter().run(Scenario.INCORRECT_JOIN)
    metrics = {metric.dimension: metric for metric in profile_quality(simulation)}

    assert simulation.operational_status == "SUCCESS"
    assert len(simulation.output) == 10
    assert metrics["uniqueness"].passed is False
    assert metrics["consistency"].passed is False
    assert metrics["accuracy_proxy"].passed is False
    assert metrics["business_rule_compliance"].passed is False
    assert overall_score(list(metrics.values())) < 0.96


def test_operational_failure_has_no_materialized_output():
    simulation = SyntheticPipelineAdapter().run(Scenario.OPERATIONAL_FAILURE)

    assert simulation.operational_status == "FAILED"
    assert simulation.output == []
    assert profile_quality(simulation) == []


def test_one_failed_dimension_cannot_hide_inside_a_passing_average():
    metrics = [
        QualityMetric(dimension=f"metric_{index}", score=1.0, passed=True, observed="ok", expected="ok", detail="ok")
        for index in range(7)
    ]
    metrics.append(QualityMetric(dimension="critical", score=0.98, passed=False, observed="bad", expected="ok", detail="failed"))
    assert overall_score(metrics) > 0.96
    assert quality_gate_passed(metrics) is False


def test_referential_integrity_profiles_transformed_output_not_source():
    baseline = SyntheticPipelineAdapter().run(Scenario.HEALTHY)
    invalid_output = [dict(row) for row in baseline.output]
    invalid_output[0]["customer_id"] = "missing-customer"
    simulation = PipelineSimulation(
        scenario=Scenario.HEALTHY,
        operational_status="SUCCESS",
        orders=baseline.orders,
        customers=baseline.customers,
        output=invalid_output,
    )
    metrics = {item.dimension: item for item in profile_quality(simulation)}
    assert metrics["referential_integrity"].passed is False

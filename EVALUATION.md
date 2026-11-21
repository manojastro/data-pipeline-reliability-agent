# Evaluation

LedgerLine evaluates the workflow against simulator ground truth, not presentation
quality or subjective LLM grading.

The reasoner receives persisted evidence only. Ground truth is held in a separate
scenario label map and is compared with the completed prediction afterward. The test
suite changes ground truth without changing evidence and requires RCA accuracy to fall,
preventing label leakage.

| Metric | Definition | Persisted field |
|---|---|---|
| Anomaly recall | detected expected incidents / expected incidents | `detected_incident`, `expected_incident` |
| RCA accuracy | correct predicted causes / detected incidents | `rca_correct` |
| False-positive rate | healthy runs incorrectly alerted / all runs | `false_positive` |
| Quality improvement | shadow after score − production-like before score | `quality_improvement` |
| MTTD | event-to-detection seconds | `mttd_seconds` |
| MTTR | detection-to-validated proposal seconds in this POC | `mttr_seconds` |

## Benchmark cases

1. `healthy`: pipeline succeeds, all eight dimensions score 1.0, no incident expected.
2. `operational_failure`: task fails before materialization; an operational incident
   with `warehouse_connection_timeout` ground truth is expected.
3. `incorrect_join`: task succeeds but duplicates two fact rows; a silent logical
   incident with `non_unique_customer_snapshot_join` ground truth is expected.

The incorrect-join validation must execute the exact hashed SQL in isolated SQLite,
restore 10 output rows to 8, move overall quality to 1.0, pass every dimension,
preserve the source snapshot, and satisfy SQL policy. A high average cannot hide any
failed quality dimension. Execution performs the same checks independently against a
fresh adapter snapshot rather than reusing the earlier score.

## Reproduce

```bash
python -m pytest
```

Run `POST /api/v1/demo/bootstrap`, then inspect `/api/v1/overview` and
`/api/v1/evaluations`. Small datasets and in-process timing mean latency values are
demonstration signals, not performance benchmarks. Adding more scenarios should add
ground-truth fixtures before changing thresholds.

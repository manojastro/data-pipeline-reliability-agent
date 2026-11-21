# Limitations

- Synthetic rows replace a real warehouse and orchestrator. They exercise control
  flow and invariants, not connector throughput or distributed failure modes.
- Only healthy, connection-timeout, and non-unique-join scenarios are executable in
  this first vertical slice. Schema drift, null explosion, type errors, missing
  partitions, late data, field renames, and other transformation bugs are natural
  next fixtures using the same profiler interface.
- The fix generator selects one reviewed SQL template. It demonstrates policy,
  evidence, hashing, validation, and approval, not arbitrary code generation.
- Remediation execution is simulated and cannot update production data.
- SQLite is ideal for a self-contained POC but not concurrent enterprise workloads.
- Rate limiting and runtime Prometheus counters are process-local.
- Mock RCA is heuristic and deterministic. A future OpenAI-compatible implementation
  must retain schema validation, evidence citation, timeouts, and fallback behavior.
- MTTD/MTTR values from tiny synchronous workflows are useful for contract testing,
  not capacity planning.
- One environment-configured API key demonstrates verified identity-to-role mapping.
  Production needs multiple principals, rotation, OIDC-backed RBAC, and enforced
  separation of approver and executor duties.

Future work: ingest dbt manifests/tests, integrate Airflow/Dagster adapters, add
warehouse shadow schemas, evaluate a broader failure corpus, export OpenTelemetry,
introduce signed change bundles and OIDC, and run post-deployment canary comparisons.

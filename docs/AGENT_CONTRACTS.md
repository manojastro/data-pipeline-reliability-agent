# Agent contracts

The runtime contract is available from `GET /api/v1/agents`. Agent outputs that affect
decisions use Pydantic types: `QualityMetric`, `Evidence`, `Hypothesis`, `RCAResult`,
`ActionProposal`, and `ValidationResult`.

The monitoring, profiling, and lineage agents are read-only. RCA cannot mutate data.
Fix generation can select only reviewed `WITH`/`SELECT` templates. Validation operates
only by executing the exact artifact against an in-process SQLite shadow copy.
Deployment requires a passing validation and verified approver/executor credentials,
then independently reruns only the approval-bound hashed proposal through a
simulated deployer.

Once approved, SQLite triggers make the stored SQL/hash and approval binding/identity
immutable. Legacy reapproval leaves the invalidated record intact and appends a new
decision after fresh exact-artifact validation.

If profiling is unavailable, the workflow must not claim data health. If reasoning is
unavailable or malformed, production integration must return a low-confidence
deterministic fallback. If validation fails, approval is rejected. Any attempt to
change approved SQL or its binding is rejected at the persistence boundary.

# SQLite migrations

`001_initial` is applied idempotently by `Database.initialize()` and recorded in the
`schema_migrations` table. The schema lives beside the database adapter so app
startup, tests, and containers all use the same migration path. Future changes must
add a new version and preserve existing rows; destructive migrations are prohibited.

`002_bind_approval_artifact` adds `approvals.artifact_sha256`. Existing approvals are
marked `REAPPROVAL_REQUIRED` with a null binding because the artifact originally
reviewed cannot be inferred safely from mutable proposal state.

`003_invalidate_legacy_approval_bindings` repairs databases initialized by an early
POC version that backfilled the proposal's current hash. It invalidates those bindings
exactly once. A verified reviewer must submit a new decision; the workflow first
revalidates the current exact SQL and only then stores its SHA-256. Until that happens,
execution is blocked.

`004_immutable_approval_bindings` installs triggers after legacy invalidation. Once a
decision is approved, its identity/binding and the proposal SQL/hash cannot be updated
or deleted. Reapproval preserves the `REAPPROVAL_REQUIRED` row and appends a new
reviewer decision, so the migration history and human decisions remain inspectable.

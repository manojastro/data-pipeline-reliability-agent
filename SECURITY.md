# Security model

## Controls implemented

- API payloads are schema-validated by Pydantic; unknown scenario values fail with 422.
- `API_KEY`, when configured, is checked on `/api/v1/*` using constant-time comparison.
- All workflow-changing POST endpoints, including demo run/bootstrap, require a
  configured verified credential. No-key mode is read-only; there is no anonymous
  operator fallback.
- Identity and role come from server-side `API_KEY_ACTOR` and `API_KEY_ROLE`, never a
  caller-controlled role header. `approver` decides, `executor` executes, and `admin`
  can perform both safe local-demo operations.
- CORS origins and per-minute API request limits are environment-configurable.
- CSP, no-sniff, referrer-policy, and request-duration headers are returned.
- SQL generation uses an internal fixed template. A policy rejects non-`SELECT`/`WITH`
  SQL and destructive tokens. No endpoint accepts SQL, code, file paths, or commands.
- The exact proposed SQL is rendered and executed in a query-only, in-memory SQLite
  shadow; validation does not substitute a known-correct implementation.
- Approval stores the reviewed artifact hash. Both proposal integrity and approval
  binding are rechecked immediately before execution. Database triggers reject updates
  or deletes of an approved proposal's SQL/hash and its approval identity/binding.
- Legacy approvals without a verifiable stored artifact hash are never backfilled from
  mutable proposal state. They become `REAPPROVAL_REQUIRED`; execution stays blocked
  until a fresh validation and newly appended reviewed decision bind the current bytes;
  the invalidated legacy row remains unchanged as history.
- A passing isolated shadow validation and verified human approval are both mandatory.
- Deployment verification independently reruns the exact proposal against a fresh
  source snapshot and fails closed on drift, a non-success operational state, or an
  adapter timeout. No execution record is created and the incident remains open.
- Execution is a bounded, idempotent simulation; it never invokes a shell or production
  database.
- Audit records contain structured inputs and outputs but no environment secrets.
  SQLite triggers reject audit update/delete and protected approved-binding mutation.
- The container runs as non-root user `10001`.

## Secret management

Copy `.env.example` to `.env` locally and supply secrets through environment variables.
Never commit `.env`. Mock mode does not need `LLM_API_KEY`. If a real provider adapter
is added, sanitize prompts, outputs, and telemetry before persistence.

## Risk model

Each proposed action includes risk, blast radius, reversibility, expected benefit,
confidence, required approval, and rollback plan. The incorrect-join change is
`MEDIUM` risk because it affects one model and its downstream revenue mart but is a
reversible version-controlled transformation.

## Production gaps

The POC uses one environment-configured shared credential and role. Production requires
TLS termination, OIDC SSO, centrally enforced RBAC, separation of approver and executor
duties, a distributed rate limiter, signed proposals, managed
secrets, database encryption, audit export/WORM retention, dependency and container
scanning, and a real change-management integration.

Report security concerns privately to the repository owner; do not include credentials
or sensitive datasets in reports.

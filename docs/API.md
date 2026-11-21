# API guide

Operational endpoints: `GET /health`, `GET /ready`, `GET /metrics`.

Versioned API:

- `GET /api/v1/overview`
- `POST /api/v1/demo/run` with `healthy`, `operational_failure`, or `incorrect_join`
- `POST /api/v1/demo/bootstrap`
- `GET /api/v1/runs` and `GET /api/v1/runs/{id}/quality`
- `GET /api/v1/incidents` and `GET /api/v1/incidents/{id}`
- `GET /api/v1/lineage`, `/agents`, `/proposals`, `/approvals`
- `POST /api/v1/proposals/{id}/approval`
- `POST /api/v1/proposals/{id}/execute`
- `GET /api/v1/evaluations`, `/audit`

Interactive schemas and examples are at `/docs`. Every POST endpoint requires a
configured `API_KEY` sent as
`X-API-Key`. Approval and execution never trust a caller-supplied role: the service
derives `API_KEY_ACTOR` and `API_KEY_ROLE` from the verified credential. Mutations are
locked when no credential is configured. `approver` can decide, `executor` can execute,
and `admin` can perform both local demo operations.

An upgraded database can return approval status `REAPPROVAL_REQUIRED` when the exact
artifact reviewed by a legacy approval cannot be proven. Execution is blocked in that
state. Posting a new `APPROVED` decision triggers fresh validation of the current SQL
and appends a new SHA-256-bound reviewer decision before execution becomes eligible;
the invalidated row is retained.

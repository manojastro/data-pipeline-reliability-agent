# Demo runbook

## Goal

Show that a green pipeline can silently corrupt a business KPI, then prove why the
proposed remediation is safe enough to request approval.

## Start

```powershell
$env:API_KEY = "local-demo-key"
$env:API_KEY_ROLE = "admin"
$env:API_KEY_ACTOR = "local-demo-admin"
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8103
```

Open `http://localhost:8103`.

## Dashboard path

1. Enter the configured control credential (`local-demo-key` for the local-only
   Compose/script defaults). No-key dashboard access is read-only.
2. Click **Run healthy**. The pipeline is `SUCCESS`, row cardinality is 8→8, and all
   eight quality dimensions pass.
3. Click **Inject silent failure**. The orchestrator still says `SUCCESS`, but the
   dashboard shows 8→10 rows and failed uniqueness, reconciliation, accuracy proxy,
   and business-rule gates.
4. Inspect the lineage from `raw.customer_snapshots` through `enriched_orders` to
   `daily_revenue` and open the evidence cards.
5. Explain the typed RCA: two snapshots for customer `c-002` were joined without a
   latest-record window.
6. Review the bounded dbt-style SQL, `MEDIUM` risk, scope, and rollback plan.
7. Confirm execution of the exact SQL in isolated SQLite restores 10→8 and raises
   quality to 100%.
8. Click **Approve & simulate deploy**. The API checks
   the approval-bound hash and independently reruns the SQL against a fresh source
   snapshot before resolving the incident.
9. Open `/api/v1/audit`, `/api/v1/evaluations`, `/metrics`, and `/docs`.

## Scripted path

With the server running:

```powershell
.\scripts\demo.ps1
```

Expected summary: operational `SUCCESS`, incident kind `silent_logical_failure`, root
cause `non_unique_customer_snapshot_join`, improved score `1`, execution `SUCCEEDED`.

## Safety proof

Try execution before approval: the API returns 403. Try a missing or incorrect
`X-API-Key`: the API returns 401 even if a caller invents a role header. Try an invalid
scenario: validation returns 422. Audit rows cannot be updated or deleted by the
application database. Approved SQL/hash and approval binding/identity rows are also
database-immutable; legacy reapproval appends a new decision instead of rewriting history.

# Architecture

## System architecture

```mermaid
flowchart TB
  subgraph Experience
    DASH[Domain dashboard]
    DOCS[OpenAPI]
  end
  subgraph Control_Plane[FastAPI control plane]
    AUTH[API key + role abstraction]
    API[Versioned REST API]
    RATE[Rate limiter]
    OBS[Health / readiness / Prometheus metrics]
  end
  subgraph Reliability_Workflow
    MON[Pipeline monitor]
    PROF[Data profiler]
    LIN[Lineage investigator]
    RCA[Typed RCA agent]
    GEN[Bounded fix generator]
    VAL[Shadow validator]
    APPROVAL[Human approval gate]
    VERIFY[Outcome verifier]
  end
  subgraph Local_Adapters
    SIM[Synthetic pipeline + warehouse]
    MOCK[Deterministic reasoning provider]
    TEMPLATES[Allow-listed SQL templates]
  end
  DB[(SQLite evidence + audit store)]
  DASH --> API
  DOCS --> API
  RATE --> AUTH --> API
  API --> MON --> PROF --> LIN --> RCA --> GEN --> VAL --> APPROVAL --> VERIFY
  MON --> SIM
  RCA --> MOCK
  GEN --> TEMPLATES
  MON & PROF & LIN & RCA & GEN & VAL & APPROVAL & VERIFY --> DB
  API --> OBS
```

## Agent workflow

```mermaid
stateDiagram-v2
  [*] --> EventReceived
  EventReceived --> PipelineInspected
  PipelineInspected --> ManualRecovery: operational FAILED
  PipelineInspected --> DataProfiled: operational SUCCESS
  DataProfiled --> Complete: score >= 0.96
  DataProfiled --> AnomalyDetected: score < 0.96
  AnomalyDetected --> LineageCollected
  LineageCollected --> RCACompleted
  RCACompleted --> FixProposed
  FixProposed --> ValidationFailed: shadow gate fails
  FixProposed --> ShadowValidated: all checks pass
  ShadowValidated --> AwaitingHumanApproval
  AwaitingHumanApproval --> ReapprovalRequired: legacy binding cannot be proven
  ReapprovalRequired --> AwaitingHumanApproval: fresh validation + append decision
  AwaitingHumanApproval --> Rejected: reviewer rejects
  AwaitingHumanApproval --> Executed: verified approver/executor credentials
  Executed --> OutcomeVerified
  OutcomeVerified --> [*]
  Complete --> [*]
  ManualRecovery --> [*]
  Rejected --> [*]
  ValidationFailed --> [*]
```

## Validation workflow

```mermaid
sequenceDiagram
  participant A as Fix Generation Agent
  participant P as SQL Policy
  participant S as Shadow Dataset
  participant Q as 8 Quality Gates
  participant H as Human Operator
  participant D as Simulated Deployer
  participant R as Fresh pipeline adapter run
  A->>P: proposed dbt SELECT/WITH + SHA-256
  P-->>A: allow-listed and read-only
  A->>S: apply deterministic transform
  S->>Q: before/after profiles
  Q-->>A: row count 10→8, score→1.0
  A->>H: risk, blast radius, rollback, evidence
  H->>D: explicit immutable hash-bound approval
  D->>P: verify unchanged hash and policy
  D->>R: recollect current operational state and source snapshot
  R-->>D: require operational SUCCESS; timeout/failure blocks
  D->>Q: independently rerun exact SQL on fresh source snapshot
  Q-->>H: verified score and audit event
```

## Persistence

The project-local SQLite database contains versioned migration state and domain tables
for runs, metrics, incidents, evidence, lineage, proposals, validations, approvals,
executions, evaluations, and audit events. Triggers reject audit updates/deletes and
prevent approved SQL/hash or approval binding/identity updates/deletes. A legacy
`REAPPROVAL_REQUIRED` record remains immutable history once a new approved decision is
appended. Each test receives an isolated database.

## Extension boundaries

`SyntheticPipelineAdapter` is the local implementation of the pipeline/warehouse
boundary. A production adapter can collect Airflow or Dagster run metadata and query
DuckDB, PostgreSQL, Snowflake, or BigQuery. `ReasoningProvider` isolates optional LLM
reasoning. The correctness gates remain deterministic regardless of provider.

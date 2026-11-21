from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from hmac import compare_digest
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .agents import AGENTS
from .database import Database
from .schemas import ApprovalRequest, DemoRequest, ExecuteResponse, Overview, WorkflowResult
from .workflow import ReliabilityWorkflow, default_database_path


class RuntimeMetrics:
    def __init__(self) -> None:
        self.requests = 0
        self.errors = 0
        self.workflow_runs = 0
        self.workflow_seconds = 0.0
        self._lock = threading.Lock()

    def observe_request(self, error: bool) -> None:
        with self._lock:
            self.requests += 1
            self.errors += int(error)

    def observe_workflow(self, elapsed: float) -> None:
        with self._lock:
            self.workflow_runs += 1
            self.workflow_seconds += elapsed

    def render(self) -> str:
        with self._lock:
            return "\n".join(
                [
                    "# HELP data_reliability_http_requests_total HTTP requests processed.",
                    "# TYPE data_reliability_http_requests_total counter",
                    f"data_reliability_http_requests_total {self.requests}",
                    "# HELP data_reliability_http_errors_total HTTP 5xx responses.",
                    "# TYPE data_reliability_http_errors_total counter",
                    f"data_reliability_http_errors_total {self.errors}",
                    "# HELP data_reliability_workflow_runs_total Agent workflows executed.",
                    "# TYPE data_reliability_workflow_runs_total counter",
                    f"data_reliability_workflow_runs_total {self.workflow_runs}",
                    "# HELP data_reliability_workflow_duration_seconds_total Cumulative workflow time.",
                    "# TYPE data_reliability_workflow_duration_seconds_total counter",
                    f"data_reliability_workflow_duration_seconds_total {self.workflow_seconds:.6f}",
                    "",
                ]
            )


class SlidingWindowRateLimiter:
    def __init__(self, limit: int = 120, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.clients: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self.clients[key]
            while bucket and now - bucket[0] > self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str


def create_app(db_path: str | Path | None = None) -> FastAPI:
    database = Database(db_path or os.getenv("DATABASE_PATH") or default_database_path())
    workflow = ReliabilityWorkflow(database)
    runtime_metrics = RuntimeMetrics()
    limiter = SlidingWindowRateLimiter(limit=int(os.getenv("RATE_LIMIT_PER_MINUTE", "120")))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        yield

    app = FastAPI(
        title="LedgerLine Data Reliability Agent",
        description="Evidence-backed detection, RCA, shadow validation, and approval-gated remediation for data pipelines.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.workflow = workflow
    origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "http://localhost:8103").split(",") if item.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-API-Key"], allow_credentials=False)

    @app.middleware("http")
    async def safety_and_observability(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            client = request.client.host if request.client else "unknown"
            if not limiter.allowed(client):
                runtime_metrics.observe_request(False)
                return PlainTextResponse("rate limit exceeded", status_code=429)
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Duration-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'"
        runtime_metrics.observe_request(response.status_code >= 500)
        return response

    def authenticate(x_api_key: Annotated[str | None, Header()] = None) -> Principal | None:
        configured = os.getenv("API_KEY")
        if configured and (not x_api_key or not compare_digest(x_api_key, configured)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
        if not configured:
            return None
        return Principal(
            actor=os.getenv("API_KEY_ACTOR", "configured-api-client"),
            role=os.getenv("API_KEY_ROLE", "viewer").lower(),
        )

    def verified_principal(
        principal: Principal | None = Depends(authenticate),
    ) -> Principal:
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="mutation credentials are not configured",
            )
        return principal

    def approver_principal(
        principal: Principal = Depends(verified_principal),
    ) -> Principal:
        if principal.role not in {"approver", "admin"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="approver role required")
        return principal

    def executor_principal(
        principal: Principal = Depends(verified_principal),
    ) -> Principal:
        if principal.role not in {"executor", "admin"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="executor role required")
        return principal

    protected = [Depends(authenticate)]

    @app.get("/health", tags=["operations"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "data-pipeline-reliability-agent"}

    @app.get("/ready", tags=["operations"])
    def ready() -> dict[str, str]:
        try:
            database.one("SELECT 1 AS ready")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ready", "database": "sqlite"}

    @app.get("/metrics", response_class=PlainTextResponse, tags=["operations"])
    def metrics() -> str:
        return runtime_metrics.render()

    @app.get("/api/v1/overview", response_model=Overview, dependencies=protected, tags=["reliability"])
    def overview() -> dict:
        return workflow.overview()

    @app.post("/api/v1/demo/run", response_model=WorkflowResult, tags=["demo"])
    def run_demo(
        payload: DemoRequest,
        _: Principal = Depends(verified_principal),
    ) -> WorkflowResult:
        started = time.perf_counter()
        result = workflow.run(payload.scenario)
        runtime_metrics.observe_workflow(time.perf_counter() - started)
        return result

    @app.post("/api/v1/demo/bootstrap", tags=["demo"])
    def bootstrap_demo(_: Principal = Depends(verified_principal)) -> list[WorkflowResult]:
        results = []
        for scenario in ("healthy", "operational_failure", "incorrect_join"):
            started = time.perf_counter()
            from .schemas import Scenario
            results.append(workflow.run(Scenario(scenario)))
            runtime_metrics.observe_workflow(time.perf_counter() - started)
        return results

    @app.get("/api/v1/runs", dependencies=protected, tags=["reliability"])
    def runs() -> list[dict]:
        return database.all("SELECT * FROM pipeline_runs ORDER BY completed_at DESC LIMIT 100")

    @app.get("/api/v1/runs/{run_id}/quality", dependencies=protected, tags=["quality"])
    def run_quality(run_id: str) -> list[dict]:
        if not database.one("SELECT id FROM pipeline_runs WHERE id=?", (run_id,)):
            raise HTTPException(status_code=404, detail="run not found")
        return database.all("SELECT dimension,score,passed,observed,expected,detail FROM quality_metrics WHERE run_id=? ORDER BY id", (run_id,))

    @app.get("/api/v1/incidents", dependencies=protected, tags=["investigations"])
    def incidents() -> list[dict]:
        return database.all("SELECT * FROM incidents ORDER BY detected_at DESC LIMIT 100")

    @app.get("/api/v1/incidents/{incident_id}", dependencies=protected, tags=["investigations"])
    def incident(incident_id: str) -> dict:
        result = workflow.incident_detail(incident_id)
        if not result:
            raise HTTPException(status_code=404, detail="incident not found")
        return result

    @app.get("/api/v1/lineage", dependencies=protected, tags=["lineage"])
    def lineage() -> list[dict]:
        return database.all("SELECT upstream,downstream,transformation FROM lineage_edges ORDER BY id")

    @app.get("/api/v1/agents", dependencies=protected, tags=["governance"])
    def agents() -> list[dict]:
        return [agent.model_dump() for agent in AGENTS]

    @app.get("/api/v1/proposals", dependencies=protected, tags=["remediation"])
    def proposals() -> list[dict]:
        return database.all("SELECT * FROM proposals ORDER BY created_at DESC")

    @app.get("/api/v1/approvals", dependencies=protected, tags=["remediation"])
    def approvals() -> list[dict]:
        return database.all("SELECT a.*, p.title, p.risk FROM approvals a JOIN proposals p ON p.id=a.proposal_id ORDER BY a.created_at DESC")

    @app.post("/api/v1/proposals/{proposal_id}/approval", tags=["remediation"])
    def approve(
        proposal_id: str,
        payload: ApprovalRequest,
        principal: Principal = Depends(approver_principal),
    ) -> dict:
        try:
            return workflow.decide_approval(proposal_id, payload.decision, principal.actor, payload.reason)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/proposals/{proposal_id}/execute", response_model=ExecuteResponse, tags=["remediation"])
    def execute(
        proposal_id: str,
        _: Principal = Depends(executor_principal),
    ) -> ExecuteResponse:
        try:
            return workflow.execute(proposal_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/evaluations", dependencies=protected, tags=["evaluation"])
    def evaluations() -> list[dict]:
        return database.all("SELECT * FROM evaluations ORDER BY id DESC LIMIT 100")

    @app.get("/api/v1/audit", dependencies=protected, tags=["governance"])
    def audit() -> list[dict]:
        return database.all("SELECT * FROM audit_events ORDER BY id DESC LIMIT 100")

    frontend = Path(__file__).resolve().parents[2] / "frontend"
    app.mount("/ui", StaticFiles(directory=frontend), name="ui")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(frontend / "index.html")

    return app


app = create_app()

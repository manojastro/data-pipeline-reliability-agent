def test_health_readiness_metrics_and_dashboard(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["status"] == "ready"
    assert "data_reliability_http_requests_total" in client.get("/metrics").text
    assert "LedgerLine" in client.get("/").text


def test_full_api_workflow(client):
    response = client.post("/api/v1/demo/run", json={"scenario": "incorrect_join"})
    assert response.status_code == 200
    result = response.json()
    assert result["pipeline_status"] == "SUCCESS"
    assert result["incident_kind"] == "silent_logical_failure"
    assert result["validation"]["passed"] is True

    run_id = result["run_id"]
    quality = client.get(f"/api/v1/runs/{run_id}/quality").json()
    assert len(quality) == 8
    assert any(item["passed"] == 0 for item in quality)

    proposal_id = result["rca"]["recommended_action"]["proposal_id"]
    blocked = client.post(f"/api/v1/proposals/{proposal_id}/execute")
    assert blocked.status_code == 403

    approval = client.post(
        f"/api/v1/proposals/{proposal_id}/approval",
        json={"decision": "APPROVED", "reviewer": "api-test", "reason": "Validated in isolated shadow data"},
    )
    assert approval.status_code == 200
    assert approval.json()["reviewer"] == "verified-test-admin"
    execution = client.post(f"/api/v1/proposals/{proposal_id}/execute")
    assert execution.status_code == 200
    assert execution.json()["verified_quality_score"] == 1.0

    incident = client.get(f"/api/v1/incidents/{result['incident_id']}").json()
    assert incident["status"] == "RESOLVED"
    assert incident["execution"]["status"] == "SUCCEEDED"


def test_input_and_verified_credential_controls(client):
    assert client.post("/api/v1/demo/run", json={"scenario": "made_up"}).status_code == 422
    result = client.post("/api/v1/demo/run", json={"scenario": "incorrect_join"}).json()
    proposal_id = result["rca"]["recommended_action"]["proposal_id"]
    denied = client.post(
        f"/api/v1/proposals/{proposal_id}/approval",
        headers={"X-API-Key": "wrong-key", "X-Role": "admin"},
        json={"decision": "APPROVED", "reviewer": "viewer-user", "reason": "Should not be allowed"},
    )
    assert denied.status_code == 401


def test_missing_configured_credential_cannot_mutate(client):
    result = client.post("/api/v1/demo/run", json={"scenario": "incorrect_join"}).json()
    proposal_id = result["rca"]["recommended_action"]["proposal_id"]
    response = client.post(
        f"/api/v1/proposals/{proposal_id}/approval",
        headers={"X-API-Key": ""},
        json={"decision": "APPROVED", "reviewer": "anonymous", "reason": "No credential"},
    )
    assert response.status_code == 401


def test_openapi_exposes_versioned_contract(client):
    schema = client.get("/openapi.json").json()
    assert "/api/v1/demo/run" in schema["paths"]
    assert "/api/v1/proposals/{proposal_id}/approval" in schema["paths"]


def test_mutations_are_locked_when_no_credential_is_configured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app.main import create_app

    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("API_KEY_ROLE", raising=False)
    with TestClient(create_app(tmp_path / "locked.db")) as anonymous:
        assert anonymous.get("/api/v1/overview").status_code == 200
        run = anonymous.post("/api/v1/demo/run", json={"scenario": "incorrect_join"})
        bootstrap = anonymous.post("/api/v1/demo/bootstrap")
        assert run.status_code == 401
        assert bootstrap.status_code == 401
        assert run.json()["detail"] == "mutation credentials are not configured"
        assert anonymous.app.state.database.one("SELECT COUNT(*) count FROM pipeline_runs")["count"] == 0
        assert anonymous.app.state.database.one("SELECT COUNT(*) count FROM incidents")["count"] == 0
        assert anonymous.app.state.database.one("SELECT COUNT(*) count FROM approvals")["count"] == 0

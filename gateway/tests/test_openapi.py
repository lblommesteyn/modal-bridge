import json
from pathlib import Path
from fastapi.testclient import TestClient
from modal_bridge.app import create_app
from modal_bridge.demo import DEMO_TOKEN, create_demo_app


def test_openapi_contract_and_bearer_auth(tmp_path):
    app = create_app(token="x" * 32, targets={"square": ("demo", "square")}, db_path=str(tmp_path / "db"))
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        assert schema["components"]["securitySchemes"]["GatewayToken"]["scheme"] == "bearer"
        for path, method in [("/v1/jobs", "post"), ("/v1/jobs/{job_id}", "get"), ("/v1/jobs/{job_id}/cancel", "post")]:
            operation = schema["paths"][path][method]
            assert operation["security"] == [{"GatewayToken": []}]
            assert "Authorization" not in [p["name"] for p in operation.get("parameters", [])]
            response = operation["responses"]["202" if path == "/v1/jobs" else "200"]
            assert response["content"]["application/json"]["schema"]["$ref"].endswith("/Job")
        assert "security" not in schema["paths"]["/health"]["get"]
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        assert client.get("/v1/jobs/missing").headers["www-authenticate"] == "Bearer"
        assert client.get("/v1/jobs/missing", headers={"Authorization": "Basic " + "x" * 32}).status_code == 401
        assert client.get("/v1/jobs/missing", headers={"Authorization": "bearer " + "x" * 32}).status_code == 404
    exported = Path(__file__).resolve().parents[2] / "docs/openapi.json"
    assert schema == json.loads(exported.read_text()), "Regenerate with uv run python scripts/export_openapi.py"


def test_swagger_demo_walkthrough():
    headers = {"Authorization": f"Bearer {DEMO_TOKEN}", "Idempotency-Key": "demo-001"}
    body = {"function": "square", "input": {"value": 12}}
    with TestClient(create_demo_app()) as client:
        first = client.post("/v1/jobs", headers=headers, json=body)
        assert first.status_code == 202
        job = first.json()
        assert "result" not in job
        assert client.post("/v1/jobs", headers=headers, json=body).json()["id"] == job["id"]
        result = client.get(f'/v1/jobs/{job["id"]}', headers=headers).json()
        assert result["result"] == {"value": 144}
        assert result["status"] == "succeeded"
        body["input"]["value"] = 13
        assert client.post("/v1/jobs", headers=headers, json=body).status_code == 409

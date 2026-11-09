import concurrent.futures
import threading

import pytest
from fastapi.testclient import TestClient
from modal_bridge.app import Store, create_app

TOKEN = "a" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "test-key"}

class FakeBackend:
    def __init__(self):
        self.calls = []
        self.result = {"status": "pending"}
        self.cancellations = []
        self.error = False
    def submit(self, target, payload):
        self.calls.append((target, payload))
        if self.error:
            raise ConnectionError("secret should never reach client")
        return "fc-test"
    def poll(self, call_id):
        if self.error:
            raise ConnectionError("secret")
        return self.result
    def cancel(self, call_id):
        self.cancellations.append(call_id)

@pytest.fixture
def setup(tmp_path):
    backend = FakeBackend()
    path = str(tmp_path / "jobs.sqlite")
    def make():
        return TestClient(create_app(token=TOKEN, targets={"square": ("demo", "square")}, db_path=path, backend=backend))
    return make(), backend, make

def submit(client, **kwargs):
    return client.post("/v1/jobs", headers=HEADERS, json={"function": "square", "input": {"value": 3}}, **kwargs)

def test_auth_allowlist_and_required_key(setup):
    client, backend, _ = setup
    assert client.post("/v1/jobs", json={}).status_code == 401
    assert client.get("/v1/jobs/nonexistent").status_code == 401
    assert client.post("/v1/jobs", headers=HEADERS, json={"function": "arbitrary", "input": {}}).status_code == 404
    assert client.post("/v1/jobs", headers={"Authorization": f"Bearer {TOKEN}"}, json={"function": "square", "input": {}}).status_code == 422
    assert backend.calls == []

def test_dedup_conflict_and_restart(setup):
    client, backend, make = setup
    first = submit(client)
    assert first.status_code == 202
    assert submit(make()).json()["id"] == first.json()["id"]
    assert len(backend.calls) == 1
    assert client.post("/v1/jobs", headers=HEADERS, json={"function": "square", "input": {"value": 4}}).status_code == 409
    backend.result = {"status": "succeeded", "result": {"value": 9}}
    url = f'/v1/jobs/{first.json()["id"]}'
    assert make().get(url, headers=HEADERS).json()["result"] == {"value": 9}
    backend.error = True
    assert make().get(url, headers=HEADERS).json()["status"] == "succeeded"

def test_unknown_submission_never_automatically_retries(setup):
    client, backend, _ = setup
    backend.error = True
    result = submit(client).json()
    assert result["status"] == "submission_unknown"
    assert "secret" not in str(result)
    submit(client)
    assert len(backend.calls) == 1

def test_poll_transport_error_is_not_job_failure(setup):
    client, backend, _ = setup
    job = submit(client).json()
    backend.error = True
    url = f'/v1/jobs/{job["id"]}'
    assert client.get(url, headers=HEADERS).status_code == 502
    backend.error = False
    assert client.get(url, headers=HEADERS).json()["status"] == "pending"

def test_cancel_is_request_not_promise_and_can_race_completion(setup):
    client, backend, _ = setup
    job = submit(client).json()
    url = f'/v1/jobs/{job["id"]}'
    assert client.post(url + "/cancel", headers=HEADERS).json()["status"] == "cancel_requested"
    client.post(url + "/cancel", headers=HEADERS)
    assert len(backend.cancellations) == 1
    backend.result = {"status": "succeeded", "result": 9}
    assert client.get(url, headers=HEADERS).json()["status"] == "succeeded"
    client.post(url + "/cancel", headers=HEADERS)
    assert len(backend.cancellations) == 1

def test_concurrent_idempotency(tmp_path):
    store = Store(str(tmp_path / "race.sqlite"))
    barrier = threading.Barrier(8)
    def reserve(_):
        barrier.wait()
        return store.reserve("same", "hash", "square")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(reserve, range(8)))
    assert sum(created for _, created in rows) == 1
    assert len({row["id"] for row, _ in rows}) == 1

def test_terminal_state_cannot_be_overwritten(tmp_path):
    store = Store(str(tmp_path / "terminal.sqlite"))
    row, _ = store.reserve("key", "hash", "square")
    store.attach(row["id"], "fc-1")
    store.update(row["id"], {"status": "succeeded", "result": 1})
    store.update(row["id"], {"status": "cancel_requested"})
    assert 'succeeded' in store.get(row["id"])["state"]

def test_body_limit_and_unknown_job(setup):
    client, _, _ = setup
    assert client.post("/v1/jobs", headers=HEADERS, content=b'x' * 1_048_577).status_code == 413
    assert client.get("/v1/jobs/missing", headers=HEADERS).status_code == 404
    assert client.get("/health").headers["cache-control"] == "no-store"

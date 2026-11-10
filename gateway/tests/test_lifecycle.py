"""Callbacks, reconciliation, lifecycle metadata, quotas, limits and the Modal-hosted ledger."""
import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient
from modal_bridge.app import DictStore, Store, create_app
from modal_bridge.reconcile import check_callback_url, sign
from modal_bridge.store import BACKOFF

TOKEN = "t" * 32
SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HOOK = "https://app.example/hooks/bridge"


class Backend:
    def __init__(self):
        self.calls, self.results, self.submit_error = [], {}, False

    def submit(self, target, payload):
        self.calls.append(payload)
        if self.submit_error:
            raise ConnectionError("uncertain")
        call_id = f"fc-{len(self.calls)}"
        self.results[call_id] = {"status": "pending"}
        return call_id

    def poll(self, call_id, timeout=0):
        return self.results[call_id]

    def cancel(self, call_id):
        pass


class Outbox:
    def __init__(self):
        self.sent, self.error = [], None

    def __call__(self, url, headers, body):
        self.sent.append((url, headers, body))
        return self.error


class FakeDict(dict):
    """The subset of modal.Dict the ledger uses."""
    def put(self, key, value, *, skip_if_exists=False):
        if skip_if_exists and key in self:
            return False
        self[key] = json.loads(json.dumps(value))
        return True

    def get(self, key, default=None):
        value = dict.get(self, key, default)
        return json.loads(json.dumps(value))


@pytest.fixture(params=["sqlite", "dict"])
def env(request, tmp_path):
    backend, outbox = Backend(), Outbox()
    store = Store(str(tmp_path / "jobs.sqlite")) if request.param == "sqlite" else DictStore(FakeDict(), FakeDict())

    def make(**options):
        app = create_app(token=TOKEN, targets={"square": ("demo", "square")}, store=store, backend=backend,
                         webhook_secret=SECRET, sender=outbox, **options)
        return TestClient(app), app
    client, app = make()
    return client, app, backend, outbox, make


def submit(client, key="k1", value=3, hook=HOOK, owner=None):
    body = {"function": "square", "input": {"value": value}}
    if hook:
        body["on_complete"] = {"url": hook}
    headers = {**AUTH, "Idempotency-Key": key, **({"Bridge-Owner": owner} if owner else {})}
    return client.post("/v1/jobs", headers=headers, json=body)


def test_callback_is_signed_and_delivered_without_client_polling(env):
    client, app, backend, outbox, _ = env
    job = submit(client).json()
    assert job["callback"]["status"] == "waiting" and job["target"] == "demo/square"
    assert job["created_at"] <= job["submitted_at"] and "completed_at" not in job
    app.state.reconciler.tick()
    assert outbox.sent == []
    backend.results["fc-1"] = {"status": "succeeded", "result": {"value": 9}}
    app.state.reconciler.tick()
    (url, headers, body), = outbox.sent
    event = json.loads(body)
    assert url == HOOK and event["type"] == "job.succeeded" and event["job"]["result"] == {"value": 9}
    signed = f'{headers["webhook-id"]}.{headers["webhook-timestamp"]}.'.encode() + body
    expected = base64.b64encode(hmac.new(b"k" * 32, signed, hashlib.sha256).digest()).decode()
    assert headers["webhook-signature"] == "v1," + expected and headers["webhook-id"] == job["id"]
    app.state.reconciler.tick()
    assert len(outbox.sent) == 1
    done = client.get(f'/v1/jobs/{job["id"]}', headers=AUTH).json()
    assert done["callback"]["status"] == "delivered" and done["completed_at"] >= done["submitted_at"]


def test_failed_deliveries_back_off_then_give_up(env):
    client, app, backend, outbox, _ = env
    job = submit(client).json()
    backend.results["fc-1"] = {"status": "failed", "error": "boom"}
    outbox.error = "HTTP 500"
    now = 1_000_000_000.0
    app.state.store.update(job["id"], backend.results["fc-1"], now=now)
    for attempt, delay in enumerate(BACKOFF, start=1):
        app.state.reconciler.tick(now)
        assert len(outbox.sent) == attempt
        app.state.reconciler.tick(now + delay - 1)
        assert len(outbox.sent) == attempt, "retried before the backoff elapsed"
        now += delay
    app.state.reconciler.tick(now)
    state = app.state.store.get(job["id"])["callback"]
    assert state["status"] == "failed" and state["attempts"] == len(BACKOFF) + 1 and state["last_error"] == "HTTP 500"
    app.state.reconciler.tick(now + 10 ** 6)
    assert len(outbox.sent) == len(BACKOFF) + 1


def test_callback_validation_and_idempotency_scope(env):
    client, _, backend, _, make = env
    assert submit(client, hook="http://remote.example/x").status_code == 422
    assert submit(client, hook="https://user:pw@app.example/x").status_code == 422
    assert backend.calls == []
    first = submit(client)
    assert submit(client).json()["id"] == first.json()["id"]
    assert submit(client, hook=HOOK + "2").status_code == 409
    assert submit(client, owner="someone-else").status_code == 409
    restricted, _ = make(callback_hosts=["hooks.example", "*.workers.dev"])
    assert submit(restricted, key="k2").status_code == 422
    assert submit(restricted, key="k2", hook="https://a.workers.dev/done").status_code == 202
    assert check_callback_url("http://localhost:8787/hook") is None
    no_secret = TestClient(create_app(token=TOKEN, targets={"square": ("demo", "square")},
                                      store=DictStore(FakeDict(), FakeDict()), backend=backend))
    assert submit(no_secret, key="k3").status_code == 422


def test_uncertain_submission_resolution(env):
    client, app, backend, outbox, _ = env
    backend.submit_error = True
    job = submit(client).json()
    assert job["status"] == "submission_unknown"
    url = f'/v1/jobs/{job["id"]}/resolve'
    listed = client.get("/v1/jobs", headers=AUTH, params={"status": "submission_unknown"}).json()["jobs"]
    assert [j["id"] for j in listed] == [job["id"]]
    backend.submit_error = False
    assert client.post(url, headers=AUTH, json={"action": "attach"}).status_code == 422
    assert client.post(url, headers=AUTH, json={"action": "attach", "call_id": "fc-missing"}).status_code == 502
    assert client.post(url, headers=AUTH, json={"action": "resubmit"}).status_code == 422
    assert client.post(url, headers=AUTH, json={"action": "resubmit", "input": {"value": 4}}).status_code == 409
    assert len(backend.calls) == 1
    resubmitted = client.post(url, headers=AUTH, json={"action": "resubmit", "input": {"value": 3}}).json()
    assert resubmitted["status"] == "pending" and len(backend.calls) == 2
    assert client.post(url, headers=AUTH, json={"action": "abandon"}).status_code == 409
    backend.results["fc-2"] = {"status": "succeeded", "result": 9}
    app.state.reconciler.tick()
    assert json.loads(outbox.sent[0][2])["type"] == "job.succeeded"


def test_attach_and_abandon(env):
    client, app, backend, outbox, _ = env
    backend.submit_error = True
    one, two = submit(client, "a").json(), submit(client, "b").json()
    backend.results["fc-found"] = {"status": "succeeded", "result": 1}
    attached = client.post(f'/v1/jobs/{one["id"]}/resolve', headers=AUTH,
                           json={"action": "attach", "call_id": "fc-found"}).json()
    assert attached["status"] == "succeeded" and attached["call_id"] == "fc-found"
    abandoned = client.post(f'/v1/jobs/{two["id"]}/resolve', headers=AUTH, json={"action": "abandon"}).json()
    assert abandoned["status"] == "failed" and len(backend.calls) == 2
    app.state.reconciler.tick()
    assert sorted(json.loads(body)["type"] for _, _, body in outbox.sent) == ["job.failed", "job.succeeded"]


def test_crashed_reservation_is_quarantined_then_resolvable(env):
    client, app, backend, _, _ = env
    store = app.state.store
    row, _ = store.reserve("crashed", "print", "square", now=1000.0)
    assert client.post(f'/v1/jobs/{row["id"]}/resolve', headers=AUTH, json={"action": "abandon"}).status_code == 200
    fresh, _ = store.reserve("fresh", "print", "square")
    assert client.post(f'/v1/jobs/{fresh["id"]}/resolve', headers=AUTH, json={"action": "abandon"}).status_code == 409
    stale, _ = store.reserve("stale", "print", "square", now=1000.0)
    app.state.reconciler.tick()
    assert store.get(stale["id"])["state"]["status"] == "submission_unknown"
    assert store.get(fresh["id"])["state"]["status"] == "submitting" and backend.calls == []


def test_owner_quotas(env):
    client, _, backend, _, make = env
    limited, _ = make(owner_max_active=1, owner_max_daily=2)
    first = submit(limited, "a", owner="u1")
    assert first.status_code == 202 and first.json()["owner"] == "u1"
    assert submit(limited, "b", owner="u1").status_code == 429
    assert submit(limited, "a", owner="u1").status_code == 202, "replays are never throttled"
    assert submit(limited, "c", owner="u2").status_code == 202
    backend.results["fc-1"] = {"status": "succeeded", "result": 1}
    limited.get(f'/v1/jobs/{first.json()["id"]}', headers=AUTH)
    assert submit(limited, "d", owner="u1").status_code == 202
    backend.results["fc-3"] = {"status": "succeeded", "result": 1}
    limited.get(f'/v1/jobs/{submit(limited, "d", owner="u1").json()["id"]}', headers=AUTH)
    assert submit(limited, "e", owner="u1").status_code == 429
    assert len(backend.calls) == 3


def test_concurrency_limit_rejects_before_reserving(env):
    client, _, backend, _, make = env
    limited, app = make(max_modal_concurrency=1)
    app.state.meter.wait_seconds = 0.05
    entered, release = threading.Event(), threading.Event()
    original = backend.submit

    def slow(target, payload):
        entered.set()
        release.wait(5)
        return original(target, payload)
    backend.submit = slow
    worker = threading.Thread(target=lambda: submit(limited, "slow"))
    worker.start()
    assert entered.wait(5)
    rejected = submit(limited, "fast")
    assert rejected.status_code == 503 and rejected.headers["retry-after"] == "1"
    release.set(); worker.join()
    assert submit(limited, "fast").status_code == 202, "the rejected key was not consumed"
    metrics = limited.get("/v1/metrics", headers=AUTH).json()
    assert metrics["modal"]["saturated"] == 1 and metrics["modal"]["calls"]["submit"]["count"] == 2
    assert metrics["jobs"]["pending"] == 2 and limited.get("/v1/metrics").status_code == 401


def test_cancel_then_callback_reports_the_real_outcome(env):
    client, app, backend, outbox, _ = env
    job = submit(client).json()
    assert client.post(f'/v1/jobs/{job["id"]}/cancel', headers=AUTH).json()["status"] == "cancel_requested"
    app.state.reconciler.tick()
    assert outbox.sent == []
    backend.results["fc-1"] = {"status": "succeeded", "result": 9}
    app.state.reconciler.tick()
    assert json.loads(outbox.sent[0][2])["type"] == "job.succeeded"


def test_settle_blocks_then_delivers(env):
    client, app, backend, outbox, _ = env
    job = submit(client).json()
    backend.results["fc-1"] = {"status": "succeeded", "result": 9}
    app.state.reconciler.settle(job["id"], deadline=time.time() + 5)
    assert len(outbox.sent) == 1


def test_v01_database_is_upgraded_in_place(tmp_path):
    path = str(tmp_path / "old.sqlite")
    db = sqlite3.connect(path)
    db.execute('''CREATE TABLE jobs (id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
        function TEXT NOT NULL, call_id TEXT, state TEXT NOT NULL)''')
    old = hashlib.sha256(b'{"function":"square","input":{"value":3}}').hexdigest()
    db.execute("INSERT INTO jobs VALUES ('old-1','k1',?,'square','fc-1',?)", (old, json.dumps({"status": "pending"})))
    db.commit(); db.close()
    backend = Backend()
    backend.results["fc-1"] = {"status": "succeeded", "result": {"value": 9}}
    client = TestClient(create_app(token=TOKEN, targets={"square": ("demo", "square")}, db_path=path, backend=backend))
    assert submit(client, hook=None).json()["id"] == "old-1" and backend.calls == []
    done = client.get("/v1/jobs/old-1", headers=AUTH).json()
    assert done["result"] == {"value": 9} and "completed_at" in done and "created_at" not in done


def test_retention_frees_settled_jobs(tmp_path):
    store = Store(str(tmp_path / "jobs.sqlite"))
    old, _ = store.reserve("old", "p", "square", now=100.0)
    store.attach(old["id"], "fc-1", now=100.0)
    store.update(old["id"], {"status": "succeeded", "result": 1}, now=200.0)
    live, _ = store.reserve("live", "p", "square", now=100.0)
    assert store.purge(before=300.0) == 1
    assert store.find("old") is None and store.find("live")["id"] == live["id"]


def test_standard_webhooks_signature_vector():
    assert sign("plain-secret-of-sufficient-length", "msg", "1700000000", b"{}") == "v1," + base64.b64encode(
        hmac.new(b"plain-secret-of-sufficient-length", b"msg.1700000000.{}", hashlib.sha256).digest()).decode()

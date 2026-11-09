"""Offline Swagger playground. Never imports or calls Modal; not a deployment mode."""
import threading
import uuid
from tempfile import TemporaryDirectory
from contextlib import asynccontextmanager
from .app import create_app

DEMO_TOKEN = "demo-only-token-do-not-use-in-production"


class DemoBackend:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def submit(self, target, payload):
        call_id = str(uuid.uuid4())
        value = payload.get("value") if isinstance(payload, dict) else None
        state = ({"status": "succeeded", "result": {"value": value * value}}
                 if type(value) in (int, float) and abs(value) <= 1e100 else
                 {"status": "failed", "error": "Expected {value: number} with magnitude <= 1e100"})
        with self.lock:
            self.jobs[call_id] = state
        return call_id

    def poll(self, call_id):
        with self.lock:
            return self.jobs[call_id]

    def cancel(self, call_id):
        with self.lock:
            self.jobs[call_id] = {"status": "failed", "error": "Canceled in local demo"}


def create_demo_app():
    directory = TemporaryDirectory(prefix="modal-bridge-demo-")
    app = create_app(token=DEMO_TOKEN, targets={"square": ("demo", "square")},
                     db_path=f"{directory.name}/jobs.sqlite", backend=DemoBackend())
    app.title = "Modal Bridge"
    app.description = ("**Offline playground. No Modal account, network calls, or compute charges.** "
                       "State is discarded when this process exits. Authorize with `" + DEMO_TOKEN +
                       "`, submit the square example, then poll its returned ID. "
                       "Do not expose this demo server publicly.")

    @asynccontextmanager
    async def lifespan(app):
        yield
        directory.cleanup()

    app.router.lifespan_context = lifespan
    return app

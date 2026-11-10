"""Single-workspace gateway. SQLite must live on persistent, local storage."""
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import uuid
from contextlib import closing
from typing import Any, Literal, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, JsonValue

log = logging.getLogger(__name__)


class Backend(Protocol):
    def submit(self, target: tuple[str, str], payload: Any) -> str: ...
    def poll(self, call_id: str) -> dict: ...
    def cancel(self, call_id: str) -> None: ...


class ModalBackend:
    def submit(self, target, payload):
        import modal
        return modal.Function.from_name(*target).spawn(payload).object_id

    def poll(self, call_id):
        import modal
        try:
            result = modal.FunctionCall.from_id(call_id).get(timeout=0)
        except modal.exception.FunctionTimeoutError:
            return {"status": "failed", "error": "Function timed out"}
        except modal.exception.OutputExpiredError:
            return {"status": "expired", "error": "Modal result expired"}
        except TimeoutError:
            return {"status": "pending"}
        except (modal.exception.RemoteError, modal.exception.ExecutionError):
            log.exception("Remote execution failed: %s", call_id)
            return {"status": "failed", "error": "Remote execution failed; inspect Modal logs"}
        except (modal.exception.Error, ConnectionError):
            # SDK/transport failures are not evidence that the job failed.
            raise
        except Exception:
            log.exception("Remote invocation failed: %s", call_id)
            return {"status": "failed", "error": "Remote function failed; inspect Modal logs"}
        try:
            encoded = json.dumps(result, allow_nan=False)
            if len(encoded.encode()) > 1_048_576:
                return {"status": "failed", "error": "Result exceeds 1 MiB; return an object-storage reference"}
        except (TypeError, ValueError):
            return {"status": "failed", "error": "Function returned a non-JSON result"}
        return {"status": "succeeded", "result": result}

    def cancel(self, call_id):
        import modal
        modal.FunctionCall.from_id(call_id).cancel(terminate_containers=False)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"function": "square", "input": {"value": 12}}]
    })
    function: str = Field(min_length=1, max_length=100)
    input: JsonValue


class Job(BaseModel):
    """A durable gateway job. `pending` includes both queued and running work."""
    id: str = Field(description="Gateway job ID; distinct from a Modal call ID")
    function: str = Field(description="Allowlisted function alias")
    status: Literal["submitting", "submission_unknown", "pending", "succeeded",
                    "failed", "expired", "cancel_requested"]
    result: JsonValue = Field(default=None, description="Present on success; JSON null is a valid result")
    error: str | None = Field(default=None, description="Sanitized failure description")
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"id": "a7bdf038-4594-4a30-b498-367005e7b561", "function": "square", "status": "pending"},
        {"id": "a7bdf038-4594-4a30-b498-367005e7b561", "function": "square", "status": "succeeded", "result": {"value": 144}}
    ]})


class APIError(BaseModel):
    detail: str


class Health(BaseModel):
    ok: bool


JOB_RESPONSES = {
    401: {"model": APIError, "description": "Missing or invalid gateway bearer token"},
    404: {"model": APIError, "description": "Unknown job or function alias"},
}


class Store:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                function TEXT NOT NULL, call_id TEXT, state TEXT NOT NULL)''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def reserve(self, key, fingerprint, function):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(409, "Idempotency key already used for different input")
                return dict(existing), False
            job_id = str(uuid.uuid4())
            state = json.dumps({"status": "submitting"})
            db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, NULL, ?)",
                       (job_id, key, fingerprint, function, state))
            return dict(id=job_id, function=function, call_id=None, state=state), True

    def get(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Job not found")
        return dict(row)

    def attach(self, job_id, call_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE jobs SET call_id=?, state=? WHERE id=?",
                       (call_id, json.dumps({"status": "pending"}), job_id))

    def update(self, job_id, state):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            # Do not overwrite a terminal result with a concurrent stale poll.
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if json.loads(row[0])["status"] in {"submitting", "pending", "cancel_requested"}:
                db.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps(state), job_id))


def view(row):
    return {"id": row["id"], "function": row["function"], **json.loads(row["state"])}


def create_app(*, token: str, targets: dict[str, tuple[str, str]], db_path: str,
               backend: Backend | None = None):
    if len(token) < 32:
        raise ValueError("BRIDGE_TOKEN must contain at least 32 characters")
    if not targets or any(not isinstance(k, str) or not isinstance(v, (list, tuple))
                          or len(v) != 2 or not all(isinstance(s, str) and s for s in v)
                          for k, v in targets.items()):
        raise ValueError("Targets must map aliases to [app_name, function_name]")
    store = Store(db_path)
    backend = backend or ModalBackend()
    app = FastAPI(
        title="Modal Bridge", version="0.1.0",
        summary="An HTTP job API for Modal and edge runtimes",
        description=("Submit an allowlisted function, keep its job ID, and poll for a result. "
                     "Use **Authorize** with your gateway token (without the Bearer prefix). "
                     "Reuse the same Idempotency-Key when retrying a submission. "
                     "This API serves one trusted workspace; it is not a multi-tenant API."),
        license_info={"name": "MIT", "identifier": "MIT"},
        openapi_tags=[
            {"name": "Jobs", "description": "Submit, inspect, and request cancellation."},
            {"name": "Operations", "description": "Gateway health; no Modal compute is invoked."},
        ],
        swagger_ui_parameters={"displayRequestDuration": True, "docExpansion": "list",
                               "defaultModelsExpandDepth": -1, "persistAuthorization": False},
    )
    bearer = HTTPBearer(auto_error=False, scheme_name="GatewayToken",
                        description="Your BRIDGE_TOKEN. Enter the token only; Swagger adds Bearer.")

    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, "Invalid gateway token", headers={"WWW-Authenticate": "Bearer"})

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        # Buffer at most 1 MiB even for chunked submissions.
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1_048_576:
                    from fastapi.responses import JSONResponse
                    return JSONResponse({"detail": "Request exceeds 1 MiB"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health", tags=["Operations"], response_model=Health,
             operation_id="getHealth", summary="Check gateway liveness")
    def health():
        return {"ok": True}

    @app.post("/v1/jobs", status_code=202, dependencies=[Depends(authenticate)],
              tags=["Jobs"], response_model=Job, response_model_exclude_unset=True,
              operation_id="submitJob", summary="Submit an allowlisted function",
              description="Returns the original job for a repeated key and identical input. "
                          "A 202 response does not guarantee submission succeeded: inspect status. "
                          "Never automatically retry submission_unknown with a new key.",
              responses={**JOB_RESPONSES,
                         409: {"model": APIError, "description": "Key already used with different input"},
                         413: {"model": APIError, "description": "Body exceeds 1 MiB"}})
    def submit(body: Submission, idempotency_key: str = Header(min_length=1, max_length=200, description="Stable unique request key. Reuse it for retries of the same input.", examples=["square-demo-001"])):
        if body.function not in targets:
            raise HTTPException(404, "Function alias is not allowlisted")
        try:
            canonical = json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        except ValueError:
            raise HTTPException(422, "Input must be finite JSON")
        row, created = store.reserve(idempotency_key, hashlib.sha256(canonical.encode()).hexdigest(), body.function)
        if created:
            try:
                call_id = backend.submit(tuple(targets[body.function]), body.input)
                store.attach(row["id"], call_id)
            except Exception:
                log.exception("Submission outcome uncertain for job %s", row["id"])
                store.update(row["id"], {"status": "submission_unknown",
                    "error": "Submission outcome uncertain; inspect Modal before submitting a new key"})
        return view(store.get(row["id"]))

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(authenticate)],
             tags=["Jobs"], response_model=Job, response_model_exclude_unset=True,
             operation_id="getJob", summary="Poll a job and retrieve its result",
             description="Refreshes pending work from Modal and caches terminal results. "
                         "A 502 is a polling failure, not a failed job; retry this GET.",
             responses={**JOB_RESPONSES, 502: {"model": APIError, "description": "Modal unavailable"}})
    def get(job_id: str):
        row = store.get(job_id)
        state = json.loads(row["state"])
        if row["call_id"] and state["status"] in {"pending", "cancel_requested"}:
            try:
                latest = backend.poll(row["call_id"])
                if latest["status"] != "pending":
                    store.update(job_id, latest)
            except Exception:
                log.exception("Unable to poll %s", job_id)
                raise HTTPException(502, "Modal unavailable; retry this status request")
        return view(store.get(job_id))

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(authenticate)],
              tags=["Jobs"], response_model=Job, response_model_exclude_unset=True,
              operation_id="cancelJob", summary="Request job cancellation",
              description="Best effort. A job can finish while cancellation is being requested. "
                          "Does not terminate containers shared with other jobs.",
              responses={**JOB_RESPONSES,
                         502: {"model": APIError, "description": "Cancellation outcome uncertain"},
                         413: {"model": APIError, "description": "Body exceeds 1 MiB"}})
    def cancel(job_id: str):
        row = store.get(job_id)
        if json.loads(row["state"])["status"] == "pending" and row["call_id"]:
            try:
                backend.cancel(row["call_id"])
            except Exception:
                raise HTTPException(502, "Cancellation outcome uncertain; retry cancellation or check Modal")
            store.update(job_id, {"status": "cancel_requested"})
        return view(store.get(job_id))

    from .runs import Runs, router
    app.state.runs = Runs(store, targets, backend)
    app.include_router(router(app.state.runs, authenticate))
    return app


def from_env():
    from dotenv import load_dotenv
    load_dotenv(os.environ.get("BRIDGE_ENV_FILE", ".env"), override=False)
    path = os.environ.get("BRIDGE_DB", "data/jobs.sqlite")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return create_app(token=os.environ["BRIDGE_TOKEN"],
                      targets=json.loads(os.environ["BRIDGE_TARGETS"]), db_path=path)

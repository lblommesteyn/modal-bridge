"""Single-workspace gateway: an HTTP job contract in front of the Modal Python SDK."""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from contextlib import contextmanager, nullcontext
from typing import Any, Literal, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .reconcile import Reconciler, check_callback_url, send
from .store import SETTLED, DictStore, Store  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)


class Backend(Protocol):
    def submit(self, target: tuple[str, str], payload: Any) -> str: ...
    def poll(self, call_id: str) -> dict: ...
    def cancel(self, call_id: str) -> None: ...


class ModalBackend:
    def submit(self, target, payload):
        import modal
        return modal.Function.from_name(*target).spawn(payload).object_id

    def poll(self, call_id, timeout=0):
        import modal
        try:
            result = modal.FunctionCall.from_id(call_id).get(timeout=timeout)
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


class Callback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2000, description="HTTPS endpoint that receives one signed POST when the job settles")


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"function": "square", "input": {"value": 12}}]
    })
    function: str = Field(min_length=1, max_length=100)
    input: JsonValue
    on_complete: Callback | None = Field(default=None, description="Optional completion callback")


class CallbackState(BaseModel):
    url: str
    status: Literal["waiting", "pending", "delivered", "failed"] = Field(
        description="waiting: job not settled. pending: delivery due or being retried")
    attempts: int
    next_attempt_at: float | None = None
    delivered_at: float | None = None
    last_error: str | None = None


class Job(BaseModel):
    """A durable gateway job. `pending` includes both queued and running work."""
    id: str = Field(description="Gateway job ID; distinct from a Modal call ID")
    function: str = Field(description="Allowlisted function alias")
    status: Literal["submitting", "submission_unknown", "pending", "succeeded",
                    "failed", "expired", "cancel_requested"]
    result: JsonValue = Field(default=None, description="Present on success; JSON null is a valid result")
    error: str | None = Field(default=None, description="Sanitized failure description")
    target: str | None = Field(default=None, description="Modal app/function the alias resolved to at submission")
    owner: str | None = Field(default=None, description="Caller-supplied Bridge-Owner value")
    call_id: str | None = Field(default=None, description="Modal FunctionCall ID, for finding the call in Modal")
    created_at: float | None = Field(default=None, description="Unix seconds; reservation time")
    submitted_at: float | None = Field(default=None, description="Unix seconds; Modal accepted the call")
    completed_at: float | None = Field(default=None, description="Unix seconds; the gateway recorded a settled state")
    updated_at: float | None = None
    callback: CallbackState | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"id": "a7bdf038-4594-4a30-b498-367005e7b561", "function": "square", "status": "pending"},
        {"id": "a7bdf038-4594-4a30-b498-367005e7b561", "function": "square", "status": "succeeded", "result": {"value": 144}}
    ]})


class JobList(BaseModel):
    jobs: list[Job]


class Resolution(BaseModel):
    """How an operator settles a job whose submission outcome is uncertain."""
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"action": "attach", "call_id": "fc-01JABCDEF"},
        {"action": "resubmit", "input": {"value": 12}},
        {"action": "abandon"}]})
    action: Literal["attach", "resubmit", "abandon"]
    call_id: str | None = Field(default=None, min_length=1, max_length=200,
                                description="attach: the Modal FunctionCall ID you found for this job")
    input: JsonValue = Field(default=None, description="resubmit: the original input; it must match the stored hash")


class APIError(BaseModel):
    detail: str


class Health(BaseModel):
    ok: bool


JOB_RESPONSES = {
    401: {"model": APIError, "description": "Missing or invalid gateway bearer token"},
    404: {"model": APIError, "description": "Unknown job or function alias"},
}


def fingerprint(function, input, callback_url=None, owner=None):
    # Without a callback or owner this equals the v0.1 hash, so existing keys keep deduplicating.
    body = {"function": function, "input": input}
    if callback_url:
        body["on_complete"] = {"url": callback_url}
    if owner:
        body["owner"] = owner
    try:
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError:
        raise HTTPException(422, "Input must be finite JSON")
    return hashlib.sha256(canonical.encode()).hexdigest()


def view(row, result=True):
    out = {"id": row["id"], "function": row["function"], **row["state"]}
    if not result:
        out.pop("result", None)
    for name in ("target", "owner", "call_id", "created_at", "submitted_at", "completed_at", "updated_at", "callback"):
        if row.get(name) is not None:
            out[name] = row[name]
    return out


class Meter:
    """Bounds concurrent Modal SDK calls and records their latency."""

    def __init__(self, limit, wait_seconds=5.0):
        self.slots = threading.BoundedSemaphore(limit)
        self.limit, self.wait_seconds = limit, wait_seconds
        self.lock = threading.Lock()
        self.in_flight = self.saturated = 0
        self.calls = {}

    @contextmanager
    def call(self, name):
        if not self.slots.acquire(timeout=self.wait_seconds):
            with self.lock:
                self.saturated += 1
            raise HTTPException(503, "Gateway is at its Modal concurrency limit; retry shortly",
                                headers={"Retry-After": "1"})
        started = time.perf_counter()
        with self.lock:
            self.in_flight += 1
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            self.slots.release()
            with self.lock:
                self.in_flight -= 1
                stats = self.calls.setdefault(name, {"count": 0, "errors": 0, "total_ms": 0.0, "max_ms": 0.0})
                stats["count"] += 1
                stats["errors"] += failed
                stats["total_ms"] = round(stats["total_ms"] + elapsed, 3)
                stats["max_ms"] = round(max(stats["max_ms"], elapsed), 3)

    def snapshot(self):
        with self.lock:
            return {"limit": self.limit, "in_flight": self.in_flight, "saturated": self.saturated,
                    "calls": {name: dict(stats) for name, stats in self.calls.items()}}


def create_app(*, token: str, targets: dict[str, tuple[str, str]], db_path: str | None = None,
               backend: Backend | None = None, store=None, webhook_secret: str | None = None,
               callback_hosts: list[str] | None = None, owner_max_active: int | None = None,
               owner_max_daily: int | None = None, retention_days: float | None = None,
               max_modal_concurrency: int = 32, on_pending=None, sender=send):
    if len(token) < 32:
        raise ValueError("BRIDGE_TOKEN must contain at least 32 characters")
    if not targets or any(not isinstance(k, str) or not isinstance(v, (list, tuple))
                          or len(v) != 2 or not all(isinstance(s, str) and s for s in v)
                          for k, v in targets.items()):
        raise ValueError("Targets must map aliases to [app_name, function_name]")
    if webhook_secret is not None and len(webhook_secret) < 24:
        raise ValueError("BRIDGE_WEBHOOK_SECRET must contain at least 24 characters")
    store = store or Store(db_path)
    backend = backend or ModalBackend()
    meter = Meter(max_modal_concurrency)
    reconciler = Reconciler(store, backend, view, webhook_secret=webhook_secret, sender=sender,
                            retention_seconds=retention_days * 86400 if retention_days else None)
    app = FastAPI(
        title="Modal Bridge", version="0.2.0",
        summary="An HTTP job API for Modal and edge runtimes",
        description=("Submit an allowlisted function, keep its job ID, and either poll for the result "
                     "or receive a signed completion callback. "
                     "Use **Authorize** with your gateway token (without the Bearer prefix). "
                     "Reuse the same Idempotency-Key when retrying a submission. "
                     "This API serves one trusted workspace; it is not a multi-tenant API."),
        license_info={"name": "MIT", "identifier": "MIT"},
        openapi_tags=[
            {"name": "Jobs", "description": "Submit, inspect, and request cancellation."},
            {"name": "Operations", "description": "Health, metrics and reconciling uncertain submissions."},
        ],
        swagger_ui_parameters={"displayRequestDuration": True, "docExpansion": "list",
                               "defaultModelsExpandDepth": -1, "persistAuthorization": False},
    )
    app.state.store, app.state.reconciler, app.state.meter = store, reconciler, meter
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

    def spawn(row, payload):
        """Submit to Modal and record the call. Any failure leaves the outcome uncertain.
        Callers hold a meter slot, taken before the key was reserved."""
        try:
            call_id = backend.submit(tuple(targets[row["function"]]), payload)
            store.attach(row["id"], call_id)
        except Exception:
            log.exception("Submission outcome uncertain for job %s", row["id"])
            store.update(row["id"], {"status": "submission_unknown",
                "error": "Submission outcome uncertain; resolve this job before submitting a new key"},
                allowed=("submitting",))
            return
        if on_pending:
            try:
                on_pending(row["id"])
            except Exception:
                log.exception("Completion watcher did not start for %s; the reconciler will settle it", row["id"])

    def check_quota(owner):
        if owner_max_active is None and owner_max_daily is None:
            return
        active, recent = store.usage(owner, time.time() - 86400)
        if owner_max_active is not None and active >= owner_max_active:
            raise HTTPException(429, "Owner has too many unfinished jobs", headers={"Retry-After": "5"})
        if owner_max_daily is not None and recent >= owner_max_daily:
            raise HTTPException(429, "Owner reached the daily job limit", headers={"Retry-After": "3600"})

    @app.post("/v1/jobs", status_code=202, dependencies=[Depends(authenticate)],
              tags=["Jobs"], response_model=Job, response_model_exclude_unset=True,
              operation_id="submitJob", summary="Submit an allowlisted function",
              description="Returns the original job for a repeated key and identical input. "
                          "A 202 response does not guarantee submission succeeded: inspect status. "
                          "Never automatically retry submission_unknown with a new key. "
                          "With on_complete, the gateway POSTs one signed event when the job settles.",
              responses={**JOB_RESPONSES,
                         409: {"model": APIError, "description": "Key already used with different input"},
                         413: {"model": APIError, "description": "Body exceeds 1 MiB"},
                         429: {"model": APIError, "description": "Owner quota reached"},
                         503: {"model": APIError, "description": "Gateway concurrency limit reached; nothing was submitted"}})
    def submit(body: Submission,
               idempotency_key: str = Header(min_length=1, max_length=200, description="Stable unique request key. Reuse it for retries of the same input.", examples=["square-demo-001"]),
               bridge_owner: str | None = Header(default=None, min_length=1, max_length=200, description="Optional end-user or tenant ID, set by your backend. Used for quotas and filtering; it is not authentication.")):
        if body.function not in targets:
            raise HTTPException(404, "Function alias is not allowlisted")
        callback_url = body.on_complete.url if body.on_complete else None
        if callback_url:
            if not webhook_secret:
                raise HTTPException(422, "Callbacks need BRIDGE_WEBHOOK_SECRET on the gateway")
            problem = check_callback_url(callback_url, callback_hosts)
            if problem:
                raise HTTPException(422, problem)
        digest = fingerprint(body.function, body.input, callback_url, bridge_owner)
        existing = store.find(idempotency_key)
        if existing is None:
            check_quota(bridge_owner)
        # Take a slot before reserving, so a saturated gateway never strands a key in `submitting`.
        with meter.call("submit") if existing is None else nullcontext():
            row, created = store.reserve(idempotency_key, digest, body.function, owner=bridge_owner,
                                         target="/".join(targets[body.function]), callback_url=callback_url)
            if created:
                spawn(row, body.input)
        return view(store.get(row["id"]))

    @app.get("/v1/jobs", dependencies=[Depends(authenticate)], tags=["Jobs"], response_model=JobList,
             response_model_exclude_unset=True, operation_id="listJobs", summary="List jobs, newest first",
             description="Results are omitted; read a job by ID for its result. "
                         "A Modal-hosted gateway lists unsettled jobs only.",
             responses={401: JOB_RESPONSES[401]})
    def list_jobs(status: Literal["submitting", "submission_unknown", "pending", "succeeded", "failed",
                                  "expired", "cancel_requested"] | None = None,
                  owner: str | None = Query(default=None, max_length=200),
                  limit: int = Query(default=50, ge=1, le=500)):
        return {"jobs": [view(row, result=False) for row in store.list(status, owner, limit)]}

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(authenticate)],
             tags=["Jobs"], response_model=Job, response_model_exclude_unset=True,
             operation_id="getJob", summary="Poll a job and retrieve its result",
             description="Refreshes pending work from Modal and caches terminal results. "
                         "A 502 is a polling failure, not a failed job; retry this GET.",
             responses={**JOB_RESPONSES, 502: {"model": APIError, "description": "Modal unavailable"},
                        503: {"model": APIError, "description": "Gateway concurrency limit reached"}})
    def get(job_id: str):
        row = store.get(job_id)
        if row["call_id"] and row["state"]["status"] in {"pending", "cancel_requested"}:
            try:
                with meter.call("poll"):
                    reconciler.refresh(row)
            except HTTPException:
                raise
            except Exception:
                log.exception("Unable to poll %s", job_id)
                raise HTTPException(502, "Modal unavailable; retry this status request")
        return view(store.get(job_id))

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(authenticate)],
              tags=["Jobs"], response_model=Job, response_model_exclude_unset=True,
              operation_id="cancelJob", summary="Request job cancellation",
              description="Best effort. A job can finish while cancellation is being requested, "
                          "so cancel_requested is not final: keep polling (or wait for the callback) "
                          "until the job is succeeded, failed or expired. "
                          "Does not terminate containers shared with other jobs.",
              responses={**JOB_RESPONSES,
                         502: {"model": APIError, "description": "Cancellation outcome uncertain"},
                         413: {"model": APIError, "description": "Body exceeds 1 MiB"}})
    def cancel(job_id: str):
        row = store.get(job_id)
        if row["state"]["status"] == "pending" and row["call_id"]:
            try:
                with meter.call("cancel"):
                    backend.cancel(row["call_id"])
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "Cancellation outcome uncertain; retry cancellation or check Modal")
            store.update(job_id, {"status": "cancel_requested"}, allowed=("pending",))
        return view(store.get(job_id))

    @app.post("/v1/jobs/{job_id}/resolve", dependencies=[Depends(authenticate)],
              tags=["Operations"], response_model=Job, response_model_exclude_unset=True,
              operation_id="resolveJob", summary="Settle a job whose submission outcome is uncertain",
              description="For submission_unknown jobs, and reservations stuck in submitting. "
                          "Look for the call in Modal first. **attach** records a call you found. "
                          "**abandon** marks the job failed. **resubmit** spawns it again and is the only "
                          "action that can run the work twice, so it requires the original input.",
              responses={**JOB_RESPONSES,
                         409: {"model": APIError, "description": "Job is not awaiting resolution, or input does not match"},
                         502: {"model": APIError, "description": "Modal could not verify the call ID"}})
    def resolve(job_id: str, body: Resolution):
        row = store.get(job_id)
        status = row["state"]["status"]
        if status != "submission_unknown" and not reconciler.is_stale(row):
            raise HTTPException(409, "Job is not awaiting resolution")
        seen = dict(allowed=(status,), if_updated_at=row.get("updated_at"))
        if body.action == "attach":
            if not body.call_id:
                raise HTTPException(422, "attach requires call_id")
            try:
                with meter.call("poll"):
                    latest = backend.poll(body.call_id)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "Modal could not verify that call ID; nothing changed")
            if not store.attach(job_id, body.call_id):
                raise HTTPException(409, "Job is not awaiting resolution")
            if latest["status"] != "pending":
                store.update(job_id, latest)
            elif on_pending:
                on_pending(job_id)
        elif body.action == "abandon":
            if not store.update(job_id, {"status": "failed", "error": "Abandoned after an uncertain submission"}, **seen):
                raise HTTPException(409, "Job is not awaiting resolution")
        else:
            if "input" not in body.model_fields_set:
                raise HTTPException(422, "resubmit requires the original input")
            callback_url = row["callback"]["url"] if row.get("callback") else None
            if fingerprint(row["function"], body.input, callback_url, row.get("owner")) != row["fingerprint"]:
                raise HTTPException(409, "Input does not match the original submission")
            if row["function"] not in targets:
                raise HTTPException(404, "Function alias is not allowlisted")
            with meter.call("submit"):
                if not store.update(job_id, {"status": "submitting"}, **seen):
                    raise HTTPException(409, "Job is not awaiting resolution")
                spawn(row, body.input)
        return view(store.get(job_id))

    @app.get("/v1/metrics", dependencies=[Depends(authenticate)], tags=["Operations"],
             operation_id="getMetrics", summary="Job counts and Modal call latency",
             description="Latency covers this process since it started. A Modal-hosted gateway "
                         "counts unsettled jobs only (`scope: unsettled`).",
             responses={401: JOB_RESPONSES[401]})
    def metrics() -> dict[str, Any]:
        return {**store.counts(), "modal": meter.snapshot()}

    if hasattr(store, "connect"):  # Batch runs use SQL and are available on the SQLite ledger only.
        from .runs import Runs, router
        app.state.runs = Runs(store, targets, backend)
        app.include_router(router(app.state.runs, authenticate))
    return app


def _int(name):
    value = os.environ.get(name)
    return int(value) if value else None


def from_env(store=None, **overrides):
    if store is None:
        from dotenv import load_dotenv
        load_dotenv(os.environ.get("BRIDGE_ENV_FILE", ".env"), override=False)
        path = os.environ.get("BRIDGE_DB", "data/jobs.sqlite")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        overrides["db_path"] = path
    hosts = [host.strip() for host in os.environ.get("BRIDGE_CALLBACK_HOSTS", "").split(",") if host.strip()]
    retention = os.environ.get("BRIDGE_RETENTION_DAYS")
    return create_app(token=os.environ["BRIDGE_TOKEN"], targets=json.loads(os.environ["BRIDGE_TARGETS"]),
                      store=store, webhook_secret=os.environ.get("BRIDGE_WEBHOOK_SECRET") or None,
                      callback_hosts=hosts or None, owner_max_active=_int("BRIDGE_OWNER_MAX_ACTIVE"),
                      owner_max_daily=_int("BRIDGE_OWNER_MAX_DAILY"),
                      retention_days=float(retention) if retention else None,
                      max_modal_concurrency=_int("BRIDGE_MAX_MODAL_CONCURRENCY") or 32, **overrides)

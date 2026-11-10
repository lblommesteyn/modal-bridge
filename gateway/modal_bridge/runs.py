"""Versioned manifests and a single-host, resumable batch scheduler."""
import hashlib
import json
import time
import uuid
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError:
        raise HTTPException(422, "Inputs must be finite JSON")


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=200)
    input: JsonValue


class RunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "name": "handbook", "function": "documents", "revision": "bge-small-chunks-v1",
        "namespace": "handbook", "max_in_flight": 2,
        "items": [{"key": "intro", "input": {"text": "Our handbook explains how we work."}}]
    }]})
    name: str = Field(min_length=1, max_length=100)
    function: str = Field(min_length=1, max_length=100)
    revision: str = Field(min_length=1, max_length=200, description="Change when code, model, prompts or preprocessing change")
    namespace: str = Field(default="default", min_length=1, max_length=100)
    max_in_flight: int = Field(default=4, ge=1, le=32)
    reuse_successes: bool = True
    items: list[Item] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_keys(self):
        if len({item.key for item in self.items}) != len(self.items):
            raise ValueError("Item keys must be unique within a run")
        return self


class RunView(BaseModel):
    id: str
    name: str
    function: str
    revision: str
    namespace: str
    mode: Literal["active", "paused"]
    status: Literal["running", "paused", "needs_attention", "completed"]
    max_in_flight: int
    counts: dict[str, int]
    total: int
    created_at: float


class Preview(BaseModel):
    total: int
    reusable: int
    to_compute: int
    items: list[dict]


class Replay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[str] | None = Field(default=None, min_length=1, max_length=1000)


class Runs:
    def __init__(self, store, targets, backend):
        self.store, self.targets, self.backend = store, targets, backend
        with closing(store.connect()) as db, db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, request_key TEXT UNIQUE, fingerprint TEXT NOT NULL,
              spec TEXT NOT NULL, target TEXT NOT NULL, mode TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS run_items (
              run_id TEXT NOT NULL, item_key TEXT NOT NULL, input TEXT NOT NULL,
              cache_key TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
              call_id TEXT, result TEXT, error TEXT, reused_from TEXT,
              PRIMARY KEY(run_id,item_key));
            CREATE TABLE IF NOT EXISTS run_attempts (
              run_id TEXT NOT NULL, item_key TEXT NOT NULL, attempt INTEGER NOT NULL,
              state TEXT NOT NULL, call_id TEXT, error TEXT, started REAL, finished REAL,
              PRIMARY KEY(run_id,item_key,attempt));
            CREATE TABLE IF NOT EXISTS run_cache (
              key TEXT PRIMARY KEY, result TEXT NOT NULL, source TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS run_items_state ON run_items(state);
            ''')

    def target(self, spec):
        if spec.function not in self.targets:
            raise HTTPException(404, "Function alias is not allowlisted")
        return list(self.targets[spec.function])

    def key(self, spec, target, item):
        return digest([spec.namespace, spec.function, target, spec.revision, item.input])

    def preview(self, spec):
        target = self.target(spec)
        with closing(self.store.connect()) as db:
            items = []
            for item in spec.items:
                hit = db.execute("SELECT source FROM run_cache WHERE key=?", (self.key(spec, target, item),)).fetchone() if spec.reuse_successes else None
                items.append({"key": item.key, "action": "reuse" if hit else "compute",
                              "source": hit[0] if hit else None})
        reused = sum(i["action"] == "reuse" for i in items)
        return {"total": len(items), "reusable": reused, "to_compute": len(items)-reused, "items": items}

    def create(self, spec, request_key):
        target = self.target(spec)
        fingerprint = digest([spec.model_dump(), target])
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,fingerprint FROM runs WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(409, "Run key already used for a different manifest")
                run_id = existing["id"]
            else:
                run_id = str(uuid.uuid4())
                metadata = spec.model_dump(exclude={"items"})
                db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", (run_id, request_key, fingerprint,
                    canonical(metadata), canonical(target), "active", time.time()))
                for item in spec.items:
                    key = self.key(spec, target, item)
                    hit = db.execute("SELECT * FROM run_cache WHERE key=?", (key,)).fetchone() if spec.reuse_successes else None
                    db.execute("INSERT INTO run_items(run_id,item_key,input,cache_key,state,result,reused_from) VALUES (?,?,?,?,?,?,?)",
                        (run_id, item.key, canonical(item.input), key, "reused" if hit else "queued",
                         hit["result"] if hit else None, hit["source"] if hit else None))
        return self.get(run_id)

    def row(self, db, run_id):
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Run not found")
        return row

    def get(self, run_id):
        with closing(self.store.connect()) as db:
            row = self.row(db, run_id)
            counts = {r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM run_items WHERE run_id=? GROUP BY state", (run_id,))}
        spec = json.loads(row["spec"])
        if counts.get("submission_unknown"):
            status = "needs_attention"
        elif counts.get("queued") or counts.get("pending") or counts.get("submitting"):
            status = "paused" if row["mode"] == "paused" else "running"
        elif counts.get("failed") or counts.get("expired") or counts.get("submission_unknown"):
            status = "needs_attention"
        else:
            status = "completed"
        return {"id": run_id, **{k: spec[k] for k in ("name", "function", "revision", "namespace", "max_in_flight")},
                "mode": row["mode"], "status": status, "counts": counts,
                "total": sum(counts.values()), "created_at": row["created"]}

    def items(self, run_id, offset=0, limit=100):
        with closing(self.store.connect()) as db:
            self.row(db, run_id)
            rows = db.execute("SELECT * FROM run_items WHERE run_id=? ORDER BY item_key LIMIT ? OFFSET ?", (run_id, limit, offset)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["input"] = json.loads(item["input"])
                if item["result"] is not None:
                    item["result"] = json.loads(item["result"])
                item["attempts"] = [dict(a) for a in db.execute("SELECT * FROM run_attempts WHERE run_id=? AND item_key=? ORDER BY attempt", (run_id, row["item_key"]))]
                result.append(item)
            return result

    def mode(self, run_id, mode):
        with closing(self.store.connect()) as db, db:
            self.row(db, run_id)
            db.execute("UPDATE runs SET mode=? WHERE id=?", (mode, run_id))
        return self.get(run_id)

    def retry(self, run_id, keys):
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.row(db, run_id)
            rows = db.execute("SELECT item_key,state FROM run_items WHERE run_id=?", (run_id,)).fetchall()
            known = {row["item_key"]: row["state"] for row in rows}
            selected = keys if keys is not None else [k for k, state in known.items() if state == "failed"]
            if any(known.get(key) != "failed" for key in selected):
                raise HTTPException(409, "Only confirmed failed items can be retried; uncertain or expired work needs manual inspection")
            for key in selected:
                db.execute("UPDATE run_items SET state='queued',call_id=NULL,error=NULL WHERE run_id=? AND item_key=?", (run_id, key))
        return self.get(run_id)

    def recover(self):
        """Only call after acquiring the single scheduler lock, never from API startup."""
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE run_items SET state='submission_unknown',error='Scheduler stopped before recording the Modal call; inspect before resubmitting' WHERE state='submitting'")
            db.execute("UPDATE run_attempts SET state='submission_unknown',error='Submission interrupted',finished=? WHERE state='submitting'", (time.time(),))

    def tick(self, global_limit=32):
        # Polling is independent of clients and continues even when a run is paused.
        with closing(self.store.connect()) as db:
            pending = db.execute("SELECT * FROM run_items WHERE state='pending'").fetchall()
        for item in pending:
            try:
                state = self.backend.poll(item["call_id"])
            except Exception:
                continue  # Transport failure is not an execution failure.
            if state["status"] == "pending":
                continue
            result = canonical(state["result"]) if state["status"] == "succeeded" else None
            with closing(self.store.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE run_items SET state=?,result=?,error=? WHERE run_id=? AND item_key=? AND state='pending'",
                    (state["status"], result, state.get("error"), item["run_id"], item["item_key"]))
                db.execute("UPDATE run_attempts SET state=?,error=?,finished=? WHERE run_id=? AND item_key=? AND attempt=?",
                    (state["status"], state.get("error"), time.time(), item["run_id"], item["item_key"], item["attempt"]))
                if result is not None:
                    db.execute("INSERT OR REPLACE INTO run_cache VALUES (?,?,?)", (item["cache_key"], result, f'{item["run_id"]}/{item["item_key"]}'))
        with closing(self.store.connect()) as db:
            run_ids = [r[0] for r in db.execute("SELECT id FROM runs WHERE mode='active' ORDER BY created")]
        for run_id in run_ids:
            while True:
                with closing(self.store.connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    run = self.row(db, run_id)
                    spec = json.loads(run["spec"])
                    total = db.execute("SELECT count(*) FROM run_items WHERE state IN ('pending','submitting','submission_unknown')").fetchone()[0]
                    active = db.execute("SELECT count(*) FROM run_items WHERE run_id=? AND state IN ('pending','submitting','submission_unknown')", (run_id,)).fetchone()[0]
                    if run["mode"] != "active" or total >= global_limit or active >= spec["max_in_flight"]:
                        break
                    item = db.execute("SELECT * FROM run_items WHERE run_id=? AND state='queued' ORDER BY item_key LIMIT 1", (run_id,)).fetchone()
                    if item is None:
                        break
                    hit = db.execute("SELECT * FROM run_cache WHERE key=?", (item["cache_key"],)).fetchone() if spec["reuse_successes"] else None
                    if hit:
                        db.execute("UPDATE run_items SET state='reused',result=?,reused_from=? WHERE run_id=? AND item_key=?", (hit["result"],hit["source"],run_id,item["item_key"]))
                        continue
                    # Target changes require a new manifest, never silently route a queued item elsewhere.
                    if list(self.targets.get(spec["function"], [])) != json.loads(run["target"]):
                        db.execute("UPDATE runs SET mode='paused' WHERE id=?", (run_id,))
                        break
                    attempt = item["attempt"] + 1
                    db.execute("UPDATE run_items SET state='submitting',attempt=? WHERE run_id=? AND item_key=?", (attempt,run_id,item["item_key"]))
                    db.execute("INSERT INTO run_attempts VALUES (?,?,?,'submitting',NULL,NULL,?,NULL)", (run_id,item["item_key"],attempt,time.time()))
                try:
                    call_id = self.backend.submit(tuple(json.loads(run["target"])), json.loads(item["input"]))
                    state, error = "pending", None
                except Exception:
                    call_id, state, error = None, "submission_unknown", "Submission outcome uncertain; inspect Modal before creating another run"
                with closing(self.store.connect()) as db, db:
                    db.execute("UPDATE run_items SET state=?,call_id=?,error=? WHERE run_id=? AND item_key=?", (state,call_id,error,run_id,item["item_key"]))
                    db.execute("UPDATE run_attempts SET state=?,call_id=?,error=? WHERE run_id=? AND item_key=? AND attempt=?", (state,call_id,error,run_id,item["item_key"],attempt))


def router(runs, authenticate):
    from fastapi import Query
    api = APIRouter(prefix="/v1/runs", tags=["Runs"], dependencies=[Depends(authenticate)])

    @api.post("/preview", response_model=Preview, summary="Preview reuse without submitting work")
    def preview(body: RunSpec):
        return runs.preview(body)

    @api.post("", response_model=RunView, status_code=202, summary="Create a persistent versioned run")
    def create(body: RunSpec, idempotency_key: str = Header(min_length=1, max_length=200)):
        return runs.create(body, idempotency_key)

    @api.get("/{run_id}", response_model=RunView, summary="Get progress counts")
    def get(run_id: str):
        return runs.get(run_id)

    @api.get("/{run_id}/items", summary="Inspect persisted inputs, results, lineage and attempts")
    def items(run_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000)):
        return runs.items(run_id, offset, limit)

    @api.post("/{run_id}/pause", response_model=RunView, summary="Stop dispatching new items; active work continues")
    def pause(run_id: str):
        return runs.mode(run_id, "paused")

    @api.post("/{run_id}/resume", response_model=RunView, summary="Resume dispatching queued items")
    def resume(run_id: str):
        return runs.mode(run_id, "active")

    @api.post("/{run_id}/retry-failed", response_model=RunView, summary="Retry confirmed failures, preserving successes and attempt history")
    def retry(run_id: str, body: Replay):
        return runs.retry(run_id, body.keys)

    return api

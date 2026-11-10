"""Job ledgers. SQLite for a single host; a Modal Dict pair for a gateway hosted on Modal.

A record is a plain dict:
  id, key, fingerprint, function, target, owner, call_id,
  state {status, result?, error?}, callback {url, status, attempts, ...} | None,
  created_at, submitted_at, completed_at, updated_at   (unix seconds)
"""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager

from fastapi import HTTPException

OPEN = ("submitting", "pending", "cancel_requested")
SETTLED = ("succeeded", "failed", "expired")
# Wait after the n-th failed delivery; one more attempt than entries, then give up.
BACKOFF = (10, 60, 300, 1800, 7200, 21600, 86400)


def new_record(key, fingerprint, function, *, owner=None, target=None, callback_url=None, now=None):
    now = time.time() if now is None else now
    callback = {"url": callback_url, "status": "waiting", "attempts": 0} if callback_url else None
    return {"id": str(uuid.uuid4()), "key": key, "fingerprint": fingerprint, "function": function,
            "target": target, "owner": owner, "call_id": None, "state": {"status": "submitting"},
            "callback": callback, "created_at": now, "submitted_at": None, "completed_at": None,
            "updated_at": now}


class _Transitions:
    """State changes shared by both ledgers. Subclasses provide _txn, _load and _save."""

    def get(self, job_id):
        with self._txn() as handle:
            record = self._load(handle, job_id)
        if record is None:
            raise HTTPException(404, "Job not found")
        return record

    def attach(self, job_id, call_id, now=None):
        now = time.time() if now is None else now
        with self._txn(write=True) as handle:
            record = self._load(handle, job_id)
            if record is None or record["state"]["status"] not in ("submitting", "submission_unknown"):
                return False
            record.update(call_id=call_id, state={"status": "pending"}, submitted_at=now, updated_at=now)
            self._save(handle, record)
            return True

    def update(self, job_id, state, *, allowed=OPEN, if_updated_at=..., now=None):
        """Compare-and-set. A settled result is never overwritten by a stale poll."""
        now = time.time() if now is None else now
        with self._txn(write=True) as handle:
            record = self._load(handle, job_id)
            if record is None or record["state"]["status"] not in allowed:
                return False
            if if_updated_at is not ... and record.get("updated_at") != if_updated_at:
                return False
            record["state"] = state
            record["updated_at"] = now
            if state["status"] in SETTLED:
                record["completed_at"] = now
                callback = record.get("callback")
                if callback and callback["status"] == "waiting":
                    callback.update(status="pending", next_attempt_at=now)
            self._save(handle, record)
            return True

    def record_delivery(self, job_id, ok, error=None, now=None):
        now = time.time() if now is None else now
        with self._txn(write=True) as handle:
            record = self._load(handle, job_id)
            callback = record and record.get("callback")
            if not callback or callback["status"] != "pending":
                return
            callback["attempts"] += 1
            callback.pop("next_attempt_at", None)
            if ok:
                callback.update(status="delivered", delivered_at=now)
                callback.pop("last_error", None)
            else:
                callback["last_error"] = error
                if callback["attempts"] > len(BACKOFF):
                    callback["status"] = "failed"
                else:
                    callback["next_attempt_at"] = now + BACKOFF[callback["attempts"] - 1]
            self._save(handle, record)


class Store(_Transitions):
    """SQLite ledger. The file must live on persistent, local storage; one gateway host."""

    COLUMNS = {"status": "TEXT", "owner": "TEXT", "target": "TEXT", "created_at": "REAL",
               "submitted_at": "REAL", "completed_at": "REAL", "updated_at": "REAL",
               "callback": "TEXT", "next_delivery_at": "REAL"}

    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                function TEXT NOT NULL, call_id TEXT, state TEXT NOT NULL)''')
            # v0.1 databases have only the columns above; upgrade them in place.
            present = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, kind in self.COLUMNS.items():
                if name not in present:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
            db.execute("UPDATE jobs SET status=json_extract(state,'$.status') WHERE status IS NULL")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_delivery ON jobs(next_delivery_at)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, created_at)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _txn(self, write=False):
        with closing(self.connect()) as db, db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _record(row):
        record = dict(row)
        record["state"] = json.loads(record["state"])
        record["callback"] = json.loads(record["callback"]) if record["callback"] else None
        del record["status"], record["next_delivery_at"]
        return record

    def _load(self, db, job_id):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._record(row) if row else None

    def _save(self, db, record):
        callback = record.get("callback")
        due = callback.get("next_attempt_at") if callback and callback["status"] == "pending" else None
        db.execute('''INSERT INTO jobs (id,key,fingerprint,function,call_id,state,status,owner,target,
                created_at,submitted_at,completed_at,updated_at,callback,next_delivery_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET call_id=excluded.call_id, state=excluded.state,
                status=excluded.status, submitted_at=excluded.submitted_at,
                completed_at=excluded.completed_at, updated_at=excluded.updated_at,
                callback=excluded.callback, next_delivery_at=excluded.next_delivery_at''',
            (record["id"], record["key"], record["fingerprint"], record["function"], record["call_id"],
             json.dumps(record["state"]), record["state"]["status"], record.get("owner"), record.get("target"),
             record.get("created_at"), record.get("submitted_at"), record.get("completed_at"),
             record.get("updated_at"), json.dumps(callback) if callback else None, due))

    def find(self, key):
        with self._txn() as db:
            row = db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        return self._record(row) if row else None

    def reserve(self, key, fingerprint, function, **meta):
        with self._txn(write=True) as db:
            existing = db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(409, "Idempotency key already used for different input")
                return self._record(existing), False
            record = new_record(key, fingerprint, function, **meta)
            self._save(db, record)
            return record, True

    def _select(self, where, args, order, limit):
        with self._txn() as db:
            rows = db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY {order} LIMIT ?", (*args, limit)).fetchall()
        return [self._record(row) for row in rows]

    def open_jobs(self, limit=500):
        return self._select("status IN (?,?,?)", OPEN, "updated_at", limit)

    def due_deliveries(self, now, limit=100):
        return self._select("next_delivery_at <= ?", (now,), "next_delivery_at", limit)

    def list(self, status=None, owner=None, limit=50):
        where, args = ["1=1"], []
        if status:
            where.append("status=?"); args.append(status)
        if owner is not None:
            where.append("owner=?"); args.append(owner)
        return self._select(" AND ".join(where), args, "created_at DESC, rowid DESC", limit)

    def counts(self):
        with self._txn() as db:
            jobs = {row[0]: row[1] for row in db.execute("SELECT status,count(*) FROM jobs GROUP BY status")}
            callbacks = {row[0]: row[1] for row in db.execute(
                "SELECT json_extract(callback,'$.status'),count(*) FROM jobs WHERE callback IS NOT NULL GROUP BY 1")}
        return {"jobs": jobs, "callbacks": callbacks, "scope": "all"}

    def usage(self, owner, since):
        with self._txn() as db:
            active = db.execute("SELECT count(*) FROM jobs WHERE owner IS ? AND status IN (?,?,?)", (owner, *OPEN)).fetchone()[0]
            recent = db.execute("SELECT count(*) FROM jobs WHERE owner IS ? AND created_at >= ?", (owner, since)).fetchone()[0]
        return active, recent

    def purge(self, before):
        """Delete settled jobs older than `before`. Their idempotency keys become reusable."""
        with self._txn(write=True) as db:
            return db.execute("DELETE FROM jobs WHERE status IN (?,?,?) AND completed_at < ? AND next_delivery_at IS NULL",
                              (*SETTLED, before)).rowcount


class DictStore(_Transitions):
    """Ledger on two Modal Dicts, so the gateway needs no disk and can run on Modal.

    `jobs` maps job IDs to records and hashed idempotency keys to job IDs. `unsettled`
    holds a small marker for every job that still needs attention, so the reconciler
    never scans finished work. Reservation is atomic (put-if-absent). Other updates
    are read-modify-write: a racing stale poll can briefly rewrite a state, and the
    next poll corrects it from Modal. Modal expires Dict entries after 7 idle days,
    which is this ledger's retention policy.
    """

    def __init__(self, jobs, unsettled):
        self.jobs, self.unsettled = jobs, unsettled

    @contextmanager
    def _txn(self, write=False):
        yield None

    def _load(self, _, job_id):
        record = self.jobs.get(job_id) if not job_id.startswith(("key:", "quota:")) else None
        return record if isinstance(record, dict) else None

    def _save(self, _, record):
        self.jobs.put(record["id"], record)
        status, callback = record["state"]["status"], record.get("callback")
        pending_delivery = bool(callback) and callback["status"] == "pending"
        if status in OPEN or status == "submission_unknown" or pending_delivery:
            self.unsettled.put(record["id"], {
                "status": status, "owner": record.get("owner"), "created_at": record.get("created_at"),
                "next_attempt_at": callback.get("next_attempt_at") if pending_delivery else None})
        else:
            self.unsettled.pop(record["id"], None)

    @staticmethod
    def _slot(key):
        return "key:" + hashlib.sha256(key.encode()).hexdigest()

    def find(self, key):
        job_id = self.jobs.get(self._slot(key))
        return self._load(None, job_id) if job_id else None

    def reserve(self, key, fingerprint, function, **meta):
        record = new_record(key, fingerprint, function, **meta)
        # Write the record first so whoever wins the key always points at a readable job.
        self._save(None, record)
        if not self.jobs.put(self._slot(key), record["id"], skip_if_exists=True):
            existing = self.find(key)
            if existing is not None:
                self.jobs.pop(record["id"], None)
                self.unsettled.pop(record["id"], None)
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(409, "Idempotency key already used for different input")
                return existing, False
            self.jobs.put(self._slot(key), record["id"])  # The earlier job expired; take over its key.
        counter = self._quota_slot(record["owner"], record["created_at"])
        self.jobs.put(counter, (self.jobs.get(counter) or 0) + 1)  # Approximate under concurrency.
        return record, True

    @staticmethod
    def _quota_slot(owner, when):
        return f"quota:{owner or ''}:{time.strftime('%Y-%m-%d', time.gmtime(when))}"

    def _markers(self):
        return [(job_id, marker) for job_id, marker in self.unsettled.items()]

    def _records(self, ids):
        return [record for record in (self._load(None, job_id) for job_id in ids) if record]

    def open_jobs(self, limit=500):
        return self._records([i for i, m in self._markers() if m["status"] in OPEN][:limit])

    def due_deliveries(self, now, limit=100):
        return self._records([i for i, m in self._markers()
                              if m.get("next_attempt_at") is not None and m["next_attempt_at"] <= now][:limit])

    def list(self, status=None, owner=None, limit=50):
        """Only unsettled jobs are listable here; settled jobs are read by ID."""
        markers = [(i, m) for i, m in self._markers()
                   if (not status or m["status"] == status) and (owner is None or m.get("owner") == owner)]
        markers.sort(key=lambda pair: pair[1].get("created_at") or 0, reverse=True)
        return self._records([i for i, _ in markers[:limit]])

    def counts(self):
        jobs, pending = {}, 0
        for _, marker in self._markers():
            jobs[marker["status"]] = jobs.get(marker["status"], 0) + 1
            pending += marker.get("next_attempt_at") is not None
        return {"jobs": jobs, "callbacks": {"pending": pending}, "scope": "unsettled"}

    def usage(self, owner, since):
        active = sum(m["status"] in OPEN and m.get("owner") == owner for _, m in self._markers())
        return active, self.jobs.get(self._quota_slot(owner, time.time())) or 0

    def purge(self, before):
        return 0

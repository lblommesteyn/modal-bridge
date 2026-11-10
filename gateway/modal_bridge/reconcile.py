"""Background reconciliation: settle jobs without a client polling, and deliver signed callbacks."""
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .store import OPEN

log = logging.getLogger(__name__)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_callback_url(url, allowed_hosts=None):
    """Return an error string, or None when the URL is an acceptable callback target."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "Callback URL is not valid"
    if not host or parts.username or parts.password or parts.fragment:
        return "Callback URL is not valid"
    if parts.scheme != "https" and not (parts.scheme == "http" and host in LOCAL_HOSTS):
        return "Callback URL requires HTTPS (HTTP allowed only on localhost)"
    if allowed_hosts and not any(host == rule or (rule.startswith("*.") and host.endswith(rule[1:]))
                                 for rule in allowed_hosts):
        return "Callback host is not in BRIDGE_CALLBACK_HOSTS"
    return None


def signing_key(secret):
    """Standard Webhooks secrets are `whsec_<base64>`; any other string is used as raw bytes."""
    if secret.startswith("whsec_"):
        return base64.b64decode(secret[6:])
    return secret.encode()


def sign(secret, message_id, timestamp, body):
    mac = hmac.new(signing_key(secret), f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256)
    return "v1," + base64.b64encode(mac.digest()).decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send(url, headers, body, timeout=10):
    """POST once. Returns None on a 2xx response, otherwise a short error."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            return None if 200 <= response.status < 300 else f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except Exception as error:
        return type(error).__name__


class Reconciler:
    def __init__(self, store, backend, view, *, webhook_secret=None, sender=send,
                 stale_after=120, retention_seconds=None):
        self.store, self.backend, self.view = store, backend, view
        self.webhook_secret, self.sender = webhook_secret, sender
        self.stale_after, self.retention_seconds = stale_after, retention_seconds
        self._last_purge = 0.0

    def is_stale(self, record, now=None):
        """A reservation whose spawn was never recorded: the gateway probably died mid-submit."""
        now = time.time() if now is None else now
        seen = record.get("updated_at") or record.get("created_at")
        return record["state"]["status"] == "submitting" and (seen is None or now - seen > self.stale_after)

    def refresh(self, record, timeout=0, now=None):
        """Ask Modal once. Raises on transport failure, which is not evidence of job failure."""
        latest = self.backend.poll(record["call_id"], timeout) if timeout else self.backend.poll(record["call_id"])
        if latest["status"] != "pending":
            self.store.update(record["id"], latest, now=now)

    def deliver(self, record, now=None):
        now = time.time() if now is None else now
        callback = record["callback"]
        body = json.dumps({"type": "job." + record["state"]["status"], "id": record["id"],
                           "created_at": record.get("completed_at"), "job": self.view(record)},
                          separators=(",", ":")).encode()
        timestamp = str(int(now))
        headers = {"Content-Type": "application/json", "User-Agent": "modal-bridge",
                   "webhook-id": record["id"], "webhook-timestamp": timestamp,
                   "webhook-signature": sign(self.webhook_secret, record["id"], timestamp, body)}
        error = self.sender(callback["url"], headers, body)
        if error:
            log.warning("Callback for job %s failed: %s", record["id"], error)
        self.store.record_delivery(record["id"], error is None, error, now)
        return error is None

    def settle(self, job_id, deadline, poll_seconds=20):
        """Block until one job settles (or the deadline), then deliver its callback immediately."""
        while time.time() < deadline:
            record = self.store.get(job_id)
            if record["state"]["status"] not in ("pending", "cancel_requested") or not record["call_id"]:
                break
            try:
                self.refresh(record, timeout=poll_seconds)
            except Exception:
                log.exception("Unable to poll %s", job_id)
                time.sleep(5)
        record = self.store.get(job_id)
        callback = record.get("callback")
        if callback and callback["status"] == "pending" and callback.get("next_attempt_at", 0) <= time.time():
            self.deliver(record)

    def tick(self, now=None):
        fixed, now = now, time.time() if now is None else now
        for record in self.store.open_jobs():
            status = record["state"]["status"]
            if self.is_stale(record, now):
                self.store.update(record["id"], {"status": "submission_unknown",
                    "error": "Gateway stopped before recording the Modal call; resolve before resubmitting"},
                    allowed=("submitting",), if_updated_at=record.get("updated_at"), now=now)
            elif status in OPEN and status != "submitting" and record["call_id"]:
                try:
                    self.refresh(record, now=fixed)
                except Exception:
                    log.warning("Unable to poll %s; will retry", record["id"])
        now = time.time() if fixed is None else fixed  # Jobs settled above are due immediately.
        if self.webhook_secret:
            for record in self.store.due_deliveries(now):
                self.deliver(record, now)
        if self.retention_seconds and now - self._last_purge > 3600:
            self._last_purge = now
            removed = self.store.purge(now - self.retention_seconds)
            if removed:
                log.info("Retention removed %s settled jobs", removed)

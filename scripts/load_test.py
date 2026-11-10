"""Offline load test: submission and polling latency, and back-pressure, without Modal.

    uv run python scripts/load_test.py [--clients 64] [--jobs 500] [--modal-latency-ms 150] [--limit 32]

A fake backend sleeps for --modal-latency-ms per SDK call, which is what bounds a
gateway whose handlers block on the Modal SDK. Uses a temporary SQLite ledger.
"""
import argparse
import statistics
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from modal_bridge.app import create_app

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--clients", type=int, default=64)
parser.add_argument("--jobs", type=int, default=500)
parser.add_argument("--modal-latency-ms", type=float, default=150)
parser.add_argument("--limit", type=int, default=32, help="BRIDGE_MAX_MODAL_CONCURRENCY")
args = parser.parse_args()


class SlowBackend:
    def submit(self, target, payload):
        time.sleep(args.modal_latency_ms / 1000)
        return f"fc-{uuid.uuid4()}"

    def poll(self, call_id, timeout=0):
        time.sleep(args.modal_latency_ms / 1000)
        return {"status": "succeeded", "result": {"ok": True}}

    def cancel(self, call_id):
        pass


def percentile(values, q):
    return sorted(values)[min(len(values) - 1, int(len(values) * q))]


with tempfile.TemporaryDirectory() as directory:
    app = create_app(token="l" * 32, targets={"work": ("demo", "work")}, db_path=f"{directory}/jobs.sqlite",
                     backend=SlowBackend(), max_modal_concurrency=args.limit)
    headers = {"Authorization": "Bearer " + "l" * 32}
    local = threading.local()

    def one(index):
        if not hasattr(local, "client"):
            local.client = TestClient(app)
        started = time.perf_counter()
        response = local.client.post("/v1/jobs", headers={**headers, "Idempotency-Key": f"load-{index}"},
                                     json={"function": "work", "input": {"n": index}})
        submitted = time.perf_counter()
        if response.status_code != 202:
            return response.status_code, submitted - started, None
        poll = local.client.get(f'/v1/jobs/{response.json()["id"]}', headers=headers)
        return poll.status_code, submitted - started, time.perf_counter() - submitted

    began = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.clients) as pool:
        results = list(pool.map(one, range(args.jobs)))
    elapsed = time.perf_counter() - began
    metrics = TestClient(app).get("/v1/metrics", headers=headers).json()

codes = {}
for code, _, _ in results:
    codes[code] = codes.get(code, 0) + 1
submits = [s * 1000 for _, s, _ in results]
polls = [p * 1000 for _, _, p in results if p is not None]
print(f"{args.jobs} jobs, {args.clients} clients, {args.modal_latency_ms:.0f} ms per Modal call, limit {args.limit}")
print(f"  wall time     {elapsed:.1f}s   ({args.jobs / elapsed:.0f} submit+poll pairs/s)")
print(f"  final status  {codes}   (503 = rejected by back-pressure before reserving a key)")
print(f"  submit ms     p50 {statistics.median(submits):.0f}   p95 {percentile(submits, .95):.0f}   max {max(submits):.0f}")
if polls:
    print(f"  poll ms       p50 {statistics.median(polls):.0f}   p95 {percentile(polls, .95):.0f}   max {max(polls):.0f}")
print(f"  gateway view  saturated={metrics['modal']['saturated']}  calls={metrics['modal']['calls']}")

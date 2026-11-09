"""Exercise a running gateway against real Modal CPU functions.

Deploy examples/modal_app.py and configure square, fail, slow aliases first.
Uses normal Modal compute. Does not print credentials.
"""
import json
import os
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv()
base = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
token = os.environ["BRIDGE_TOKEN"]
run = str(uuid.uuid4())


def request(path, method="GET", body=None, key=None, authorized=True):
    headers = {"Content-Type": "application/json"}
    if authorized:
        headers["Authorization"] = f"Bearer {token}"
    if key:
        headers["Idempotency-Key"] = key
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                  headers=headers, method=method)
    try:
        with urlopen(req, timeout=45) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def submit(alias, payload, key):
    status, job = request("/v1/jobs", "POST", {"function": alias, "input": payload}, key)
    assert status == 202 and job["status"] in {"pending", "succeeded"}, (status, job)
    return job


def wait(job):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status, current = request(f'/v1/jobs/{job["id"]}')
        if status == 502:
            time.sleep(1)
            continue
        assert status == 200, (status, current)
        if current["status"] != "pending":
            return current
        time.sleep(1)
    raise TimeoutError(f'Job {job["id"]} did not settle within 120 seconds')


assert request("/health")[0] == 200
assert request("/v1/jobs/unknown", authorized=False)[0] == 401
job = submit("square", {"value": 12}, run + "-square")
assert submit("square", {"value": 12}, run + "-square")["id"] == job["id"]
assert request("/v1/jobs", "POST", {"function": "square", "input": {"value": 13}}, run + "-square")[0] == 409
result = wait(job)
assert result["status"] == "succeeded" and result["result"] == {"value": 144}, result
print("PASS real Modal result: square(12) = 144; duplicate request reused the same job")
failed = wait(submit("fail", {}, run + "-fail"))
assert failed["status"] == "failed", failed
print("PASS real Modal exception is returned as a sanitized failed job")
slow = submit("slow", {}, run + "-slow")
status, canceled = request(f'/v1/jobs/{slow["id"]}/cancel', "POST")
assert status == 200 and canceled["status"] == "cancel_requested", (status, canceled)
print("PASS cancellation accepted by Modal (best effort; not a guarantee about side effects)")
print("PASS authentication, conflict handling, submit, poll, failure and cancellation")

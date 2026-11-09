"""Small real CPU functions for integration testing; uses normal Modal compute."""
import time
import modal

app = modal.App("modal-bridge-example")

@app.function(timeout=60, max_containers=2, cpu=0.125, memory=128)
def square(payload: dict) -> dict:
    value = payload["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be a number")
    return {"value": value * value}

@app.function(timeout=30, max_containers=1, cpu=0.125, memory=128)
def fail(payload: dict) -> dict:
    raise ValueError("Intentional integration-test failure")

@app.function(timeout=30, max_containers=1, cpu=0.125, memory=128)
def slow(payload: dict) -> dict:
    time.sleep(15)
    return {"finished": True}

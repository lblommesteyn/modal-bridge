"""The gateway hosted on Modal itself: no server, disk or database to operate.

Deploy with `modal-bridge deploy`. It creates the Secret this module reads
(BRIDGE_TOKEN, BRIDGE_TARGETS, BRIDGE_WEBHOOK_SECRET) and deploys three functions:

  gateway    the HTTP API, scaling to zero between requests
  watch      waits on one submitted job, then delivers its callback immediately
  reconcile  every minute: settles anything a watcher missed and retries callbacks
"""
import os
import time

import modal

NAME = os.environ.get("MODAL_BRIDGE_APP", "modal-bridge")
app = modal.App(NAME)
image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("fastapi>=0.115,<1")
         .env({"MODAL_BRIDGE_APP": NAME})
         .add_local_python_source("modal_bridge"))
secret = modal.Secret.from_name(NAME)
jobs = modal.Dict.from_name(f"{NAME}-jobs", create_if_missing=True)
unsettled = modal.Dict.from_name(f"{NAME}-unsettled", create_if_missing=True)


def build(on_pending=None):
    from .app import DictStore, from_env
    return from_env(store=DictStore(jobs, unsettled), on_pending=on_pending)


@app.function(image=image, secrets=[secret], scaledown_window=300)
@modal.concurrent(max_inputs=32)
@modal.asgi_app()
def gateway():
    return build(on_pending=lambda job_id: watch.spawn(job_id))


@app.function(image=image, secrets=[secret], timeout=3600, cpu=0.125, memory=256)
@modal.concurrent(max_inputs=100)
def watch(job_id: str):
    # Jobs that outlive this function are settled by `reconcile`.
    build().state.reconciler.settle(job_id, deadline=time.time() + 3500)


@app.function(image=image, secrets=[secret], schedule=modal.Period(minutes=1),
              timeout=300, cpu=0.125, memory=256)
def reconcile():
    build().state.reconciler.tick()

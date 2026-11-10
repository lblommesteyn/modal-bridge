# Architecture

The Workers client uses standard `fetch`, and the public API stays small: submit,
get, cancel, plus a callback when the job settles. A gateway translates those calls
into the Modal Python SDK. It is an application-level adapter, not an implementation
of Modal's entire SDK.

## The simpler alternative

For a single integration, put a small FastAPI app directly on Modal using
`@modal.asgi_app()`. Submit with `Function.spawn()` and poll with
`FunctionCall.from_id(...).get(timeout=0)`. Modal documents this
[pattern](https://modal.com/docs/guide/job-queue). It does not by itself give you
request-key deduplication, completion callbacks or a shared contract across functions.
Use this project when those are worth a dependency.

## Two ledgers, one contract

All state changes live in `store.py` behind one interface, with two implementations.
The API (`app.py`) and the reconciler (`reconcile.py`) do not know which one they use,
and the same test suite runs against both.

| | Modal-hosted (default) | Self-hosted |
| --- | --- | --- |
| Ledger | Two Modal Dicts | SQLite on local disk |
| Runs on | Your Modal workspace, scale to zero | One Docker host |
| Replicas | Several gateway containers | Exactly one |
| Reservation | Atomic put-if-absent | `BEGIN IMMEDIATE` transaction |
| Other updates | Read-modify-write | Transactional compare-and-set |
| Retention | 7 idle days (Modal Dict expiry) | `BRIDGE_RETENTION_DAYS`, or forever |
| Listing and metrics | Unsettled jobs | All jobs |
| Batch runs (`/v1/runs`) | No | Yes |

### Why Modal Dicts

The earlier plan was to host the adapter on Modal and add PostgreSQL. That removes the
VM but adds a database, which is the same operational ask in a different place. A Modal
Volume is not a substitute: it is not a transactional shared disk, so SQLite on a Volume
breaks under concurrent writers.

The only operation that must be atomic is reserving an idempotency key, and
`Dict.put(key, value, skip_if_exists=True)` provides exactly that. The record is written
before the key, so whichever request wins the key always points at a readable job.

Every other update is read-modify-write. The consequences are bounded:

- A stale poll racing a fresh one writes the same Modal-derived outcome.
- A cancel racing completion can briefly rewrite `succeeded` as `cancel_requested`.
  That state is still polled, so the next poll restores the real outcome from Modal.
- A watcher and the reconciler can both deliver the same callback. Callbacks are
  documented as at-least-once and carry a stable `webhook-id`.
- The per-owner daily counter can undercount under heavy concurrency.

A second Dict holds a small marker for each job that still needs attention (unsettled,
uncertain, or with a callback due). The reconciler reads only that, so its cost tracks
open work rather than history.

## Settling jobs without a client

In v0.1 a job only progressed when a client polled it. Now three things can settle a job,
and all go through the same compare-and-set, so a settled result is never overwritten:

1. `GET /v1/jobs/{id}` still refreshes on read.
2. **Modal-hosted:** each submission spawns `watch(job_id)`, which long-polls the call
   and delivers the callback as soon as it settles. One small CPU container watches up
   to 100 jobs.
3. A periodic reconciler (`reconcile` every minute on Modal; the `runner` process every
   second when self-hosted) polls open jobs, delivers due callbacks, quarantines stale
   `submitting` reservations and applies retention.

Callback delivery state (`waiting`, `pending`, `delivered`, `failed`, attempt count, next
attempt time, last error) is stored on the job, so retries survive restarts.

## The crash boundary

Neither ledger can commit atomically with a Modal spawn. A crash can occur after the
spawn and before the call ID is recorded. The gateway therefore never resubmits on its
own. An errored spawn becomes `submission_unknown` at once; a reservation left in
`submitting` for two minutes is moved there by the reconciler. An operator then resolves
it explicitly through `POST /v1/jobs/{id}/resolve`:

- `attach` a call ID found in Modal (verified with Modal before it is recorded),
- `abandon` it as failed, or
- `resubmit`, the only action that can run work twice, which requires the original
  input and checks it against the stored hash.

Do not promise exactly-once execution without a provider-supported deduplication
mechanism or an idempotency contract inside the workload itself.

## Blocking handlers and back-pressure

Handlers are synchronous and block on the Modal SDK, so they run in the server's thread
pool. A semaphore bounds concurrent SDK calls (`BRIDGE_MAX_MODAL_CONCURRENCY`, default
32, below the pool size so health checks still answer). A submission takes its slot
before reserving a key: a saturated gateway answers 503 with `Retry-After` and leaves no
stranded reservation. `/v1/metrics` reports per-operation latency, in-flight calls and
how often the limit was hit. `scripts/load_test.py` reproduces this offline.

## What is still open

1. Results over 1 MiB need an artifact path (upload from the function, return a reference).
2. Inputs are stored as hashes only, so replay needs the caller to resend the input.
3. One token grants access to every job. Per-tenant credentials are not implemented;
   `Bridge-Owner` is a label for quotas and filtering, not an authorization boundary.
4. If Modal ships an edge-compatible SDK, the transport half of this project becomes
   unnecessary. The ledger, callbacks and resolution workflow would still apply.

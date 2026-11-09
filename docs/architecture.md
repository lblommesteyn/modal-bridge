# Why an HTTP gateway?

The Workers client should use standard `fetch`, and the public API should stay small:
submit, get, cancel. A gateway translates those calls into the Modal Python SDK.
It is an application-level adapter, not an implementation of Modal's entire SDK.

## The simpler alternative

For most single-app integrations, put a small FastAPI app directly on Modal using
`@modal.asgi_app()`. Submit with `Function.spawn()` and poll with
`FunctionCall.from_id(...).get(timeout=0)`. The Worker calls its HTTPS URL directly.
Modal documents this [pattern](https://modal.com/docs/guide/job-queue).
This removes a separately operated Python host. It still uses an HTTP adapter,
and does not by itself provide a durable request-key ledger or a result archive.

**Recommendation:** use the direct Modal app for a small integration. Use this
project when the durable job contract is worth an additional service.

## What the current design buys

The gateway reserves an idempotency key before spawning work, assigns a stable job
ID, and saves the Modal call ID. Duplicate HTTP requests reuse that record.
Terminal results are cached on poll, so later reads need not call Modal. All
operations share a function allowlist and a bearer-token policy.

SQLite keeps the first release easy to run and back up. It also makes this a
single-host service with persistent local disk. A Modal Volume is not a drop-in
shared transactional SQLite disk; mounting one does not solve concurrent writes
or atomic coordination with remote spawning.

## Better serverless deployment, as a next step

Keep the same HTTP contract and client. Host the FastAPI adapter on Modal, and
replace the SQLite ledger with a transactional shared store, such as PostgreSQL.
That avoids operating a dedicated gateway host, but adds a database dependency.
Use a unique request-key constraint and compare-and-set state changes. Test with
multiple replicas before calling this a supported deployment mode.

For a Cloudflare-heavy product, another option is moving the ledger into Durable
Objects and leaving a small authenticated dispatch adapter on Modal. It offers
coordination at the edge but adds distributed state transitions and another API
boundary. It is more code than this first release needs.

Neither alternative makes the database write and Modal spawn atomic. A crash can
still occur between them. Do not promise exactly-once execution without a
provider-supported deduplication mechanism or an application-level idempotency
contract inside the workload itself. A retry queue alone does not fix that gap.

## Priorities before broad adoption

1. Validate the submit / poll / cancel contract with real Modal workloads.
2. Add metrics and explicit reconciliation for uncertain submissions.
3. Add opt-in input storage and replay with side-effect boundaries.
4. Introduce a shared-store implementation only when multi-replica deployment is needed.

Swagger/OpenAPI describes the actual implemented routes. The diagrams in the
README describe the current SQLite deployment, not these proposed alternatives.

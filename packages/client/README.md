# @modal-bridge/client

Run Modal jobs, including GPU jobs, from Cloudflare Workers and other edge runtimes.
Dependency-free: `fetch` and Web Crypto only, no Node compatibility flag, no gRPC.

This is the client for a [Modal Bridge](https://github.com/lblommesteyn/modal-bridge) gateway,
which you deploy into your own Modal workspace with one command. It does not call
Modal's control plane directly. Unofficial; not affiliated with Modal or Cloudflare.

```ts
import { ModalBridge, verifyWebhook } from "@modal-bridge/client";

type Functions = {
  generate: { input: { prompt: string }; output: { image_base64: string } };
};
const bridge = new ModalBridge<Functions>({ url: env.BRIDGE_URL, token: env.BRIDGE_TOKEN });

// Start a job and return to your user immediately.
const job = await bridge.submit("generate", { prompt }, {
  idempotencyKey: requestId,
  onComplete: { url: "https://my-app.dev/webhooks/modal-bridge" },
});

// In your webhook route: verified, typed, and safe to run in a Worker.
const event = await verifyWebhook<Functions["generate"]["output"]>(request, env.BRIDGE_WEBHOOK_SECRET);
if (event.type === "job.succeeded") save(event.job.id, event.job.result);
```

| Method | Purpose |
| --- | --- |
| `submit(name, input, { idempotencyKey, onComplete?, owner? })` | Start a job. Reuse the key when retrying the same input. |
| `get(id)` / `wait(id, { timeoutMs, intervalMs, signal })` | Read once, or poll until the job settles. |
| `cancel(id)` | Request cancellation. |
| `list({ status, owner, limit })` | Find jobs, for example `submission_unknown` ones. |
| `resolve(id, { action })` | Settle an uncertain submission: `attach`, `resubmit` or `abandon`. |
| `verifyWebhook(request, secret)` | Check a completion callback and return its event. |

Things worth knowing:

- Callbacks are at-least-once. Make handlers idempotent on `event.id`.
- Stopping `wait` does not stop the remote job. After `cancel`, `wait` keeps polling until
  the gateway reports a final state: a cancelled job normally ends as `failed`, but one
  that finished first still resolves with its result.
- Types are compile-time only. Validate important results at runtime.
- Keep tokens in server-side secrets, never in browser code.

See the [repository README](https://github.com/lblommesteyn/modal-bridge) for deployment,
job states and reliability boundaries. MIT licensed.

# @modal-bridge/client

Dependency-free, fetch-only TypeScript client for the unofficial Modal Bridge
HTTP gateway. Works in Cloudflare Workers without Node compatibility or gRPC.
This package requires a self-hosted Modal Bridge gateway; it does not call Modal's
control plane directly. Not affiliated with Modal or Cloudflare.

```ts
import { ModalBridge } from '@modal-bridge/client';
type Functions = {
  square: { input: { value: number }; output: { value: number } };
};
const bridge = new ModalBridge<Functions>({url: gatewayUrl, token: gatewayToken});
const job = await bridge.submit('square', {value: 12}, {
  idempotencyKey: 'unique-request-id',
});
const result = await bridge.wait<{value: number}>(job.id);
```

Reuse the idempotency key when retrying the same submission. `get(id)` polls status;
`cancel(id)` requests cancellation. `wait` accepts `timeoutMs`, `intervalMs`, and an
AbortSignal; stopping local waiting does not stop the remote job. Keep tokens in
server-side secrets, never browser code. See the [repository README](https://github.com/lblommesteyn/modal-bridge) for gateway
setup, API states, deployment instructions and reliability boundaries.

MIT licensed.

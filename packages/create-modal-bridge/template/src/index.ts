import { BridgeError, ModalBridge, WebhookError, verifyWebhook } from "@modal-bridge/client";

interface Env {
  BRIDGE_URL: string;
  BRIDGE_FUNCTION: string;
  BRIDGE_TOKEN: string;
  BRIDGE_WEBHOOK_SECRET: string;
}
// Describe your Modal functions here to type-check inputs and results.
type Functions = Record<string, { input: unknown; output: unknown }>;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const bridge = new ModalBridge<Functions>({ url: env.BRIDGE_URL, token: env.BRIDGE_TOKEN });
    try {
      // POST /jobs with a JSON body starts a Modal job and returns at once.
      // This starter has no authentication: add your own before deploying it publicly.
      if (request.method === "POST" && url.pathname === "/jobs") {
        const key = request.headers.get("Idempotency-Key");
        if (!key) return Response.json({ error: "Send an Idempotency-Key header" }, { status: 400 });
        const job = await bridge.submit(env.BRIDGE_FUNCTION, await request.json(), {
          idempotencyKey: key,
          onComplete: { url: `${url.origin}/webhooks/modal-bridge` },
        });
        return Response.json(job, { status: 202 });
      }
      // The gateway calls this once the job settles. Deliveries are at-least-once.
      if (request.method === "POST" && url.pathname === "/webhooks/modal-bridge") {
        const event = await verifyWebhook(request, env.BRIDGE_WEBHOOK_SECRET);
        console.log(event.type, event.job.id, event.job.result ?? event.job.error);
        // Store the result, notify your user, start the next step...
        return new Response(null, { status: 204 });
      }
      const job = url.pathname.match(/^\/jobs\/([0-9a-f-]{36})$/);
      if (request.method === "GET" && job) return Response.json(await bridge.get(job[1]));
      return Response.json({ error: "Not found" }, { status: 404 });
    } catch (error) {
      if (error instanceof WebhookError) return Response.json({ error: error.message }, { status: 401 });
      if (error instanceof BridgeError) return Response.json({ error: error.message }, { status: error.status });
      throw error;
    }
  },
};

import { ModalBridge, BridgeError } from "@modal-bridge/client";
interface Env { BRIDGE_URL: string; BRIDGE_TOKEN: string; EXAMPLE_TOKEN: string }
type Functions = { square: { input: { value: number }; output: { value: number } } };

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Demo is single-user. Add tenant ownership checks before making this a SaaS API.
    if (!env.EXAMPLE_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.EXAMPLE_TOKEN}`) {
      return Response.json({ error: "Unauthorized" }, { status: 401 });
    }
    const client = new ModalBridge<Functions>({ url: env.BRIDGE_URL, token: env.BRIDGE_TOKEN });
    const path = new URL(request.url).pathname;
    try {
      if (request.method === "POST" && path === "/jobs") {
        const key = request.headers.get("Idempotency-Key");
        if (!key) return Response.json({ error: "Idempotency-Key is required" }, { status: 400 });
        const input: unknown = await request.json().catch(() => null);
        if (typeof input !== "object" || input === null || !("value" in input) || typeof input.value !== "number" || !Number.isFinite(input.value)) {
          return Response.json({ error: "Expected {value: number}" }, { status: 400 });
        }
        const job = await client.submit("square", { value: input.value }, { idempotencyKey: key });
        return Response.json(job, { status: 202, headers: { "Cache-Control": "no-store" } });
      }
      const match = path.match(/^\/jobs\/([a-zA-Z0-9-]+)(\/cancel)?$/);
      if (match && request.method === "GET" && !match[2]) {
        return Response.json(await client.get(match[1]), { headers: { "Cache-Control": "no-store" } });
      }
      if (match?.[2] && request.method === "POST") return Response.json(await client.cancel(match[1]));
      return Response.json({ error: "Not found" }, { status: 404 });
    } catch (error) {
      console.error("Gateway request failed:", error instanceof Error ? error.message : "Unknown error");
      return Response.json({ error: error instanceof BridgeError ? error.message : "Gateway request failed" },
        { status: error instanceof BridgeError ? error.status : 502 });
    }
  }
};

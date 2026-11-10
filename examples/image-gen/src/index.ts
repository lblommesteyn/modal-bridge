import { BridgeError, ModalBridge, WebhookError, verifyWebhook } from "@modal-bridge/client";
import page from "./page";

interface Env {
  BRIDGE_URL: string;
  BRIDGE_TOKEN: string;
  BRIDGE_WEBHOOK_SECRET: string;
  IMAGES: R2Bucket;
  /** Set when the Worker is reached through a different origin than the gateway should call back. */
  PUBLIC_URL?: string;
}
interface Generated {
  image_base64: string; content_type: string; width: number; height: number;
  prompt: string; seed: number; steps: number; gpu_seconds: number; device: string; model: string;
}
type Functions = { generate: { input: { prompt: string; seed?: number }; output: Generated } };

const json = (body: unknown, status = 200) =>
  Response.json(body, { status, headers: { "Cache-Control": "no-store" } });
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Quotas are per visitor: the gateway counts jobs by this opaque ID, never the raw address. */
async function visitor(request: Request): Promise<string> {
  const address = request.headers.get("CF-Connecting-IP") ?? "local";
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(address));
  return [...new Uint8Array(hash).slice(0, 8)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

async function generate(request: Request, env: Env): Promise<Response> {
  const body = await request.json().catch(() => null) as { prompt?: unknown; request_id?: unknown } | null;
  const prompt = typeof body?.prompt === "string" ? body.prompt.trim() : "";
  if (!prompt || prompt.length > 300) return json({ error: "Describe the image in 1-300 characters" }, 400);
  if (typeof body?.request_id !== "string" || !UUID.test(body.request_id)) return json({ error: "Bad request_id" }, 400);

  const bridge = new ModalBridge<Functions>({ url: env.BRIDGE_URL, token: env.BRIDGE_TOKEN });
  const origin = env.PUBLIC_URL ?? new URL(request.url).origin;
  // The browser's request_id is the idempotency key: a retried click or a flaky
  // connection returns the same job instead of paying for a second GPU run.
  const job = await bridge.submit("generate", { prompt }, {
    idempotencyKey: `image:${body.request_id}`,
    owner: await visitor(request),
    onComplete: { url: `${origin}/webhooks/modal-bridge` },
  });
  return json({ id: job.id, status: job.status }, 202);
}

/** The gateway calls this once the GPU job settles. Nothing polls Modal. */
async function completed(request: Request, env: Env): Promise<Response> {
  const event = await verifyWebhook<Generated>(request, env.BRIDGE_WEBHOOK_SECRET);
  const { job } = event;
  const record = `jobs/${job.id}.json`;
  if (event.type !== "job.succeeded" || !job.result) {
    await env.IMAGES.put(record, JSON.stringify({ status: "failed", error: job.error ?? "Generation failed" }));
    return new Response(null, { status: 204 });
  }
  const { image_base64, content_type, ...details } = job.result;
  const finished = Math.round((job.completed_at ?? Date.now() / 1000) * 1000);
  const total_seconds = job.created_at && job.completed_at ? +(job.completed_at - job.created_at).toFixed(2) : null;
  // Keys sort newest-first, and derive only from the event, so a redelivery overwrites instead of duplicating.
  const key = `images/${String(9_999_999_999_999 - finished).padStart(13, "0")}-${job.id}.jpg`;
  await env.IMAGES.put(key, Uint8Array.from(atob(image_base64), char => char.charCodeAt(0)), {
    httpMetadata: { contentType: content_type, cacheControl: "public, max-age=31536000, immutable" },
    customMetadata: { prompt: details.prompt, gpu_seconds: String(details.gpu_seconds),
      device: details.device, total_seconds: String(total_seconds ?? "") },
  });
  await env.IMAGES.put(record, JSON.stringify({ status: "succeeded", image: `/${key}`, total_seconds, ...details }));
  return new Response(null, { status: 204 });
}

async function gallery(env: Env): Promise<Response> {
  const listing = await env.IMAGES.list({ prefix: "images/", limit: 24, include: ["customMetadata"] });
  return json({ images: listing.objects.map(object => ({
    image: `/${object.key}`, prompt: object.customMetadata?.prompt ?? "",
  })) });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const { pathname } = new URL(request.url);
    try {
      if (request.method === "GET" && pathname === "/") {
        return new Response(page, { headers: { "Content-Type": "text/html; charset=utf-8" } });
      }
      if (request.method === "POST" && pathname === "/api/generate") return await generate(request, env);
      if (request.method === "POST" && pathname === "/webhooks/modal-bridge") return await completed(request, env);
      if (request.method === "GET" && pathname === "/api/gallery") return await gallery(env);

      const status = pathname.match(/^\/api\/jobs\/([0-9a-f-]{36})$/);
      if (request.method === "GET" && status) {
        // Written by the webhook. Until it arrives, the job is still running.
        const record = await env.IMAGES.get(`jobs/${status[1]}.json`);
        return record ? new Response(record.body, { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } })
                      : json({ status: "pending" });
      }
      if (request.method === "GET" && /^\/images\/[0-9]{13}-[0-9a-f-]{36}\.jpg$/.test(pathname)) {
        const object = await env.IMAGES.get(pathname.slice(1));
        if (!object) return json({ error: "Not found" }, 404);
        const headers = new Headers();
        object.writeHttpMetadata(headers);
        return new Response(object.body, { headers });
      }
      return json({ error: "Not found" }, 404);
    } catch (error) {
      if (error instanceof WebhookError) return json({ error: error.message }, 401);
      if (error instanceof BridgeError) {
        const busy = error.status === 429 || error.status === 503;
        return json({ error: busy ? "You have reached the demo limit. Try again shortly." : "The job gateway rejected the request" },
          busy ? 429 : 502);
      }
      console.error("Request failed:", error instanceof Error ? error.message : "Unknown error");
      return json({ error: "Something went wrong" }, 500);
    }
  },
};

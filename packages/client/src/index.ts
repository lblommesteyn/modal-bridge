/** JSON is the only wire format; no Node or gRPC dependencies. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Status = "submitting" | "submission_unknown" | "pending" | "succeeded" | "failed" | "expired" | "cancel_requested";
export interface CallbackState {
  url: string;
  /** waiting: job not settled yet. pending: delivery due or being retried. */
  status: "waiting" | "pending" | "delivered" | "failed";
  attempts: number;
  next_attempt_at?: number;
  delivered_at?: number;
  last_error?: string;
}
export interface Job<T = Json> {
  id: string;
  function: string;
  /** `cancel_requested` is not final: the job can still succeed, fail or expire. */
  status: Status;
  result?: T;
  error?: string;
  /** Modal app/function the alias resolved to at submission. */
  target?: string;
  owner?: string;
  /** Modal FunctionCall ID. */
  call_id?: string;
  /** Unix seconds. */
  created_at?: number;
  submitted_at?: number;
  completed_at?: number;
  updated_at?: number;
  callback?: CallbackState;
}
/** The signed body the gateway POSTs to `onComplete.url` once a job settles. */
export interface WebhookEvent<T = Json> {
  type: "job.succeeded" | "job.failed" | "job.expired";
  /** Equals the job ID and the `webhook-id` header; use it to ignore redeliveries. */
  id: string;
  created_at: number;
  job: Job<T>;
}
export type Resolution =
  | { action: "attach"; call_id: string }
  | { action: "resubmit"; input: unknown }
  | { action: "abandon" };
export interface RunSpec<I = Json> {
  name: string; function: string; revision: string;
  namespace?: string; max_in_flight?: number; reuse_successes?: boolean;
  items: { key: string; input: I }[];
}
export interface Run {
  id: string; name: string; function: string; revision: string; namespace: string;
  mode: "active" | "paused";
  status: "running" | "paused" | "needs_attention" | "completed";
  max_in_flight: number; counts: Record<string, number>; total: number; created_at: number;
}
export interface RunPreview {
  total: number; reusable: number; to_compute: number;
  items: { key: string; action: "reuse" | "compute"; source: string | null }[];
}
export interface Definition { input: unknown; output: unknown }
export class BridgeError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = "BridgeError"; }
}
export class JobError extends Error {
  constructor(public readonly job: Job<unknown>) { super(job.error ?? `Job ${job.status}`); this.name = "JobError"; }
}
export class WebhookError extends Error {
  constructor(message: string) { super(message); this.name = "WebhookError"; }
}
const decode = (text: string) => Uint8Array.from(atob(text), c => c.charCodeAt(0));
/**
 * Verify a completion callback and return its event. Throws WebhookError otherwise.
 * Uses Web Crypto only, so it runs in Workers. Deliveries are at-least-once:
 * make your handler idempotent on `event.id`.
 */
export async function verifyWebhook<T = Json>(request: Request, secret: string,
    options: { toleranceSeconds?: number } = {}): Promise<WebhookEvent<T>> {
  const id = request.headers.get("webhook-id");
  const timestamp = request.headers.get("webhook-timestamp");
  const signatures = request.headers.get("webhook-signature");
  if (!id || !timestamp || !signatures) throw new WebhookError("Missing webhook signature headers");
  const age = Math.abs(Date.now() / 1000 - Number(timestamp));
  if (!(age <= (options.toleranceSeconds ?? 300))) throw new WebhookError("Webhook timestamp is outside the tolerance");
  const body = await request.text();
  const raw = secret.startsWith("whsec_") ? decode(secret.slice(6)) : new TextEncoder().encode(secret);
  const key = await crypto.subtle.importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  const signed = new TextEncoder().encode(`${id}.${timestamp}.${body}`);
  for (const candidate of signatures.split(" ")) {
    const [version, value] = candidate.split(",");
    if (version !== "v1" || !value) continue;
    let signature: ReturnType<typeof decode>;
    try { signature = decode(value); } catch { continue; }
    if (await crypto.subtle.verify("HMAC", key, signature, signed)) return JSON.parse(body) as WebhookEvent<T>;
  }
  throw new WebhookError("Webhook signature does not match");
}
export class ModalBridge<Functions extends { [K in keyof Functions]: Definition }> {
  private readonly base: string;
  private readonly transport: typeof fetch;
  private readonly token: string;
  constructor(options: { url: string; token: string; fetch?: typeof fetch }) {
    const url = new URL(options.url);
    if (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))) {
      throw new Error("Gateway requires HTTPS (HTTP allowed only on localhost)");
    }
    if (url.username || url.password || url.search || url.hash) throw new Error("Invalid gateway URL");
    this.base = url.href.replace(/\/$/, "");
    this.token = options.token;
    this.transport = options.fetch ?? globalThis.fetch.bind(globalThis);
  }
  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await this.transport(this.base + path, { ...init, headers, redirect: "manual" });
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { detail?: unknown } | null;
      throw new BridgeError(typeof body?.detail === "string" ? body.detail : `Gateway HTTP ${response.status}`, response.status);
    }
    return await response.json() as T;
  }
  submit<K extends keyof Functions & string>(name: K, input: Functions[K]["input"],
      options: {
        idempotencyKey: string;
        /** The gateway POSTs one signed event here when the job settles; check it with verifyWebhook. */
        onComplete?: { url: string };
        /** End-user or tenant ID for quotas and filtering. Set it server-side; it is not authentication. */
        owner?: string;
        signal?: AbortSignal;
      }): Promise<Job<Functions[K]["output"]>> {
    if (!options.idempotencyKey) throw new Error("An idempotencyKey is required; reuse it when retrying");
    const headers: Record<string, string> = { "Idempotency-Key": options.idempotencyKey };
    if (options.owner) headers["Bridge-Owner"] = options.owner;
    return this.request("/v1/jobs", { method: "POST", headers, signal: options.signal,
      body: JSON.stringify({ function: name, input, ...(options.onComplete && { on_complete: options.onComplete }) }) });
  }
  /** Newest first, without results. A Modal-hosted gateway lists unsettled jobs only. */
  async list(options: { status?: Status; owner?: string; limit?: number; signal?: AbortSignal } = {}): Promise<Job[]> {
    const query = new URLSearchParams();
    if (options.status) query.set("status", options.status);
    if (options.owner) query.set("owner", options.owner);
    if (options.limit) query.set("limit", String(options.limit));
    const suffix = query.size ? `?${query}` : "";
    return (await this.request<{ jobs: Job[] }>(`/v1/jobs${suffix}`, { signal: options.signal })).jobs;
  }
  /**
   * Settle a `submission_unknown` job after looking for its call in Modal.
   * Only `resubmit` can run the work twice, and it needs the original input.
   */
  resolve(id: string, resolution: Resolution, options: { signal?: AbortSignal } = {}): Promise<Job> {
    return this.request(`/v1/jobs/${encodeURIComponent(id)}/resolve`,
      { method: "POST", body: JSON.stringify(resolution), ...options });
  }
  get<T = Json>(id: string, options: { signal?: AbortSignal } = {}): Promise<Job<T>> {
    return this.request(`/v1/jobs/${encodeURIComponent(id)}`, options);
  }
  cancel(id: string, options: { signal?: AbortSignal } = {}): Promise<Job> {
    return this.request(`/v1/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", ...options });
  }
  previewRun<K extends keyof Functions & string>(spec: Omit<RunSpec<Functions[K]["input"]>, "function"> & { function: K }): Promise<RunPreview> {
    return this.request("/v1/runs/preview", {method:"POST", body:JSON.stringify(spec)});
  }
  createRun<K extends keyof Functions & string>(spec: Omit<RunSpec<Functions[K]["input"]>, "function"> & { function: K }, options: {idempotencyKey: string}): Promise<Run> {
    if (!options.idempotencyKey) throw new Error("An idempotencyKey is required");
    return this.request("/v1/runs", {method:"POST", headers:{"Idempotency-Key":options.idempotencyKey}, body:JSON.stringify(spec)});
  }
  getRun(id: string): Promise<Run> { return this.request(`/v1/runs/${encodeURIComponent(id)}`); }
  pauseRun(id: string): Promise<Run> { return this.request(`/v1/runs/${encodeURIComponent(id)}/pause`, {method:"POST"}); }
  resumeRun(id: string): Promise<Run> { return this.request(`/v1/runs/${encodeURIComponent(id)}/resume`, {method:"POST"}); }
  retryFailed(id: string, keys?: string[]): Promise<Run> {
    return this.request(`/v1/runs/${encodeURIComponent(id)}/retry-failed`, {method:"POST", body:JSON.stringify({keys:keys ?? null})});
  }
  async wait<T = Json>(id: string, options: { timeoutMs?: number; intervalMs?: number; signal?: AbortSignal } = {}): Promise<T> {
    const timeoutMs = options.timeoutMs ?? 60_000;
    const intervalMs = options.intervalMs ?? 1_000;
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || !Number.isFinite(intervalMs) || intervalMs <= 0) {
      throw new Error("timeoutMs and intervalMs must be positive finite numbers");
    }
    const controller = new AbortController();
    const abort = () => controller.abort(options.signal?.reason);
    const timer = setTimeout(() => controller.abort(new Error("Timed out waiting for job; remote job continues")), timeoutMs);
    options.signal?.addEventListener("abort", abort, { once: true });
    if (options.signal?.aborted) abort();
    try {
      while (true) {
        controller.signal.throwIfAborted();
        const job = await this.get<T>(id, { signal: controller.signal });
        if (job.status === "succeeded") return job.result as T;
        // cancel_requested is not terminal: the gateway keeps polling Modal, and the job can still succeed.
        if (["failed", "expired", "submission_unknown"].includes(job.status)) throw new JobError(job);
        await new Promise<void>((resolve, reject) => {
          const onAbort = () => { clearTimeout(pause); reject(controller.signal.reason); };
          const pause = setTimeout(() => { controller.signal.removeEventListener("abort", onAbort); resolve(); }, intervalMs);
          controller.signal.addEventListener("abort", onAbort, { once: true });
          if (controller.signal.aborted) onAbort();
        });
      }
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", abort);
    }
  }
}

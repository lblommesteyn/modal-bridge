/** JSON is the only wire format; no Node or gRPC dependencies. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Status = "submitting" | "submission_unknown" | "pending" | "succeeded" | "failed" | "expired" | "cancel_requested";
export interface Job<T = Json> {
  id: string;
  function: string;
  status: Status;
  result?: T;
  error?: string;
}
export interface Definition { input: unknown; output: unknown }
export class BridgeError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = "BridgeError"; }
}
export class JobError extends Error {
  constructor(public readonly job: Job<unknown>) { super(job.error ?? `Job ${job.status}`); this.name = "JobError"; }
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
    this.transport = options.fetch ?? fetch;
  }
  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await this.transport(this.base + path, { ...init, headers, redirect: "error" });
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { detail?: unknown } | null;
      throw new BridgeError(typeof body?.detail === "string" ? body.detail : `Gateway HTTP ${response.status}`, response.status);
    }
    return await response.json() as T;
  }
  submit<K extends keyof Functions & string>(name: K, input: Functions[K]["input"],
      options: { idempotencyKey: string; signal?: AbortSignal }): Promise<Job<Functions[K]["output"]>> {
    if (!options.idempotencyKey) throw new Error("An idempotencyKey is required; reuse it when retrying");
    return this.request("/v1/jobs", { method: "POST", headers: { "Idempotency-Key": options.idempotencyKey },
      body: JSON.stringify({ function: name, input }), signal: options.signal });
  }
  get<T = Json>(id: string, options: { signal?: AbortSignal } = {}): Promise<Job<T>> {
    return this.request(`/v1/jobs/${encodeURIComponent(id)}`, options);
  }
  cancel(id: string, options: { signal?: AbortSignal } = {}): Promise<Job> {
    return this.request(`/v1/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", ...options });
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
        if (["failed", "expired", "submission_unknown", "cancel_requested"].includes(job.status)) throw new JobError(job);
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

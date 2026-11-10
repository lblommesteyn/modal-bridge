export default /* html */ `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edge to GPU</title>
<style>
  :root { --bg:#0c0d10; --panel:#15171c; --line:#262a33; --text:#eceef2; --muted:#8b93a3; --accent:#7df0b4; --warn:#ffb86b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; -webkit-font-smoothing:antialiased; }
  main { max-width:1040px; margin:0 auto; padding:56px 20px 80px; }
  .eyebrow { font:12px/1 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.08em; text-transform:uppercase; color:var(--accent); }
  h1 { font-size:clamp(30px,5vw,46px); line-height:1.08; letter-spacing:-.025em; margin:14px 0 10px; font-weight:650; }
  .lede { color:var(--muted); max-width:56ch; margin:0 0 28px; }
  form { display:flex; gap:10px; background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:8px; }
  form:focus-within { border-color:#3a4150; }
  input { flex:1; min-width:0; background:none; border:0; outline:0; color:var(--text); font:inherit; font-size:16px; padding:10px 12px; }
  input::placeholder { color:#5d6474; }
  button { border:0; border-radius:9px; background:var(--accent); color:#06140d; font:inherit; font-weight:650; padding:10px 18px; cursor:pointer; white-space:nowrap; }
  button:disabled { opacity:.55; cursor:default; }
  .path { display:flex; flex-wrap:wrap; gap:6px 4px; align-items:center; margin:16px 2px 36px; font:12px/1 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); }
  .path span { padding:6px 9px; border:1px solid var(--line); border-radius:999px; transition:color .25s,border-color .25s; }
  .path i { font-style:normal; opacity:.5; }
  .path span.on { color:var(--accent); border-color:rgba(125,240,180,.45); }
  .note { min-height:21px; margin:-22px 2px 22px; color:var(--warn); font-size:13px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:14px; }
  figure { margin:0; position:relative; aspect-ratio:1; border-radius:14px; overflow:hidden; background:var(--panel); border:1px solid var(--line); }
  figure img { width:100%; height:100%; object-fit:cover; display:block; opacity:0; transition:opacity .5s; }
  figure img.ready { opacity:1; }
  figcaption { position:absolute; inset:auto 0 0 0; padding:26px 12px 10px; font-size:13px; line-height:1.35; background:linear-gradient(transparent,rgba(5,6,8,.88)); }
  figcaption small { display:block; margin-top:4px; color:var(--accent); font:11px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; }
  figure.pending { display:grid; place-items:center; }
  figure.pending::before { content:""; position:absolute; inset:0; background:linear-gradient(110deg,transparent 30%,rgba(255,255,255,.055) 50%,transparent 70%); background-size:200% 100%; animation:sweep 1.4s linear infinite; }
  figure.pending b { font:500 26px/1 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); font-variant-numeric:tabular-nums; }
  figure.failed { display:grid; place-items:center; padding:18px; text-align:center; color:var(--warn); font-size:13px; }
  .empty { color:var(--muted); grid-column:1/-1; padding:40px 0; text-align:center; border:1px dashed var(--line); border-radius:14px; }
  footer { margin-top:44px; color:var(--muted); font-size:13px; }
  footer a { color:var(--text); }
  @keyframes sweep { to { background-position:-200% 0; } }
  @media (prefers-reduced-motion:reduce) { figure.pending::before { animation:none; } }
</style>
</head>
<body>
<main>
  <div class="eyebrow">Cloudflare Workers + Modal GPUs</div>
  <h1>Type a prompt at the edge.<br>A GPU paints it.</h1>
  <p class="lede">This page is one Worker. It submits a job, returns straight away, and a signed webhook drops the finished image into R2. No server to run, and nothing polls the GPU.</p>
  <form id="form">
    <input id="prompt" maxlength="300" autocomplete="off" placeholder="a lighthouse on a cliff at dawn, oil painting" required>
    <button id="go">Generate</button>
  </form>
  <div class="path" id="path">
    <span data-step="1">Worker</span><i>→</i><span data-step="2">Bridge job</span><i>→</i><span data-step="3">Modal GPU</span><i>→</i><span data-step="4">signed webhook</span><i>→</i><span data-step="5">R2</span>
  </div>
  <div class="note" id="note" role="status"></div>
  <div class="grid" id="grid"><div class="empty" id="empty">Your images will appear here.</div></div>
  <footer>Built with <a href="https://github.com/lblommesteyn/modal-bridge">modal-bridge</a>. The first image after a quiet spell waits for a GPU to start.</footer>
</main>
<script>
const grid = document.getElementById("grid"), note = document.getElementById("note");
const steps = [...document.querySelectorAll("#path span")];
const light = n => steps.forEach(s => s.classList.toggle("on", Number(s.dataset.step) <= n));
const caption = (prompt, item) => {
  const el = document.createElement("figcaption");
  el.textContent = prompt;
  if (item.gpu_seconds) {
    const small = document.createElement("small");
    small.textContent = (item.device === "cpu" ? "CPU " : "GPU ") + item.gpu_seconds.toFixed(2) + "s" + (item.total_seconds ? " · end to end " + item.total_seconds.toFixed(1) + "s" : "");
    el.append(small);
  }
  return el;
};
const picture = item => {
  const img = new Image();
  img.alt = item.prompt; img.loading = "lazy";
  img.onload = () => img.classList.add("ready");
  img.src = item.image;
  return img;
};
// Gallery cards show the prompt only; timings appear on the image you just generated.
const card = item => { const f = document.createElement("figure"); f.append(picture(item), caption(item.prompt, {})); return f; };

fetch("/api/gallery").then(r => r.json()).then(({ images }) => {
  if (!images.length) return;
  document.getElementById("empty")?.remove();
  grid.append(...images.map(card));
}).catch(() => {});

document.getElementById("form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("prompt"), button = document.getElementById("go");
  const prompt = input.value.trim();
  if (!prompt) return;
  button.disabled = true; note.textContent = ""; light(1);
  document.getElementById("empty")?.remove();
  const figure = document.createElement("figure"), clock = document.createElement("b");
  figure.className = "pending"; figure.append(clock); grid.prepend(figure);
  const started = performance.now();
  const tick = setInterval(() => { clock.textContent = ((performance.now() - started) / 1000).toFixed(1) + "s"; }, 100);
  const fail = message => { figure.className = "failed"; figure.textContent = message; light(0); };
  try {
    const request_id = crypto.randomUUID();
    let response;
    for (let attempt = 0; ; attempt++) {
      // Safe to retry: the same request_id always maps to the same job.
      try { response = await fetch("/api/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt, request_id }) }); break; }
      catch (error) { if (attempt === 2) throw error; }
    }
    const job = await response.json();
    if (!response.ok) return fail(job.error || "Could not start the job");
    light(3);
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 600));
      const state = await fetch("/api/jobs/" + job.id).then(r => r.json()).catch(() => ({ status: "pending" }));
      if (state.status === "pending") continue;
      if (state.status !== "succeeded") return fail(state.error || "Generation failed");
      light(5);
      figure.className = ""; figure.replaceChildren(picture(state), caption(state.prompt, state));
      input.value = "";
      break;
    }
  } catch { fail("Network error. Try again."); }
  finally { clearInterval(tick); button.disabled = false; }
});
</script>
</body>
</html>`;

"use strict";
// NEXUS Cloud UI - no framework, no external requests (CSP: self only).

const PRESETS = {
  probe: { help: "فحص سريع: كروت الـ GPU، سرعة الحساب، NVENC، والإنترنت على Kaggle. ابدأ بيه للتأكد إن كل حاجة شغالة.", params: {}, gpu: true },
  torch_bench: { help: "قياس سرعة PyTorch (fp32/fp16) على الـ GPU.", params: { sizes: [1024, 2048, 4096], iters: 10 }, gpu: true },
  ffmpeg_transcode: { help: "تحويل فيديو لـ MP4 H.264 (NVENC لو متاح). input_url لازم يكون رابط https مباشر للملف.",
    params: { input_url: "https://images-assets.nasa.gov/video/iss061m2627771232_Live_Interviews_Jeanette_Epps_191004/iss061m2627771232_Live_Interviews_Jeanette_Epps_191004~medium.mp4", scale: "-2:720", max_seconds: 60 }, gpu: true },
  chunked_transcode: { help: "تحويل على أجزاء مع checkpoint بعد كل جزء — لو وقعت، إعادة المحاولة بتكمل من مكانها.",
    params: { input_url: "https://example.com/video.mp4", chunks: 4, scale: "-2:720" }, gpu: true },
  batch_llm: { help: "موديل لغة من Hugging Face على قائمة أسئلة (حد أقصى 500). بيطلع JSONL بالإجابات.",
    params: { model: "Qwen/Qwen2.5-1.5B-Instruct", max_new_tokens: 256, items: [{ id: "1", prompt: "اكتب عنوان جذاب لفيديو عن الفضاء" }] }, gpu: true },
  a2_wan_video: { help: "توليد فيديو بـ Wan (نص ← فيديو). محتاج GPU وبياخد وقت.",
    params: { model: "wan21-t2v-1.3b", runs: [{ mode: "t2v", prompt: "a calm ocean at sunset, cinematic", width: 832, height: 480, frames: 33, fps: 16, steps: 30, guidance: 5.0, seed: 1 }] }, gpu: true },
  a2_render_steps: { help: "خطوات رندر من محرك المونتاج (للاستخدام المتقدم).", params: { steps: [] }, gpu: true },
};

const STATUS = { submitting: "بيتبعت", queued: "في الطابور", running: "شغالة", succeeded: "خلصت ✓", failed: "فشلت", cancelled: "اتلغت" };
const $ = (s, r = document) => r.querySelector(s);

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Nexus": "1", ...(opts.headers || {}) } });
  let j = {};
  try { j = await r.json(); } catch { /* empty */ }
  if (r.status === 401 && path !== "/api/login") { show("login"); throw new Error(j.error || "سجل دخول"); }
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

function show(view) {
  for (const s of document.querySelectorAll("main > section")) s.hidden = s.id !== `view-${view}`;
  const logged = ["dash", "new", "settings"].includes(view);
  $("#nav").hidden = !logged;
  for (const b of document.querySelectorAll(".tab")) b.classList.toggle("active", b.dataset.view === view);
  if (view === "dash") loadDash();
  if (view === "settings") loadSettings();
}

function msg(el, text, ok = false) { el.textContent = text || ""; el.className = "msg" + (ok ? " ok" : text ? " bad" : ""); }
const hrs = (s) => (s / 3600).toFixed(1);
const when = (t) => (t ? new Date(t).toLocaleString("ar-EG") : "");

let timer = null;
async function loadDash() {
  try {
    const s = await api("/api/status");
    $("#st-kaggle").textContent = s.kaggle.configured ? "متصل" : "مش متصل";
    $("#st-kaggle").className = "v " + (s.kaggle.configured && !s.kaggle_error ? "good" : "bad");
    $("#st-user").textContent = s.kaggle_error || (s.kaggle.username ? `@${s.kaggle.username}` : s.kaggle.problem || "روح للإعدادات وحط المفتاح");
    if (s.quota) {
      const used = s.quota.gpu_used_s + s.quota.gpu_reserved_s, all = s.quota.gpu_allowed_s || 1;
      $("#st-quota").textContent = `${hrs(s.quota.gpu_left_s)} س متبقي من ${hrs(all)}`;
      $("#st-bar").style.width = `${Math.min(100, (100 * used) / all)}%`;
      $("#st-refresh").textContent = s.quota.refresh ? `بيتجدد: ${when(s.quota.refresh)}` : "";
    }
    const total = Object.values(s.jobs).reduce((a, b) => a + b, 0);
    $("#st-jobs").textContent = `${total}`;
    $("#st-worker").textContent = `شغالة دلوقتي: ${(s.jobs.running || 0) + (s.jobs.queued || 0)} · worker v${s.worker_version}`;
    fillTasks(s.tasks);
  } catch (e) { $("#st-user").textContent = e.message; }
  await loadJobs();
  clearInterval(timer);
  timer = setInterval(() => { if (!$("#view-dash").hidden) loadJobs(); }, 20000);
}

async function loadJobs() {
  const box = $("#jobs");
  try {
    const { jobs } = await api("/api/jobs");
    box.replaceChildren(...(jobs.length ? jobs.map(renderJob) : [Object.assign(document.createElement("p"), { className: "hint", textContent: "لسه مفيش شغلانات. ابدأ بـ «شغلانة جديدة» ← probe." })]));
  } catch (e) { box.textContent = e.message; }
}

function button(text, cls, fn) {
  const b = document.createElement("button");
  b.textContent = text; b.className = cls;
  b.addEventListener("click", async () => { b.disabled = true; try { await fn(); } catch (e) { alert(e.message); } finally { b.disabled = false; } });
  return b;
}

function renderJob(j) {
  const el = $("#job-tpl").content.firstElementChild.cloneNode(true);
  const badge = $(".badge", el);
  badge.textContent = STATUS[j.status] || j.status; badge.classList.add(j.status);
  $(".jt", el).textContent = j.title || j.id;
  $(".jtask", el).textContent = j.task;
  const bits = [`#${j.id}`, `محاولة ${j.attempt}`, j.gpu ? "GPU" : "CPU", `اتعملت ${when(j.created)}`];
  if (j.result?.duration_s) bits.push(`المدة ${Math.round(j.result.duration_s)} ث`);
  if (j.result?.accelerator) bits.push(j.result.accelerator);
  if (j.remote_status && !["succeeded", "failed"].includes(j.status)) bits.push(`Kaggle: ${j.remote_status}`);
  $(".meta", el).textContent = bits.join(" · ");
  if (j.error) { const p = $(".err", el); p.hidden = false; p.textContent = j.error; }
  const acts = $(".actions", el);
  if (j.kernel_url) { const a = document.createElement("a"); a.href = j.kernel_url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = "افتح على Kaggle"; acts.append(a); }
  if (["failed", "cancelled"].includes(j.status)) acts.append(button(j.result?.has_checkpoint ? "كمّل من آخر نقطة" : "إعادة المحاولة", "ghost", async () => { await api(`/api/jobs/${j.id}/retry`, { method: "POST" }); loadJobs(); }));
  if (["submitting", "queued", "running"].includes(j.status)) acts.append(button("إلغاء", "ghost danger", async () => { if (confirm("تلغي الشغلانة؟")) { await api(`/api/jobs/${j.id}/cancel`, { method: "POST" }); loadJobs(); } }));
  const ul = $(".files", el);
  const arts = new Set((j.result?.artifacts || []).map((a) => a.name));
  for (const f of j.files || []) {
    if (!(f.fileName === "result.json" || f.fileName.startsWith("artifacts/") || arts.has(f.fileName.split("/").pop()))) continue;
    const li = document.createElement("li"); const a = document.createElement("a");
    a.href = `/api/jobs/${j.id}/file?name=${encodeURIComponent(f.fileName)}`; a.textContent = f.fileName; a.target = "_blank"; a.rel = "noopener";
    li.append(a); ul.append(li);
  }
  if (j.result?.log_tail) { const d = $(".log", el); d.hidden = false; $("pre", d).textContent = j.result.log_tail; }
  return el;
}

function fillTasks(tasks) {
  const sel = $("#task");
  if (sel.options.length) return;
  for (const t of tasks) sel.append(new Option(t, t));
  sel.value = tasks.includes("probe") ? "probe" : tasks[0];
  applyPreset();
}
function applyPreset() {
  const p = PRESETS[$("#task").value] || { help: "", params: {}, gpu: true };
  $("#task-help").textContent = p.help;
  $("#params").value = JSON.stringify(p.params, null, 2);
  $("#gpu").checked = p.gpu;
}

async function loadSettings() {
  try {
    const k = await api("/api/settings/kaggle");
    const st = $("#kaggle-state");
    st.className = "state " + (k.configured ? "good" : "bad");
    st.textContent = k.configured
      ? `متصل ✓ ${k.username ? "@" + k.username : ""} · ${k.token_hint} · ${k.source === "cloudflare_secret" ? "من Cloudflare Secret" : "محفوظ هنا متشفّر"}`
      : (k.problem || "مفيش مفتاح لسه");
    $("#kaggle-delete").hidden = !(k.configured && k.source === "settings");
  } catch (e) { msg($("#kaggle-msg"), e.message); }
}

document.addEventListener("DOMContentLoaded", async () => {
  for (const b of document.querySelectorAll(".tab")) b.addEventListener("click", () => show(b.dataset.view));
  $("#task").addEventListener("change", applyPreset);
  $("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); show("login"); });
  $("#refresh").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api("/api/jobs/refresh", { method: "POST" }); await loadDash(); } catch (x) { alert(x.message); } finally { e.target.disabled = false; }
  });

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api("/api/login", { method: "POST", body: JSON.stringify({ password: e.target.password.value }) }); e.target.reset(); show("dash"); }
    catch (x) { msg($("#login-msg"), x.message); }
  });

  $("#job-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, m = $("#job-msg");
    let params;
    try { params = JSON.parse(f.params.value || "{}"); } catch { return msg(m, "الـ JSON فيه غلطة"); }
    const btn = f.querySelector("button[type=submit]"); btn.disabled = true; msg(m, "بيتبعت لـ Kaggle…", true);
    try {
      const j = await api("/api/jobs", { method: "POST", body: JSON.stringify({ task: f.task.value, title: f.title.value, params, gpu: f.gpu.checked, max_runtime_s: Math.round(Number(f.hours.value) * 3600) }) });
      if (j.status === "failed") msg(m, j.error || "فشلت");
      else { msg(m, "اتبعتت ✓", true); show("dash"); }
    } catch (x) { msg(m, x.message); } finally { btn.disabled = false; }
  });

  $("#kaggle-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, m = $("#kaggle-msg");
    msg(m, "بنجرب المفتاح على Kaggle…", true);
    try {
      const r = await api("/api/settings/kaggle", { method: "POST", body: JSON.stringify({ token: f.token.value, username: f.username.value }) });
      f.reset();
      msg(m, `اتحفظ ✓ — رصيد GPU المتبقي ${hrs(r.quota.gpu_left_s)} ساعة`, true);
      loadSettings();
    } catch (x) { msg(m, x.message); }
  });
  $("#kaggle-delete").addEventListener("click", async () => {
    if (!confirm("تمسح مفتاح Kaggle المحفوظ؟")) return;
    try { await api("/api/settings/kaggle", { method: "DELETE" }); loadSettings(); } catch (x) { msg($("#kaggle-msg"), x.message); }
  });

  try {
    const s = await api("/api/session");
    $("#minlen").textContent = s.min_password_len;
    show(!s.owner_configured ? "setup" : s.logged_in ? "dash" : "login");
  } catch (e) { document.body.append(e.message); }
});

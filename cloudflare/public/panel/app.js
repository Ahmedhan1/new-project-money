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
  if (r.status === 401 && path !== "/panel/api/login") { show("login"); throw new Error(j.error || "سجل دخول"); }
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

const STUDIO_STATE = {
  off: ["مقفول", ""], starting: ["بيتبعت لـ Kaggle", "running"], installing: ["بيجهّز البرنامج", "running"],
  ready: ["شغال ✓", "succeeded"], saving: ["بيحفظ شغلك", "running"], stopped: ["مقفول (الشغل محفوظ)", ""], error: ["حصل خطأ", "failed"],
};
const STEPS = [["starting", "Kaggle بيحجز جهاز"], ["installing", "تحميل وتثبيت NEXUS (أول مرة 5-10 دقايق)"], ["ready", "الاستوديو جاهز"]];
let studioTimer = null;

async function loadStudio() {
  let s;
  try { s = await api("/panel/api/studio"); } catch (e) { msg($("#studio-msg"), e.message); return; }
  const [label, cls] = STUDIO_STATE[s.state] || [s.state, ""];
  const badge = $("#studio-badge");
  badge.textContent = label; badge.className = "badge " + cls;
  const live = ["starting", "installing", "ready", "saving"].includes(s.state);
  $("#studio-open").hidden = s.state !== "ready";
  const stuck = s.state === "starting" && Date.now() - (s.last_seen || 0) > 15 * 60 * 1000;
  $("#studio-start").hidden = live && !stuck;
  if (stuck) $("#studio-start").textContent = "Kaggle ما بدأش — ابعته تاني";
  $("#studio-stop").hidden = !live || s.state === "saving";
  $("#studio-opts").hidden = live;
  const idx = STEPS.findIndex(([k]) => k === s.state);
  $("#studio-steps").replaceChildren(...(live && s.state !== "saving" ? STEPS.map(([k, t], i) => {
    const d = document.createElement("div");
    d.className = "step " + (i < idx || s.state === "ready" ? "done" : i === idx ? "now" : "");
    d.textContent = t; return d;
  }) : []));
  let note = "";
  if (s.state === "ready") note = `شغال${s.gpu ? " على GPU" : ""}. بيقفل لوحده بعد ${s.idle_min || 45} دقيقة من غير استخدام، وشغلك بيتحفظ.`;
  else if (s.state === "saving") note = "بيحفظ مشاريعك عشان الجلسة الجاية…";
  else if (s.state === "error") note = s.error || "";
  else if (s.state === "stopped" || s.state === "off") note = s.has_saved_work ? "مشاريعك محفوظة وهترجع لما تشغّل الاستوديو تاني." : "";
  if (s.stop_requested && live) note = "بيقفل ويحفظ…";
  msg($("#studio-msg"), note, s.state !== "error");
  const lb = $("#studio-log-box");
  lb.hidden = !(s.log && s.log.length);
  $("#studio-log").textContent = (s.log || []).join("\n");
  clearTimeout(studioTimer);
  if (live) studioTimer = setTimeout(() => { if (!$("#view-dash").hidden) loadStudio(); }, s.state === "ready" ? 30000 : 8000);
  if (s.kernel_url) {
    let a = $("#studio-kaggle");
    if (!a) { a = document.createElement("a"); a.id = "studio-kaggle"; a.target = "_blank"; a.rel = "noopener noreferrer"; a.className = "hint"; $("#studio-controls").append(a); }
    a.href = s.kernel_url; a.textContent = "النوتبوك على Kaggle";
  }
}

let timer = null;
async function loadDash() {
  loadStudio();
  try {
    const s = await api("/panel/api/status");
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
    const { jobs } = await api("/panel/api/jobs");
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
  if (["failed", "cancelled"].includes(j.status)) acts.append(button(j.result?.has_checkpoint ? "كمّل من آخر نقطة" : "إعادة المحاولة", "ghost", async () => { await api(`/panel/api/jobs/${j.id}/retry`, { method: "POST" }); loadJobs(); }));
  if (["submitting", "queued", "running"].includes(j.status)) acts.append(button("إلغاء", "ghost danger", async () => { if (confirm("تلغي الشغلانة؟")) { await api(`/panel/api/jobs/${j.id}/cancel`, { method: "POST" }); loadJobs(); } }));
  const ul = $(".files", el);
  const arts = new Set((j.result?.artifacts || []).map((a) => a.name));
  for (const f of j.files || []) {
    if (!(f.fileName === "result.json" || f.fileName.startsWith("artifacts/") || arts.has(f.fileName.split("/").pop()))) continue;
    const li = document.createElement("li"); const a = document.createElement("a");
    a.href = `/panel/api/jobs/${j.id}/file?name=${encodeURIComponent(f.fileName)}`; a.textContent = f.fileName; a.target = "_blank"; a.rel = "noopener";
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

async function loadYoutube() {
  try {
    const y = await api("/panel/api/youtube");
    $("#yt-redirect").textContent = y.redirect_uri;
    const st = $("#yt-state");
    st.className = "state " + (y.connected ? "good" : "bad");
    st.textContent = y.connected ? `متصلة ✓ ${y.channel?.title ? "— " + y.channel.title : ""}` : y.client_configured ? `Client محفوظ (${y.client_id_hint}) — فاضل تضغط «اربط القناة»` : "مش متصلة";
    $("#yt-connected").hidden = !y.connected;
    $("#yt-connect").hidden = !y.client_configured || y.connected;
    $("#yt-setup").open = !y.connected;
  } catch (e) { msg($("#yt-msg"), e.message); }
}

async function loadSettings() {
  loadYoutube();
  try {
    const k = await api("/panel/api/settings/kaggle");
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
  $("#logout").addEventListener("click", async () => { await api("/panel/api/logout", { method: "POST" }); show("login"); });
  $("#refresh").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api("/panel/api/jobs/refresh", { method: "POST" }); await loadDash(); } catch (x) { alert(x.message); } finally { e.target.disabled = false; }
  });

  $("#studio-start").addEventListener("click", async (e) => {
    e.target.disabled = true; msg($("#studio-msg"), "بيتبعت لـ Kaggle…", true);
    try {
      await api("/panel/api/studio/start", { method: "POST", body: JSON.stringify({ gpu: $("#studio-gpu").checked, hours: Number($("#studio-hours").value), idle_min: Number($("#studio-idle").value) }) });
      await loadStudio();
    } catch (x) { msg($("#studio-msg"), x.message); } finally { e.target.disabled = false; }
  });
  $("#studio-stop").addEventListener("click", async (e) => {
    if (!confirm("تقفل الاستوديو؟ شغلك هيتحفظ للجلسة الجاية.")) return;
    e.target.disabled = true;
    try { await api("/panel/api/studio/stop", { method: "POST" }); await loadStudio(); } catch (x) { alert(x.message); } finally { e.target.disabled = false; }
  });
  $("#yt-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/panel/api/youtube/client", { method: "POST", body: JSON.stringify({ client_id: e.target.client_id.value, client_secret: e.target.client_secret.value }) });
      e.target.reset(); msg($("#yt-msg"), "اتحفظ ✓ — اضغط «اربط القناة مع Google»", true); loadYoutube();
    } catch (x) { msg($("#yt-msg"), x.message); }
  });
  $("#yt-delete").addEventListener("click", async () => {
    if (!confirm("تفصل قناة يوتيوب؟")) return;
    try { await api("/panel/api/youtube", { method: "DELETE" }); loadYoutube(); } catch (x) { msg($("#yt-msg"), x.message); }
  });
  const params = new URLSearchParams(location.search);
  if (params.get("youtube") === "ok" || params.get("youtube_error")) {
    setTimeout(() => { show("settings"); msg($("#yt-msg"), params.get("youtube_error") || "القناة اتربطت ✓ — هتشتغل من جلسة الاستوديو الجاية", !params.get("youtube_error")); }, 400);
  }
  const q = params.get("studio");
  if (q === "off") setTimeout(() => msg($("#studio-msg"), "الاستوديو مقفول — شغّله الأول."), 300);
  if (q === "lost") setTimeout(() => msg($("#studio-msg"), "الاستوديو مش بيرد. لو لسه بيقفل استنى شوية، أو شغّله تاني."), 300);

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api("/panel/api/login", { method: "POST", body: JSON.stringify({ password: e.target.password.value }) }); e.target.reset(); show("dash"); }
    catch (x) { msg($("#login-msg"), x.message); }
  });

  $("#job-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, m = $("#job-msg");
    let params;
    try { params = JSON.parse(f.params.value || "{}"); } catch { return msg(m, "الـ JSON فيه غلطة"); }
    const btn = f.querySelector("button[type=submit]"); btn.disabled = true; msg(m, "بيتبعت لـ Kaggle…", true);
    try {
      const j = await api("/panel/api/jobs", { method: "POST", body: JSON.stringify({ task: f.task.value, title: f.title.value, params, gpu: f.gpu.checked, max_runtime_s: Math.round(Number(f.hours.value) * 3600) }) });
      if (j.status === "failed") msg(m, j.error || "فشلت");
      else { msg(m, "اتبعتت ✓", true); show("dash"); }
    } catch (x) { msg(m, x.message); } finally { btn.disabled = false; }
  });

  $("#kaggle-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, m = $("#kaggle-msg");
    msg(m, "بنجرب المفتاح على Kaggle…", true);
    try {
      const r = await api("/panel/api/settings/kaggle", { method: "POST", body: JSON.stringify({ token: f.token.value, username: f.username.value }) });
      f.reset();
      msg(m, `اتحفظ ✓ — رصيد GPU المتبقي ${hrs(r.quota.gpu_left_s)} ساعة`, true);
      loadSettings();
    } catch (x) { msg(m, x.message); }
  });
  $("#kaggle-delete").addEventListener("click", async () => {
    if (!confirm("تمسح مفتاح Kaggle المحفوظ؟")) return;
    try { await api("/panel/api/settings/kaggle", { method: "DELETE" }); loadSettings(); } catch (x) { msg($("#kaggle-msg"), x.message); }
  });

  try {
    const s = await api("/panel/api/session");
    $("#minlen").textContent = s.min_password_len;
    show(!s.owner_configured ? "setup" : s.logged_in ? "dash" : "login");
  } catch (e) { document.body.append(e.message); }
});

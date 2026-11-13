// Job ledger (D1) + the Kaggle lifecycle: submit -> poll (cron every 5 min, or "تحديث" in the UI) -> collect outputs.
// The NEXUS worker that runs on Kaggle is nexus/cloud/worker_kit (frozen into worker_script.generated.js), with the job
// bundle embedded exactly as the Python dispatcher does it, so results/checkpoints have the same contracts.

import { WORKER_SCRIPT, WORKER_TASKS, WORKER_VERSION } from "./worker_script.generated.js";
import * as kaggle from "./kaggle.js";
import { b64utf8 } from "./security.js";

export const TASKS = WORKER_TASKS;
const ACTIVE = ["submitting", "queued", "running"];
const LOST_GRACE_S = 900;
const MAX_PARAMS_BYTES = 256 * 1024; // the whole pushed script must stay well under what Kaggle accepts (~2.9 MB failed)
const GPU_TASKS_REQUIRED = new Set(["batch_llm", "a2_wan_video"]);

export class JobError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

function newId() {
  const r = crypto.getRandomValues(new Uint8Array(4));
  return Date.now().toString(36) + Array.from(r, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function slugFor(id, attempt) {
  return `nexus-${id}` + (attempt > 1 ? `-a${attempt}` : "");
}

export function buildScript(bundle) {
  const marker = '_EMBEDDED_BUNDLE_B64 = ""';
  if (!WORKER_SCRIPT.includes(marker)) throw new Error("worker script has no bundle marker");
  return WORKER_SCRIPT.replace(marker, `_EMBEDDED_BUNDLE_B64 = "${b64utf8(JSON.stringify(bundle))}"`);
}

function parseJson(s, fallback = null) {
  try {
    return s ? JSON.parse(s) : fallback;
  } catch {
    return fallback;
  }
}

export function publicJob(row) {
  if (!row) return null;
  const result = parseJson(row.result);
  return {
    id: row.id, task: row.task, title: row.title, params: parseJson(row.params, {}), gpu: !!row.gpu, max_runtime_s: row.max_runtime_s,
    status: row.status, attempt: row.attempt, kernel_ref: row.kernel_ref, kernel_url: row.kernel_url, version: row.version,
    remote_status: row.remote_status, error: row.error, files: parseJson(row.files, []), created: row.created, updated: row.updated,
    submitted: row.submitted, finished: row.finished,
    result: result && {
      status: result.status, duration_s: result.duration_s, accelerator: result.accelerator, error: result.error,
      artifacts: result.artifacts, metrics: result.metrics, log_tail: result.log_tail, has_checkpoint: !!result.checkpoint,
    },
  };
}

export async function listJobs(db, limit = 50) {
  const { results } = await db.prepare("SELECT * FROM jobs ORDER BY created DESC LIMIT ?").bind(limit).all();
  return results.map(publicJob);
}

export async function getJob(db, id) {
  return db.prepare("SELECT * FROM jobs WHERE id = ?").bind(id).first();
}

async function update(db, id, fields) {
  const keys = Object.keys(fields);
  const sql = `UPDATE jobs SET ${keys.map((k) => `${k} = ?`).join(", ")}, updated = ? WHERE id = ?`;
  await db.prepare(sql).bind(...keys.map((k) => fields[k]), Date.now(), id).run();
}

export async function requireCreds(env) {
  const c = await kaggle.loadCredentials(env);
  if (!c) throw new JobError("Kaggle غير متصل: حط مفتاح الـ API في صفحة الإعدادات", 409);
  if (c.undecryptable) throw new JobError("المفتاح المحفوظ لا يمكن فكه (اتغيرت كلمة السر). أدخل مفتاح Kaggle تاني في الإعدادات", 409);
  return c; // username is optional: pushes return the kernel ref, and refs carry the owner
}

export function validateSubmission(body, env) {
  const task = String(body?.task || "");
  if (!TASKS.includes(task)) throw new JobError(`مهمة غير معروفة: ${task}`);
  const params = body.params ?? {};
  if (typeof params !== "object" || Array.isArray(params) || params === null) throw new JobError("params لازم تكون JSON object");
  if (new TextEncoder().encode(JSON.stringify(params)).length > MAX_PARAMS_BYTES) throw new JobError("params كبيرة جدا (الحد 256KB). الملفات الكبيرة تتبعت كرابط https");
  const gpu = GPU_TASKS_REQUIRED.has(task) ? true : body.gpu !== false;
  const def = Number(env.MAX_RUNTIME_S || 10800);
  const max = Math.max(300, Math.min(kaggle.SESSION_MAX_S, Math.floor(Number(body.max_runtime_s || def))));
  const title = String(body.title || "").slice(0, 120);
  return { task, params, gpu, max, title };
}

async function pushAttempt(env, creds, row, attempt, checkpoint) {
  const params = parseJson(row.params, {});
  const bundle = {
    job_id: row.id, task: row.task, params, attempt, provider: "kaggle", connection_id: "kaggle_primary",
    max_runtime_s: row.max_runtime_s, worker_version: WORKER_VERSION, inputs: [], checkpoint: checkpoint || null,
  };
  const slug = slugFor(row.id, attempt);
  return kaggle.push(creds, { slug, script: buildScript(bundle), gpu: !!row.gpu, timeoutS: row.max_runtime_s });
}

async function gpuGuard(env, creds, gpu, maxRuntime) {
  if (!gpu) return null;
  const q = await kaggle.quota(creds);
  const reserve = Number(env.MIN_GPU_RESERVE_S || 3600);
  if (q.gpu_allowed_s > 0 && q.gpu_left_s < reserve) {
    throw new JobError(`رصيد GPU الأسبوعي على Kaggle قرب يخلص (متبقي ${(q.gpu_left_s / 3600).toFixed(1)} ساعة). بيتجدد ${q.refresh || "الأسبوع الجاي"}`, 409);
  }
  return q;
}

export async function submit(env, body) {
  const v = validateSubmission(body, env);
  const creds = await requireCreds(env);
  await gpuGuard(env, creds, v.gpu, v.max);
  const id = newId(), now = Date.now();
  await env.DB.prepare("INSERT INTO jobs (id, task, title, params, gpu, max_runtime_s, status, attempt, created, updated) VALUES (?, ?, ?, ?, ?, ?, 'submitting', 1, ?, ?)")
    .bind(id, v.task, v.title, JSON.stringify(v.params), v.gpu ? 1 : 0, v.max, now, now).run();
  const row = await getJob(env.DB, id);
  try {
    const r = await pushAttempt(env, creds, row, 1, null);
    await update(env.DB, id, { status: "queued", kernel_ref: r.ref, kernel_url: r.url, version: r.version, submitted: Date.now() });
  } catch (e) {
    await update(env.DB, id, { status: "failed", error: String(e.message || e).slice(0, 1000), finished: Date.now() });
  }
  return publicJob(await getJob(env.DB, id));
}

export async function retry(env, id) {
  const row = await getJob(env.DB, id);
  if (!row) throw new JobError("الشغلانة مش موجودة", 404);
  if (ACTIVE.includes(row.status)) throw new JobError("الشغلانة لسه شغالة", 409);
  const creds = await requireCreds(env);
  await gpuGuard(env, creds, !!row.gpu, row.max_runtime_s);
  const checkpoint = parseJson(row.result)?.checkpoint || null; // resumable tasks skip the steps already done
  const attempt = row.attempt + 1;
  try {
    const r = await pushAttempt(env, creds, row, attempt, checkpoint);
    await update(env.DB, id, { status: "queued", attempt, kernel_ref: r.ref, kernel_url: r.url, version: r.version, error: null, remote_status: null,
      result: null, files: null, submitted: Date.now(), finished: null });
  } catch (e) {
    await update(env.DB, id, { attempt, status: "failed", error: String(e.message || e).slice(0, 1000), finished: Date.now() });
  }
  return publicJob(await getJob(env.DB, id));
}

export async function cancel(env, id) {
  const row = await getJob(env.DB, id);
  if (!row) throw new JobError("الشغلانة مش موجودة", 404);
  if (!ACTIVE.includes(row.status)) return publicJob(row);
  // Kaggle's public API can only cancel a session by an id it does not return from a push; NEXUS stops tracking the
  // job and the run ends by itself at its time limit (same behaviour as nexus/cloud/compute/kaggle.py).
  await update(env.DB, id, { status: "cancelled", error: "اتلغت من لوحة التحكم. لو لسه شغالة على Kaggle هتقف لوحدها عند الحد الزمني، أو أوقفها من صفحة النوتبوك.", finished: Date.now() });
  return publicJob(await getJob(env.DB, id));
}

async function collect(creds, row, fetchImpl = fetch) {
  const out = await kaggle.outputs(creds, row.kernel_ref, fetchImpl);
  const files = out.files.map((f) => ({ fileName: f.fileName }));
  let result = null;
  const rj = out.files.find((f) => f.fileName === "result.json");
  if (rj?.url) {
    try {
      const r = await fetchImpl(rj.url);
      if (r.ok) result = await r.json();
    } catch { /* reported below as missing */ }
  }
  return { files, result, log: kaggle.logText(out.log).slice(-4000) };
}

/** Poll one active job; returns true when its row changed. */
export async function refreshJob(env, creds, row, now = Date.now(), fetchImpl = fetch) {
  if (!ACTIVE.includes(row.status) || !row.kernel_ref) return false;
  let st;
  try {
    st = await kaggle.status(creds, row.kernel_ref, fetchImpl);
  } catch (e) {
    await update(env.DB, row.id, { remote_status: `poll error: ${String(e.message).slice(0, 200)}` });
    return true;
  }
  if (st.state === "queued" || st.state === "running" || st.state === "unknown") {
    const startedS = (row.submitted || row.created) / 1000;
    if (now / 1000 - startedS > row.max_runtime_s + LOST_GRACE_S + 6 * 3600) {
      await update(env.DB, row.id, { status: "failed", remote_status: st.raw, error: "الشغلانة عدّت الحد الزمني بكتير من غير ما تخلص (lost). اعمل إعادة محاولة.", finished: now });
      return true;
    }
    const next = st.state === "running" ? "running" : row.status === "submitting" ? "queued" : row.status;
    if (next !== row.status || st.raw !== row.remote_status) {
      await update(env.DB, row.id, { status: next, remote_status: st.raw });
      return true;
    }
    return false;
  }
  if (st.state === "cancelled") {
    await update(env.DB, row.id, { status: "cancelled", remote_status: st.raw, error: "اتلغت على Kaggle", finished: now });
    return true;
  }
  let c = { files: [], result: null, log: "" };
  try {
    c = await collect(creds, row, fetchImpl);
  } catch (e) {
    c.log = `could not list outputs: ${e.message}`;
  }
  const res = c.result;
  let status, error = null;
  if (st.state === "complete" && res?.status === "succeeded") status = "succeeded";
  else {
    status = "failed";
    error = res?.error || st.failure || (st.state === "complete" ? "Kaggle خلص بس مفيش result.json" : "Kaggle رجّع ERROR") ;
    if (res?.status === "lost") error = `${error} — إعادة المحاولة هتكمل من آخر checkpoint`;
    if (!res && c.log) error += `\n${c.log.slice(-1500)}`;
  }
  const slim = res ? { ...res, log_tail: String(res.log_tail || "").slice(-4000) } : null;
  await update(env.DB, row.id, { status, remote_status: st.raw, error: error && String(error).slice(0, 4000), result: slim ? JSON.stringify(slim) : null,
    files: JSON.stringify(c.files.slice(0, 500)), finished: now });
  return true;
}

export async function refreshActive(env, { limit = 20 } = {}) {
  const { results } = await env.DB.prepare(`SELECT * FROM jobs WHERE status IN ('submitting','queued','running') ORDER BY updated ASC LIMIT ?`).bind(limit).all();
  if (!results.length) return { checked: 0, changed: 0 };
  let creds;
  try {
    creds = await requireCreds(env);
  } catch (e) {
    return { checked: 0, changed: 0, error: e.message };
  }
  let changed = 0;
  for (const row of results) if (await refreshJob(env, creds, row)) changed++;
  return { checked: results.length, changed };
}

export async function fileUrl(env, id, name) {
  const row = await getJob(env.DB, id);
  if (!row?.kernel_ref) throw new JobError("الشغلانة مش موجودة", 404);
  const creds = await requireCreds(env);
  const out = await kaggle.outputs(creds, row.kernel_ref);
  const f = out.files.find((x) => x.fileName === name);
  if (!f?.url) throw new JobError("الملف مش موجود في مخرجات Kaggle (ممكن يكون اتشغّل إصدار أحدث)", 404);
  return f.url;
}

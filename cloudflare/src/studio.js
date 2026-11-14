// NEXUS Studio: the FULL NEXUS app (same UI/features as the Windows app) running in a private Kaggle kernel, reached
// through this Worker. Lifecycle (one session at a time, state in the NexusStore Durable Object):
//
//   start  -> push cloudflare/studio/studio_kernel.py (GPU or CPU, private, internet) with a per-session config:
//             fresh NEXUS_API_TOKEN, callback secret, previous session's kernel attached as input (projects restored)
//   kernel -> POST /panel/api/studio/callback  installing | starting | ready(url) | saving | stopped | error
//             each heartbeat gets {stop} back: true when the owner pressed Stop or the UI was idle too long
//   proxy  -> every non-/panel request of a logged-in owner is forwarded to the tunnel with
//             "Authorization: Bearer <session token>" (the app's hosted mode); the browser never sees the token.

import { STUDIO_SCRIPT } from "./worker_script.generated.js";
import * as kaggle from "./kaggle.js";
import { b64url, b64utf8 } from "./security.js";
import { JobError, requireCreds } from "./jobs.js";

const SLUGS = ["nexus-studio-a", "nexus-studio-b"];
export const LIVE = ["starting", "installing", "ready", "saving"];
const DEFAULTS = { repo: "https://github.com/Ahmedhan1/new-project-money", ref: "main", idle_min: 45, hours: 6, gpu: true };

export const STUDIO_SCHEMA = [
  `CREATE TABLE IF NOT EXISTS studio (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    session TEXT, state TEXT NOT NULL DEFAULT 'off', slug TEXT, kernel_ref TEXT, kernel_url TEXT, url TEXT,
    token TEXT, secret TEXT, gpu INTEGER, idle_s INTEGER, started INTEGER, updated INTEGER, last_seen INTEGER,
    last_activity INTEGER, stop_requested INTEGER NOT NULL DEFAULT 0, saved_ref TEXT, log TEXT, error TEXT)`,
  `INSERT OR IGNORE INTO studio (id, state) VALUES (1, 'off')`,
];

const rnd = (n = 24) => b64url(crypto.getRandomValues(new Uint8Array(n)));

async function row(db) {
  return db.prepare("SELECT * FROM studio WHERE id = 1").first();
}

async function set(db, fields) {
  const keys = Object.keys(fields);
  await db.prepare(`UPDATE studio SET ${keys.map((k) => `${k} = ?`).join(", ")}, updated = ? WHERE id = 1`)
    .bind(...keys.map((k) => fields[k]), Date.now()).run();
}

export function publicState(r) {
  if (!r) return { state: "off" };
  let log = [];
  try { log = JSON.parse(r.log || "[]"); } catch { /* ignore */ }
  return { state: r.state, gpu: !!r.gpu, kernel_url: r.kernel_url, started: r.started, last_seen: r.last_seen, last_activity: r.last_activity,
    stop_requested: !!r.stop_requested, has_saved_work: !!r.saved_ref, error: r.error, log: log.slice(-25), idle_min: r.idle_s ? Math.round(r.idle_s / 60) : null };
}

export async function status(env) {
  return publicState(await row(env.DB));
}

export async function start(env, body = {}) {
  const r = await row(env.DB);
  if (LIVE.includes(r.state)) throw new JobError("الاستوديو شغال بالفعل", 409);
  const gpu = body.gpu !== false;
  const hours = Math.max(1, Math.min(12, Number(body.hours || DEFAULTS.hours)));
  const idle = Math.max(10, Math.min(240, Number(body.idle_min || DEFAULTS.idle_min)));
  const creds = await requireCreds(env);
  if (gpu) {
    const q = await kaggle.quota(creds);
    if (q.gpu_allowed_s > 0 && q.gpu_left_s < Number(env.MIN_GPU_RESERVE_S || 3600)) {
      throw new JobError(`رصيد GPU الأسبوعي قرب يخلص (${(q.gpu_left_s / 3600).toFixed(1)} س). شغّل الاستوديو من غير GPU.`, 409);
    }
  }
  // Never push over the notebook whose output we restore from: use the other one of the pair.
  const savedSlug = r.saved_ref ? r.saved_ref.split("/").pop() : null;
  const slug = savedSlug ? SLUGS.find((x) => x !== savedSlug) : SLUGS[(SLUGS.indexOf(r.slug) + 1) % SLUGS.length];
  const session = rnd(9), token = rnd(32), secret = rnd(32);
  const origin = new URL(body.origin).origin;
  const cfg = { repo: env.STUDIO_REPO || DEFAULTS.repo, ref: env.STUDIO_REF || DEFAULTS.ref, token, secret, session,
    callback: `${origin}/panel/api/studio/callback`, max_s: hours * 3600, restore_from: r.saved_ref || null };
  const script = STUDIO_SCRIPT.replace('_CONFIG_B64 = ""', `_CONFIG_B64 = "${b64utf8(JSON.stringify(cfg))}"`);
  const res = await kaggle.push(creds, { slug, script, gpu, timeoutS: hours * 3600, kernelSources: r.saved_ref ? [r.saved_ref] : [] });
  const now = Date.now();
  await set(env.DB, { session, state: "starting", slug, kernel_ref: res.ref, kernel_url: res.url, url: null, token, secret, gpu: gpu ? 1 : 0,
    idle_s: idle * 60, started: now, last_seen: now, last_activity: now, stop_requested: 0, log: "[]", error: null });
  return status(env);
}

export async function stop(env) {
  const r = await row(env.DB);
  if (!LIVE.includes(r.state)) return publicState(r);
  await set(env.DB, { stop_requested: 1 });
  return status(env);
}

function equal(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

/** Heartbeat from the kernel. Authenticated by the per-session secret only. */
export async function callback(env, body) {
  const r = await row(env.DB);
  if (!r.secret || !equal(String(body.secret || ""), r.secret) || body.session !== r.session) throw new JobError("forbidden", 403);
  const state = String(body.state || "");
  if (!["installing", "starting", "ready", "saving", "stopped", "error"].includes(state)) throw new JobError("bad state");
  const now = Date.now();
  const fields = { state, last_seen: now, log: JSON.stringify((Array.isArray(body.log) ? body.log : []).slice(-25).map((x) => String(x).slice(0, 400))) };
  if (state === "ready") {
    const u = String(body.url || "");
    if (!/^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(u)) throw new JobError("bad tunnel url");
    fields.url = u;
    if (r.state !== "ready") fields.last_activity = now; // idle clock starts when the studio becomes usable
  }
  if (state === "stopped" || state === "error") {
    fields.url = null;
    fields.token = null;
    // Only a session that really saved NEXUS_HOME becomes the next restore source; otherwise the older save stays.
    if (body.saved === true) fields.saved_ref = r.kernel_ref;
    if (state === "error") fields.error = String(body.error || "خطأ غير معروف").slice(0, 1000);
  }
  await set(env.DB, fields);
  const idle = state === "ready" && now - (fields.last_activity || r.last_activity || now) > (r.idle_s || 2700) * 1000;
  return { stop: !!r.stop_requested || idle, reason: r.stop_requested ? "owner" : idle ? "idle" : null };
}

/** Secrets a live session needs at start-up (YouTube), handed over the authenticated callback channel only. */
export async function secrets(env, body, youtubeEnv) {
  const r = await row(env.DB);
  if (!r.secret || !equal(String(body.secret || ""), r.secret) || body.session !== r.session || !LIVE.includes(r.state)) throw new JobError("forbidden", 403);
  return { env: await youtubeEnv(env) };
}

/** Where to proxy (read by the Worker; cached briefly per isolate). */
export async function route(env) {
  const r = await row(env.DB);
  return r.state === "ready" && r.url && r.token ? { url: r.url, token: r.token } : null;
}

export async function touch(env) {
  await env.DB.prepare("UPDATE studio SET last_activity = ? WHERE id = 1").bind(Date.now()).run();
}

/** Cron: notice a kernel that died without reporting (Kaggle error / timeout / lost). */
export async function watchdog(env) {
  const r = await row(env.DB);
  if (!LIVE.includes(r.state) || !r.kernel_ref) return null;
  if (Date.now() - (r.last_seen || 0) < 4 * 60 * 1000) return null;
  let st;
  try {
    st = await kaggle.status(await requireCreds(env), r.kernel_ref);
  } catch (e) {
    return { error: e.message };
  }
  if (st.state === "complete" || st.state === "error" || st.state === "cancelled") {
    // The kernel died without reporting: keep the previous restore source (we cannot know this one saved anything).
    await set(env.DB, { state: st.state === "complete" ? "stopped" : "error", url: null, token: null,
      error: st.state === "complete" ? null : st.failure || `Kaggle: ${st.raw}` });
  }
  return st;
}

// ---------------------------------------------------------------- the gateway (runs in the Worker, not the DO)
const HOP = ["cookie", "host", "authorization", "cf-connecting-ip", "cf-ipcountry", "cf-ray", "cf-visitor", "cf-worker", "x-forwarded-for",
  "x-forwarded-proto", "x-forwarded-host", "x-real-ip", "forwarded", "connection", "keep-alive", "upgrade", "te", "trailer", "transfer-encoding"];

export async function proxy(request, rt) {
  const url = new URL(request.url);
  const target = new URL(url.pathname + url.search, rt.url);
  const h = new Headers(request.headers);
  for (const k of HOP) h.delete(k);
  h.set("Authorization", `Bearer ${rt.token}`);
  const body = ["GET", "HEAD"].includes(request.method) ? undefined : request.body;
  let r;
  try {
    r = await fetch(target, { method: request.method, headers: h, body, redirect: "manual" });
  } catch {
    return null; // tunnel unreachable
  }
  if (r.status === 530 || r.status === 1033) return null; // Cloudflare: tunnel gone
  const out = new Response(r.body, r);
  const loc = out.headers.get("Location");
  if (loc && loc.startsWith(rt.url)) out.headers.set("Location", loc.slice(rt.url.length) || "/");
  out.headers.delete("Set-Cookie"); // the app's own session cookie is not used: the gateway authenticates
  return out;
}

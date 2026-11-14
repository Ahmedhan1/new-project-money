// NEXUS Cloud - control plane on Cloudflare Workers (free plan). UI in ./public, API under /api (served by the
// NexusStore Durable Object, which holds all state in its SQLite storage), cron polls Kaggle.
import { DurableObject } from "cloudflare:workers";
import * as sec from "./security.js";
import * as kaggle from "./kaggle.js";
import * as jobs from "./jobs.js";
import { WORKER_VERSION } from "./worker_script.generated.js";
import { ensureSchema } from "./schema.js";
import * as studio from "./studio.js";
import * as youtube from "./youtube.js";

const SECURITY_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
  "Cache-Control": "no-store",
};

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json; charset=utf-8", ...SECURITY_HEADERS, ...headers } });
}

const err = (message, status = 400) => json({ error: message }, status);

async function body(request) {
  const len = Number(request.headers.get("Content-Length") || 0);
  if (len > 512 * 1024) throw new jobs.JobError("الطلب كبير جدا", 413);
  try {
    return await request.json();
  } catch {
    throw new jobs.JobError("JSON غير صالح");
  }
}

async function kaggleSettings(env) {
  const c = await kaggle.loadCredentials(env).catch(() => null);
  if (!c) return { configured: false };
  if (c.undecryptable) return { configured: false, source: c.source, problem: "المفتاح المحفوظ لا يمكن فكه: اتغيرت كلمة السر. أدخله تاني." };
  return { configured: true, source: c.source, kind: c.kind, username: c.username || null, token_hint: kaggle.hint(c.token) };
}

async function handleApi(request, env, url) {
  const path = url.pathname.replace(/^\/panel(?=\/api\/)/, ""); // the panel lives under /panel; its API under /panel/api
  const m = request.method;

  // ---- studio kernel heartbeat: authenticated by its per-session secret (no browser, no cookie, no CSRF header)
  if (path === "/api/studio/callback" && m === "POST") return json(await studio.callback(env, await body(request)));
  if (path === "/api/studio/secrets" && m === "POST") return json(await studio.secrets(env, await body(request), youtube.sessionEnv));
  // Google redirects back here cross-site, so the SameSite=Strict cookie is absent: the one-time state authenticates.
  if (path === "/api/youtube/callback" && m === "GET") {
    try {
      await youtube.finish(env, url.origin, url.searchParams);
      return new Response(null, { status: 302, headers: { Location: "/panel/?youtube=ok", ...SECURITY_HEADERS } });
    } catch (e) {
      return new Response(null, { status: 302, headers: { Location: `/panel/?youtube_error=${encodeURIComponent(e.message)}`, ...SECURITY_HEADERS } });
    }
  }

  // ---- public endpoints
  if (path === "/api/health") return json({ ok: true, owner_configured: sec.ownerConfigured(env), storage: "durable-object-sqlite" });
  if (path === "/api/session" && m === "GET") {
    return json({ owner_configured: sec.ownerConfigured(env), logged_in: await sec.hasSession(env, request), min_password_len: sec.MIN_PASSWORD_LEN });
  }
  if (!sec.csrfOk(request)) return err("CSRF check failed", 403);
  if (path === "/api/login" && m === "POST") {
    if (!sec.ownerConfigured(env)) return err(`لازم تضيف Secret اسمه OWNER_PASSWORD (على الأقل ${sec.MIN_PASSWORD_LEN} حرف) في إعدادات الـ Worker على Cloudflare`, 503);
    const ip = sec.clientIp(request);
    if (await sec.tooManyFailures(env.DB, ip)) return err("محاولات كتير غلط. استنى 15 دقيقة.", 429);
    const b = await body(request);
    if (!(await sec.passwordMatches(env, b.password || ""))) {
      await sec.recordFailure(env.DB, ip);
      return err("كلمة السر غلط", 401);
    }
    return json({ ok: true }, 200, { "Set-Cookie": await sec.issueSession(env) });
  }
  if (path === "/api/logout" && m === "POST") return json({ ok: true }, 200, { "Set-Cookie": sec.clearSession() });

  // ---- owner only
  if (!(await sec.hasSession(env, request))) return err("سجل دخول الأول", 401);

  if (path === "/api/status" && m === "GET") {
    const k = await kaggleSettings(env);
    let q = null, kaggle_error = null;
    if (k.configured) {
      try {
        q = await kaggle.quota(await jobs.requireCreds(env));
      } catch (e) {
        kaggle_error = e.message;
      }
    }
    const counts = await env.DB.prepare("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").all();
    return json({ kaggle: k, quota: q, kaggle_error, worker_version: WORKER_VERSION, tasks: jobs.TASKS,
      jobs: Object.fromEntries(counts.results.map((r) => [r.status, r.n])) });
  }

  if (path === "/api/settings/kaggle") {
    if (m === "GET") return json(await kaggleSettings(env));
    if (m === "DELETE") {
      await sec.deleteSetting(env.DB, "secret:kaggle");
      return json(await kaggleSettings(env));
    }
    if (m === "POST") {
      const b = await body(request);
      const creds = kaggle.parseCredentials(b.token, b.username);
      // Verify before saving: a wrong key must not be stored silently.
      const q = await kaggle.quota(creds); // proves the key works before it is stored
      if (!creds.username) creds.username = await kaggle.introspectUsername(creds).catch(() => "");
      await sec.putSecret(env, "kaggle", { token: creds.token, username: creds.username, kind: creds.kind });
      return json({ ...(await kaggleSettings(env)), quota: q, verified: true });
    }
  }

  if (path === "/api/youtube" && m === "GET") return json(await youtube.status(env, url.origin));
  if (path === "/api/youtube" && m === "DELETE") {
    await youtube.disconnect(env);
    return json(await youtube.status(env, url.origin));
  }
  if (path === "/api/youtube/client" && m === "POST") {
    await youtube.saveClient(env, await body(request));
    return json(await youtube.status(env, url.origin));
  }
  if (path === "/api/youtube/connect" && m === "GET") {
    return new Response(null, { status: 302, headers: { Location: await youtube.begin(env, url.origin), ...SECURITY_HEADERS } });
  }
  if (path === "/api/studio" && m === "GET") return json(await studio.status(env));
  if (path === "/api/studio/start" && m === "POST") return json(await studio.start(env, { ...(await body(request)), origin: url.origin }));
  if (path === "/api/studio/stop" && m === "POST") return json(await studio.stop(env));

  if (path === "/api/jobs" && m === "GET") return json({ jobs: await jobs.listJobs(env.DB, Math.min(200, Number(url.searchParams.get("limit") || 50))) });
  if (path === "/api/jobs" && m === "POST") return json(await jobs.submit(env, await body(request)), 201);
  if (path === "/api/jobs/refresh" && m === "POST") return json(await jobs.refreshActive(env));

  const jm = path.match(/^\/api\/jobs\/([a-z0-9]{8,40})(?:\/(retry|cancel|file))?$/);
  if (jm) {
    const [, id, action] = jm;
    if (!action && m === "GET") {
      const row = await jobs.getJob(env.DB, id);
      return row ? json(jobs.publicJob(row)) : err("مش موجودة", 404);
    }
    if (action === "retry" && m === "POST") return json(await jobs.retry(env, id));
    if (action === "cancel" && m === "POST") return json(await jobs.cancel(env, id));
    if (action === "file" && m === "GET") {
      const name = url.searchParams.get("name") || "";
      return new Response(null, { status: 302, headers: { Location: await jobs.fileUrl(env, id, name), ...SECURITY_HEADERS } });
    }
  }
  return err("Not found", 404);
}

/** The API with a D1-compatible `env.DB` (used by the Durable Object below, and directly by the tests). */
export async function app(request, env) {
  const url = new URL(request.url);
  try {
    await ensureSchema(env.DB);
    return await handleApi(request, env, url);
  } catch (e) {
    if (e instanceof jobs.JobError) return err(e.message, e.status);
    if (e instanceof kaggle.KaggleError) return err(e.auth ? `Kaggle رفض المفتاح: ${e.message}` : e.message, e.auth ? 400 : 502);
    console.error("unhandled", e && e.stack);
    return err("خطأ داخلي", 500);
  }
}

/** D1-shaped adapter over Durable Object SQLite storage (prepare/bind/first/all/run/batch). */
export function sqlDb(sql) {
  const clean = (a) => a.map((v) => (v === undefined ? null : typeof v === "boolean" ? (v ? 1 : 0) : v));
  const stmt = (q, args = []) => ({
    bind: (...a) => stmt(q, clean(a)),
    first: async () => sql.exec(q, ...args).toArray()[0] ?? null,
    all: async () => ({ results: sql.exec(q, ...args).toArray() }),
    run: async () => {
      const c = sql.exec(q, ...args);
      return { meta: { changes: c.rowsWritten } };
    },
  });
  return { prepare: (q) => stmt(q), batch: async (list) => { const out = []; for (const s of list) out.push(await s.run()); return out; } };
}

/**
 * All state (jobs, encrypted settings, login throttling, studio session) lives in ONE SQLite-backed Durable Object -
 * free on the Workers Free plan and needs no separate database or extra API-token permission. One owner => one instance.
 */
export class NexusStore extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.db = sqlDb(ctx.storage.sql);
  }

  get env2() {
    return { ...this.env, DB: this.db };
  }

  async fetch(request) {
    return app(request, this.env2);
  }

  async cron() {
    await ensureSchema(this.db);
    const jobsResult = await jobs.refreshActive(this.env2);
    const studioResult = await studio.watchdog(this.env2).catch((e) => ({ error: e.message }));
    return { jobs: jobsResult, studio: studioResult };
  }

  async route() {
    await ensureSchema(this.db);
    return studio.route(this.env2);
  }

  async touch() {
    await ensureSchema(this.db);
    return studio.touch(this.env2);
  }
}

const store = (env) => env.STORE.get(env.STORE.idFromName("owner"));

// Per-isolate cache of where the studio is, so page loads do not hit the Durable Object on every request.
let routeCache = { at: 0, rt: null };
let lastTouch = 0;

function redirect(to) {
  return new Response(null, { status: 302, headers: { Location: to, ...SECURITY_HEADERS } });
}

function sameOrigin(request) {
  if (["GET", "HEAD", "OPTIONS"].includes(request.method)) return true;
  const o = request.headers.get("Origin");
  if (o) return o === new URL(request.url).origin;
  const site = request.headers.get("Sec-Fetch-Site");
  return !site || site === "same-origin" || site === "none";
}

function wantsHtml(request) {
  return request.method === "GET" && (request.headers.get("Accept") || "").includes("text/html");
}

/** Everything outside /panel is the full NEXUS app running in the Kaggle studio session. */
async function gateway(request, env, ctx) {
  if (!(await sec.hasSession(env, request))) return wantsHtml(request) ? redirect("/panel/") : err("سجل دخول الأول", 401);
  if (!sameOrigin(request)) return err("cross-origin request refused", 403);
  const now = Date.now();
  if (!routeCache.rt || now - routeCache.at > 20000) routeCache = { at: now, rt: await store(env).route() };
  const rt = routeCache.rt;
  if (!rt) return wantsHtml(request) ? redirect("/panel/?studio=off") : err("الاستوديو مش شغال. شغّله من لوحة التحكم /panel", 503);
  const res = await studio.proxy(request, rt);
  if (!res) {
    routeCache = { at: 0, rt: null };
    return wantsHtml(request) ? redirect("/panel/?studio=lost") : err("الاستوديو مش بيرد", 502);
  }
  if (now - lastTouch > 60000) {
    lastTouch = now;
    ctx.waitUntil(store(env).touch());
  }
  return res;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const p = url.pathname;
    if (p.startsWith("/panel/api/")) return store(env).fetch(request);
    if (p === "/panel") return redirect("/panel/");
    if (p.startsWith("/panel/")) return env.ASSETS.fetch(request);
    return gateway(request, env, ctx);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(store(env).cron().then((r) => console.log("cron", JSON.stringify(r))));
  },
};

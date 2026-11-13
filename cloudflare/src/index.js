// NEXUS Cloud - control plane on Cloudflare Workers (free plan). UI in ./public, API under /api (served by the
// NexusStore Durable Object, which holds all state in its SQLite storage), cron polls Kaggle.
import { DurableObject } from "cloudflare:workers";
import * as sec from "./security.js";
import * as kaggle from "./kaggle.js";
import * as jobs from "./jobs.js";
import { WORKER_VERSION } from "./worker_script.generated.js";
import { ensureSchema } from "./schema.js";

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
  const path = url.pathname;
  const m = request.method;

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
 * All state (jobs, encrypted settings, login throttling) lives in ONE SQLite-backed Durable Object - free on the
 * Workers Free plan and needs no separate database or extra API-token permission. One owner => one instance.
 */
export class NexusStore extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.db = sqlDb(ctx.storage.sql);
  }

  async fetch(request) {
    return app(request, { ...this.env, DB: this.db });
  }

  async cron() {
    const env = { ...this.env, DB: this.db };
    await ensureSchema(this.db);
    return jobs.refreshActive(env);
  }
}

const store = (env) => env.STORE.get(env.STORE.idFromName("owner"));

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return store(env).fetch(request);
    return env.ASSETS.fetch(request);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(store(env).cron().then((r) => console.log("cron refresh", JSON.stringify(r))));
  },
};

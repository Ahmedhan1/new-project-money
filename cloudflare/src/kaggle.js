// Kaggle public API (the same endpoints the official kaggle CLI 2.x / kagglesdk call), from a Cloudflare Worker.
//
//   POST https://api.kaggle.com/v1/kernels.KernelsApiService/<Method>   JSON body, camelCase fields
//   SaveKernel                      push a private script kernel and start a run ("Save & Run All")
//   GetKernelSessionStatus          QUEUED | RUNNING | COMPLETE | ERROR | CANCEL_* (+ failureMessage)
//   ListKernelSessionOutput         files of /kaggle/working of the latest run ({fileName, url}) + log
//   GetAcceleratorQuotaStatistics   weekly GPU/TPU quota: used / allowed (real numbers from Kaggle)
//   security.OAuthService/IntrospectToken   username of a KGAT_ API token
//
// Policy (same as nexus/cloud/compute/kaggle.py): exactly one Kaggle account, kernels are always private, no paid option
// exists or is ever set. Credentials: a new-style API token (KGAT_..., Bearer) or a legacy kaggle.json username + key
// (HTTP Basic). They are only ever sent to api.kaggle.com, and never logged or returned to the browser.

import { getSecret } from "./security.js";

export const API = "https://api.kaggle.com/v1";
export const GPU_SHAPE = "NvidiaTeslaT4"; // free "GPU T4 x2"
export const SESSION_MAX_S = 12 * 3600;

export class KaggleError extends Error {
  constructor(message, { status = 0, quota = false, auth = false } = {}) {
    super(message);
    this.status = status;
    this.quota = quota;
    this.auth = auth;
  }
}

const QUOTA_RE = /quota|exceeded|usage limit|limit (?:has been )?reached|too many (?:requests|sessions)|maximum (?:number of )?(?:concurrent|weekly|gpu)|no (?:more )?gpu/i;

/** Parse what the owner pasted: a KGAT_ token, a legacy key, or the whole kaggle.json. */
export function parseCredentials(token, username) {
  let t = String(token || "").trim();
  let u = String(username || "").trim();
  if (t.startsWith("{")) {
    try {
      const j = JSON.parse(t);
      t = String(j.key || j.token || "").trim();
      u = u || String(j.username || "").trim();
    } catch {
      throw new KaggleError("kaggle.json غير صالح");
    }
  }
  if (!t) throw new KaggleError("المفتاح فاضي");
  if (!/^[\x21-\x7e]{8,512}$/.test(t)) throw new KaggleError("المفتاح فيه حروف غير مسموحة");
  if (u && !/^[A-Za-z0-9_-]{1,64}$/.test(u)) throw new KaggleError("اسم المستخدم غير صالح");
  const kind = t.startsWith("KGAT_") || !u ? "bearer" : "basic";
  return { token: t, username: u, kind };
}

/** Credentials in force: Cloudflare secrets win over the token saved from the Settings page. */
export async function loadCredentials(env) {
  if (env.KAGGLE_API_TOKEN) {
    return { ...parseCredentials(env.KAGGLE_API_TOKEN, env.KAGGLE_USERNAME), source: "cloudflare_secret" };
  }
  const saved = await getSecret(env, "kaggle");
  if (!saved) return null;
  if (saved.undecryptable) return { undecryptable: true, source: "settings" };
  return { ...saved, source: "settings" };
}

function authHeader(c) {
  return c.kind === "basic" ? `Basic ${btoa(`${c.username}:${c.token}`)}` : `Bearer ${c.token}`;
}

export function hint(token) {
  const t = String(token || "");
  return t.length > 10 ? `${t.slice(0, 5)}…${t.slice(-4)}` : "…";
}

export async function call(creds, service, method, body = {}, fetchImpl = fetch) {
  let r;
  try {
    r = await fetchImpl(`${API}/${service}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json", Authorization: authHeader(creds), "User-Agent": "nexus-cloud/1.0" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new KaggleError(`تعذر الاتصال بـ Kaggle: ${e.message}`);
  }
  const text = await r.text();
  let j = null;
  try {
    j = text ? JSON.parse(text) : {};
  } catch {
    j = null;
  }
  const code = j && typeof j.code === "number" ? j.code : 0;
  if (!r.ok || code >= 400) {
    const e = j && (j.message || j.error);
    const raw = typeof e === "object" && e ? e.message || JSON.stringify(e) : e;
    const msg = String(raw || text || `HTTP ${r.status}`).replaceAll(creds.token, "***").slice(0, 400);
    const status = code || r.status;
    throw new KaggleError(`Kaggle ${method}: ${msg}`, { status, quota: status === 429 || QUOTA_RE.test(msg), auth: status === 401 || status === 403 });
  }
  if (j === null) throw new KaggleError(`Kaggle ${method}: unexpected non-JSON reply (HTTP ${r.status})`);
  return j;
}

const kernels = (creds, method, body, f) => call(creds, "kernels.KernelsApiService", method, body, f);

export async function introspectUsername(creds, fetchImpl = fetch) {
  const j = await call(creds, "security.OAuthService", "IntrospectToken", { token: creds.token }, fetchImpl);
  if (!j.active || !j.username) throw new KaggleError("Kaggle: التوكن غير نشط أو منتهي", { auth: true });
  return j.username;
}

/** "20141.994s" -> 20141.994 */
export function seconds(v) {
  const n = parseFloat(String(v ?? "0").replace(/s$/, ""));
  return Number.isFinite(n) ? n : 0;
}

export async function quota(creds, fetchImpl = fetch) {
  const j = await kernels(creds, "GetAcceleratorQuotaStatistics", {}, fetchImpl);
  const g = j.gpuQuota || {};
  const used = seconds(g.timeUsed), reserved = seconds(g.timeReserved), allowed = seconds(g.totalTimeAllowed);
  return { gpu_used_s: used, gpu_reserved_s: reserved, gpu_allowed_s: allowed, gpu_left_s: Math.max(0, allowed - used - reserved), refresh: j.quotaRefreshTime || null };
}

export function splitRef(ref) {
  const [userName, kernelSlug] = String(ref).split("/");
  return { userName, kernelSlug };
}

/** "/code/user/slug" or "user/slug" -> "user/slug" */
export function cleanRef(ref) {
  return String(ref || "").replace(/^https?:\/\/www\.kaggle\.com/, "").replace(/^\/?code\//, "").replace(/^\//, "");
}

export async function push(creds, { slug, script, gpu, timeoutS }, fetchImpl = fetch) {
  // No owner prefix needed: Kaggle creates the kernel under the token's account and returns its ref (verified live).
  const body = {
    ...(creds.username ? { slug: `${creds.username}/${slug}` } : {}), newTitle: slug, text: script, language: "python", kernelType: "script",
    isPrivate: true, enableGpu: !!gpu, enableTpu: false, enableInternet: true,
    datasetDataSources: [], competitionDataSources: [], kernelDataSources: [], modelDataSources: [], categoryIds: [],
    sessionTimeoutSeconds: Math.max(60, Math.min(SESSION_MAX_S, Math.floor(timeoutS))),
  };
  if (gpu) body.machineShape = GPU_SHAPE;
  const j = await kernels(creds, "SaveKernel", body, fetchImpl);
  if (j.error) throw new KaggleError(`Kaggle push: ${j.error}`, { quota: QUOTA_RE.test(j.error) });
  const ref = cleanRef(j.ref || j.url || (creds.username ? `${creds.username}/${slug}` : ""));
  if (!/^[^/]+\/[^/]+$/.test(ref)) throw new KaggleError(`Kaggle push: unexpected reply ${JSON.stringify(j).slice(0, 200)}`);
  return { ref, url: j.url || `https://www.kaggle.com/code/${ref}`, version: j.versionNumber ?? null };
}

/** Kaggle status -> queued | running | complete | error | cancelled | unknown */
export function normaliseStatus(s) {
  const w = String(s || "").toLowerCase();
  for (const [k, v] of [["complete", "complete"], ["error", "error"], ["cancel", "cancelled"], ["running", "running"], ["queued", "queued"], ["new_script", "queued"]]) {
    if (w.includes(k)) return v;
  }
  return "unknown";
}

export async function status(creds, ref, fetchImpl = fetch) {
  const j = await kernels(creds, "GetKernelSessionStatus", splitRef(ref), fetchImpl);
  return { state: normaliseStatus(j.status), raw: j.status ?? null, failure: j.failureMessage || "" };
}

export async function outputs(creds, ref, fetchImpl = fetch) {
  const files = [];
  let log = "", pageToken = undefined;
  for (let page = 0; page < 10; page++) {
    const j = await kernels(creds, "ListKernelSessionOutput", { ...splitRef(ref), pageSize: 200, ...(pageToken ? { pageToken } : {}) }, fetchImpl);
    for (const f of j.files || []) files.push({ fileName: f.fileName, url: f.url });
    if (j.log && !log) log = j.log;
    if (!j.nextPageToken) break;
    pageToken = j.nextPageToken;
  }
  return { files, log };
}

/** Kaggle logs are a JSON array of {stream_name, time, data}; return plain text. */
export function logText(raw) {
  if (!raw) return "";
  try {
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) return arr.map((x) => x.data || "").join("");
  } catch { /* plain text */ }
  return String(raw);
}

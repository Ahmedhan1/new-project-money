// Owner authentication, sessions, CSRF and the settings vault - Web Crypto only, no dependencies.
//
// * One owner. The password is the Cloudflare secret OWNER_PASSWORD (never stored in the database, never sent anywhere else).
// * Session cookie: "<expiry>.<HMAC-SHA256(expiry)>" with a key derived (HKDF) from OWNER_PASSWORD, so changing the
//   password logs every session out. HttpOnly, Secure, SameSite=Strict.
// * CSRF: every state-changing /api call must carry the header "X-Nexus: 1" (a cross-site form cannot set it) and,
//   when the browser sends Origin, it must be this host.
// * Vault: secrets saved from the Settings page (the Kaggle API token) are AES-256-GCM encrypted in the Durable Object's SQLite with a key
//   derived from OWNER_PASSWORD + a random per-install salt. The stored rows alone never reveal them.

const enc = new TextEncoder();
const dec = new TextDecoder();
export const SESSION_COOKIE = "nx_s";
const SESSION_TTL_S = 7 * 24 * 3600;
export const MIN_PASSWORD_LEN = 12;
const MAX_FAILURES = 10;
const FAILURE_WINDOW_S = 15 * 60;

export function b64url(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function fromB64url(s) {
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

export function b64utf8(text) {
  let s = "";
  for (const b of enc.encode(text)) s += String.fromCharCode(b);
  return btoa(s);
}

async function hkdf(secret, salt, info, usages, algo) {
  const base = await crypto.subtle.importKey("raw", enc.encode(secret), "HKDF", false, ["deriveKey"]);
  return crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt, info: enc.encode(info) }, base, algo, false, usages);
}

function sessionKey(env) {
  return hkdf(env.OWNER_PASSWORD, enc.encode("nexus-cloud/session/v1"), "session", ["sign", "verify"], { name: "HMAC", hash: "SHA-256", length: 256 });
}

export function ownerConfigured(env) {
  return typeof env.OWNER_PASSWORD === "string" && env.OWNER_PASSWORD.length >= MIN_PASSWORD_LEN;
}

export async function passwordMatches(env, candidate) {
  // Constant-time: compare HMACs of both values under a random key.
  const k = await crypto.subtle.importKey("raw", crypto.getRandomValues(new Uint8Array(32)), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const [a, b] = await Promise.all([crypto.subtle.sign("HMAC", k, enc.encode(String(candidate))), crypto.subtle.sign("HMAC", k, enc.encode(env.OWNER_PASSWORD))]);
  const x = new Uint8Array(a), y = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

export async function issueSession(env, now = Date.now()) {
  const exp = String(Math.floor(now / 1000) + SESSION_TTL_S);
  const sig = await crypto.subtle.sign("HMAC", await sessionKey(env), enc.encode(exp));
  const value = `${exp}.${b64url(sig)}`;
  return `${SESSION_COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_TTL_S}`;
}

export function clearSession() {
  return `${SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`;
}

function cookie(request, name) {
  const raw = request.headers.get("Cookie") || "";
  for (const part of raw.split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return null;
}

export async function hasSession(env, request, now = Date.now()) {
  if (!ownerConfigured(env)) return false;
  const v = cookie(request, SESSION_COOKIE);
  if (!v) return false;
  const [exp, sig] = v.split(".");
  if (!/^\d+$/.test(exp || "") || !sig || Number(exp) * 1000 < now) return false;
  try {
    return await crypto.subtle.verify("HMAC", await sessionKey(env), fromB64url(sig), enc.encode(exp));
  } catch {
    return false;
  }
}

export function csrfOk(request) {
  if (["GET", "HEAD", "OPTIONS"].includes(request.method)) return true;
  if (request.headers.get("X-Nexus") !== "1") return false;
  const origin = request.headers.get("Origin");
  return !origin || origin === new URL(request.url).origin;
}

export function clientIp(request) {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

export async function tooManyFailures(db, ip, now = Date.now()) {
  const since = Math.floor(now / 1000) - FAILURE_WINDOW_S;
  const row = await db.prepare("SELECT COUNT(*) AS n FROM login_failures WHERE ip = ? AND ts > ?").bind(ip, since).first();
  return (row?.n || 0) >= MAX_FAILURES;
}

export async function recordFailure(db, ip, now = Date.now()) {
  const t = Math.floor(now / 1000);
  await db.batch([
    db.prepare("INSERT INTO login_failures (ip, ts) VALUES (?, ?)").bind(ip, t),
    db.prepare("DELETE FROM login_failures WHERE ts < ?").bind(t - FAILURE_WINDOW_S),
  ]);
}

// ---------------------------------------------------------------- vault (encrypted settings)
async function vaultKey(env) {
  let salt = await getSetting(env.DB, "vault_salt");
  if (!salt) {
    salt = b64url(crypto.getRandomValues(new Uint8Array(16)));
    await env.DB.prepare("INSERT OR IGNORE INTO settings (k, v, updated) VALUES ('vault_salt', ?, ?)").bind(salt, Date.now()).run();
    salt = await getSetting(env.DB, "vault_salt");
  }
  return hkdf(env.OWNER_PASSWORD, fromB64url(salt), "vault", ["encrypt", "decrypt"], { name: "AES-GCM", length: 256 });
}

export async function getSetting(db, k) {
  const row = await db.prepare("SELECT v FROM settings WHERE k = ?").bind(k).first();
  return row ? row.v : null;
}

export async function setSetting(db, k, v) {
  await db.prepare("INSERT INTO settings (k, v, updated) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated = excluded.updated")
    .bind(k, v, Date.now()).run();
}

export async function deleteSetting(db, k) {
  await db.prepare("DELETE FROM settings WHERE k = ?").bind(k).run();
}

export async function putSecret(env, name, value) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: enc.encode(name) }, await vaultKey(env), enc.encode(JSON.stringify(value)));
  await setSetting(env.DB, `secret:${name}`, `v1.${b64url(iv)}.${b64url(ct)}`);
}

export async function getSecret(env, name) {
  const raw = await getSetting(env.DB, `secret:${name}`);
  if (!raw) return null;
  const [ver, iv, ct] = raw.split(".");
  if (ver !== "v1") return null;
  try {
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: fromB64url(iv), additionalData: enc.encode(name) }, await vaultKey(env), fromB64url(ct));
    return JSON.parse(dec.decode(pt));
  } catch {
    return { undecryptable: true }; // OWNER_PASSWORD changed since it was saved: the owner must enter it again
  }
}

// YouTube channel connection for the cloud studio: Google OAuth 2.0 web-server flow with PKCE, run by this Worker.
//
// The owner creates their OWN Google Cloud OAuth client (type "Web application", free, no card) with the redirect URI
// <origin>/panel/api/youtube/callback, pastes its ID/secret in Settings, and presses "Connect". The refresh token is
// stored AES-GCM encrypted (vault "youtube"). A studio session fetches it at start-up over its authenticated callback
// channel and passes it to the NEXUS app as NEXUS_PUB_YOUTUBE_MAIN_* environment variables (nexus/intelligence/
// publishing/creds.py reads those), so it never appears in a notebook's source or saved output.

import { b64url, getSecret, putSecret, deleteSetting, getSetting, setSetting } from "./security.js";
import { JobError } from "./jobs.js";

const AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_URL = "https://oauth2.googleapis.com/token";
export const SCOPES = [
  "https://www.googleapis.com/auth/youtube.upload",
  "https://www.googleapis.com/auth/youtube.readonly",
  "https://www.googleapis.com/auth/yt-analytics.readonly",
];

export const redirectUri = (origin) => `${origin}/panel/api/youtube/callback`;

export async function status(env, origin) {
  const c = await getSecret(env, "youtube_client");
  const y = await getSecret(env, "youtube");
  return {
    redirect_uri: redirectUri(origin),
    client_configured: !!(c && c.client_id && !c.undecryptable),
    client_id_hint: c?.client_id ? `${c.client_id.slice(0, 12)}…` : null,
    connected: !!(y && y.refresh_token && !y.undecryptable),
    channel: y?.channel || null,
    connected_at: y?.connected_at || null,
  };
}

export async function saveClient(env, body) {
  const client_id = String(body.client_id || "").trim();
  const client_secret = String(body.client_secret || "").trim();
  if (!/^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$/.test(client_id)) throw new JobError("Client ID شكله غلط (لازم ينتهي بـ .apps.googleusercontent.com)");
  if (!/^[\x21-\x7e]{10,200}$/.test(client_secret)) throw new JobError("Client secret شكله غلط");
  await putSecret(env, "youtube_client", { client_id, client_secret });
}

export async function disconnect(env) {
  await deleteSetting(env.DB, "secret:youtube");
}

async function sha256b64url(text) {
  return b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

/** Owner pressed "Connect": remember state + PKCE verifier (10 min) and send the browser to Google. */
export async function begin(env, origin) {
  const c = await getSecret(env, "youtube_client");
  if (!c?.client_id || c.undecryptable) throw new JobError("حط Client ID و Client secret الأول", 409);
  const state = b64url(crypto.getRandomValues(new Uint8Array(24)));
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)));
  await setSetting(env.DB, "youtube_oauth", JSON.stringify({ state, verifier, exp: Date.now() + 10 * 60 * 1000 }));
  const q = new URLSearchParams({
    client_id: c.client_id, redirect_uri: redirectUri(origin), response_type: "code", scope: SCOPES.join(" "),
    access_type: "offline", prompt: "consent", include_granted_scopes: "true", state,
    code_challenge: await sha256b64url(verifier), code_challenge_method: "S256",
  });
  return `${AUTH_URL}?${q}`;
}

/** Google redirected back. Authenticated by the one-time state (the SameSite=Strict cookie is not sent here). */
export async function finish(env, origin, params, fetchImpl = fetch) {
  const raw = await getSetting(env.DB, "youtube_oauth");
  await deleteSetting(env.DB, "youtube_oauth"); // one use only
  let saved = null;
  try { saved = JSON.parse(raw || "null"); } catch { /* ignore */ }
  const state = params.get("state") || "";
  if (!saved || saved.exp < Date.now() || saved.state.length !== state.length || saved.state !== state) throw new JobError("طلب الربط انتهى أو غير صالح. جرب تاني.", 400);
  if (params.get("error")) throw new JobError(`Google رفض: ${params.get("error")}`, 400);
  const code = params.get("code");
  if (!code) throw new JobError("Google مرجعش code", 400);
  const c = await getSecret(env, "youtube_client");
  const r = await fetchImpl(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ code, client_id: c.client_id, client_secret: c.client_secret, redirect_uri: redirectUri(origin),
      grant_type: "authorization_code", code_verifier: saved.verifier }),
  });
  const tok = await r.json().catch(() => ({}));
  if (!r.ok || !tok.refresh_token) throw new JobError(`Google مرجعش refresh token: ${tok.error_description || tok.error || r.status}`, 400);
  let channel = null;
  try {
    const ch = await fetchImpl("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true", { headers: { Authorization: `Bearer ${tok.access_token}` } });
    const j = await ch.json();
    if (j.items?.[0]) channel = { id: j.items[0].id, title: j.items[0].snippet?.title || "" };
  } catch { /* the name is cosmetic */ }
  await putSecret(env, "youtube", { client_id: c.client_id, client_secret: c.client_secret, refresh_token: tok.refresh_token,
    scopes: String(tok.scope || "").split(" ").filter(Boolean), channel, connected_at: Date.now() });
  return channel;
}

/** What a live studio session receives (env vars for the NEXUS app). */
export async function sessionEnv(env) {
  const y = await getSecret(env, "youtube");
  if (!y?.refresh_token || y.undecryptable) return {};
  return {
    NEXUS_PUB_YOUTUBE_MAIN_CLIENT_ID: y.client_id,
    NEXUS_PUB_YOUTUBE_MAIN_CLIENT_SECRET: y.client_secret,
    NEXUS_PUB_YOUTUBE_MAIN_REFRESH_TOKEN: y.refresh_token,
  };
}

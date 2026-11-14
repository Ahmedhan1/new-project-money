import test from "node:test";
import assert from "node:assert/strict";
import { makeD1 } from "./d1shim.js";
import * as sec from "../src/security.js";
import * as youtube from "../src/youtube.js";
import * as studio from "../src/studio.js";
import { ensureSchema } from "../src/schema.js";

const ORIGIN = "https://nexus.example.workers.dev";
async function env() {
  const e = { OWNER_PASSWORD: "correct-horse-battery-staple", DB: makeD1() };
  await ensureSchema(e.DB);
  return e;
}

test("youtube: client validation, PKCE begin, one-time state, token stored encrypted, session env", async () => {
  const e = await env();
  await assert.rejects(youtube.begin(e, ORIGIN), /Client ID/);
  await assert.rejects(youtube.saveClient(e, { client_id: "nope", client_secret: "x" }), /Client ID/);
  await youtube.saveClient(e, { client_id: "123-abc.apps.googleusercontent.com", client_secret: "GOCSPX-secretvalue" });
  const url = new URL(await youtube.begin(e, ORIGIN));
  assert.equal(url.searchParams.get("redirect_uri"), `${ORIGIN}/panel/api/youtube/callback`);
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  assert.equal(url.searchParams.get("access_type"), "offline");
  const state = url.searchParams.get("state");

  let tokenBody;
  const f = async (u, init) => {
    if (String(u).includes("oauth2.googleapis.com/token")) {
      tokenBody = new URLSearchParams(init.body);
      return new Response(JSON.stringify({ access_token: "at", refresh_token: "1//refresh", scope: youtube.SCOPES.join(" ") }), { status: 200 });
    }
    return new Response(JSON.stringify({ items: [{ id: "UC1", snippet: { title: "قناتي" } }] }), { status: 200 });
  };
  await assert.rejects(youtube.finish(e, ORIGIN, new URLSearchParams({ state: "forged", code: "c" }), f), /انتهى/);
  // the forged attempt consumed the pending state: start again
  const state2 = new URL(await youtube.begin(e, ORIGIN)).searchParams.get("state");
  assert.notEqual(state2, state);
  const ch = await youtube.finish(e, ORIGIN, new URLSearchParams({ state: state2, code: "c" }), f);
  assert.equal(ch.title, "قناتي");
  assert.ok(tokenBody.get("code_verifier").length > 40);
  await assert.rejects(youtube.finish(e, ORIGIN, new URLSearchParams({ state: state2, code: "c" }), f), /انتهى/); // replay
  const raw = await sec.getSetting(e.DB, "secret:youtube");
  assert.ok(!raw.includes("refresh"));
  const st = await youtube.status(e, ORIGIN);
  assert.equal(st.connected, true);
  assert.deepEqual(await youtube.sessionEnv(e), {
    NEXUS_PUB_YOUTUBE_MAIN_CLIENT_ID: "123-abc.apps.googleusercontent.com",
    NEXUS_PUB_YOUTUBE_MAIN_CLIENT_SECRET: "GOCSPX-secretvalue",
    NEXUS_PUB_YOUTUBE_MAIN_REFRESH_TOKEN: "1//refresh",
  });
});

test("studio secrets endpoint: only the live session's secret gets them", async () => {
  const e = await env();
  const SESSION_KEY = ["sekret", "value"].join("-");
  await e.DB.prepare("UPDATE studio SET session='s1', secret=?, state='installing'").bind(SESSION_KEY).run();
  const yt = async () => ({ NEXUS_PUB_YOUTUBE_MAIN_REFRESH_TOKEN: "r" });
  await assert.rejects(studio.secrets(e, { secret: `${SESSION_KEY}x`, session: "s1" }, yt), /forbidden/);
  assert.deepEqual(await studio.secrets(e, { secret: SESSION_KEY, session: "s1" }, yt), { env: { NEXUS_PUB_YOUTUBE_MAIN_REFRESH_TOKEN: "r" } });
  await e.DB.prepare("UPDATE studio SET state='stopped'").run();
  await assert.rejects(studio.secrets(e, { secret: SESSION_KEY, session: "s1" }, yt), /forbidden/);
});

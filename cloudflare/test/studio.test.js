import test from "node:test";
import assert from "node:assert/strict";
import { makeD1 } from "./d1shim.js";
import * as sec from "../src/security.js";
import * as studio from "../src/studio.js";
import { app } from "../src/index.js";
import { ensureSchema } from "../src/schema.js";

const PW = "correct-horse-battery-staple";
async function env() {
  const e = { OWNER_PASSWORD: PW, DB: makeD1(), MIN_GPU_RESERVE_S: "3600" };
  await ensureSchema(e.DB);
  await sec.putSecret(e, "kaggle", { token: "KGAT_tok_for_tests", username: "", kind: "bearer" });
  return e;
}
function fakeKaggle(calls, quotaLeft = 100000) {
  return async (url, init) => {
    const u = String(url);
    const body = init?.body ? JSON.parse(init.body) : null;
    calls.push({ u, body });
    const ok = (j) => new Response(JSON.stringify(j), { status: 200, headers: { "Content-Type": "application/json" } });
    if (u.endsWith("/GetAcceleratorQuotaStatistics")) return ok({ gpuQuota: { timeUsed: `${108000 - quotaLeft}s`, totalTimeAllowed: "108000s" } });
    if (u.endsWith("/SaveKernel")) return ok({ ref: `/code/owner/${body.newTitle}`, url: `https://www.kaggle.com/code/owner/${body.newTitle}`, versionNumber: 1 });
    return new Response("nope", { status: 404 });
  };
}
const cfgOf = (text) => JSON.parse(Buffer.from(text.match(/_CONFIG_B64 = "([^"]+)"/)[1], "base64").toString());

test("studio: start pushes the full-app kernel, heartbeat auth, ready url, idle stop, save + restore", async () => {
  const e = await env();
  const calls = [];
  const real = globalThis.fetch;
  globalThis.fetch = fakeKaggle(calls);
  try {
    const s = await studio.start(e, { origin: "https://nexus.example.workers.dev", gpu: true, hours: 6, idle_min: 30 });
    assert.equal(s.state, "starting");
    const push = calls.find((c) => c.u.endsWith("/SaveKernel")).body;
    assert.equal(push.isPrivate, true);
    assert.equal(push.enableInternet, true);
    assert.equal(push.newTitle, "nexus-studio-a");
    assert.deepEqual(push.kernelDataSources, []);
    const cfg = cfgOf(push.text);
    assert.equal(cfg.callback, "https://nexus.example.workers.dev/panel/api/studio/callback");
    assert.match(cfg.repo, /^https:\/\/github\.com\//);
    await assert.rejects(studio.start(e, { origin: "https://x.dev" }), /شغال بالفعل/);

    await assert.rejects(studio.callback(e, { secret: "wrong", session: cfg.session, state: "ready" }), /forbidden/);
    await assert.rejects(studio.callback(e, { secret: cfg.secret, session: cfg.session, state: "ready", url: "https://evil.example.com" }), /bad tunnel/);
    const r1 = await studio.callback(e, { secret: cfg.secret, session: cfg.session, state: "ready", url: "https://abc-def.trycloudflare.com", log: ["x"] });
    assert.equal(r1.stop, false);
    assert.deepEqual(await studio.route(e), { url: "https://abc-def.trycloudflare.com", token: cfg.token });

    await e.DB.prepare("UPDATE studio SET last_activity = ? WHERE id = 1").bind(Date.now() - 31 * 60 * 1000).run();
    const r2 = await studio.callback(e, { secret: cfg.secret, session: cfg.session, state: "ready", url: "https://abc-def.trycloudflare.com" });
    assert.deepEqual([r2.stop, r2.reason], [true, "idle"]);
    await studio.callback(e, { secret: cfg.secret, session: cfg.session, state: "stopped", saved: true });
    assert.equal(await studio.route(e), null);
    assert.equal((await studio.status(e)).has_saved_work, true);

    const s2 = await studio.start(e, { origin: "https://nexus.example.workers.dev", gpu: false });
    assert.equal(s2.state, "starting");
    const push2 = calls.filter((c) => c.u.endsWith("/SaveKernel")).at(-1).body;
    assert.equal(push2.newTitle, "nexus-studio-b");
    assert.deepEqual(push2.kernelDataSources, ["owner/nexus-studio-a"]);
    assert.equal(push2.enableGpu, false);
    assert.equal(cfgOf(push2.text).restore_from, "owner/nexus-studio-a");
    const cfg2 = cfgOf(push2.text);
    assert.notEqual(cfg2.token, cfg.token);
    await assert.rejects(studio.callback(e, { secret: cfg.secret, session: cfg.session, state: "ready" }), /forbidden/); // old session is dead
    await studio.callback(e, { secret: cfgOf(push2.text).secret, session: cfgOf(push2.text).session, state: "error", error: "x", saved: false });
    assert.equal((await e.DB.prepare("SELECT saved_ref FROM studio").first()).saved_ref, "owner/nexus-studio-a"); // failed session keeps the old save
    await e.DB.prepare("UPDATE studio SET state = 'installing'").run();
    await studio.stop(e);
    const r3 = await studio.callback(e, { secret: cfg2.secret, session: cfg2.session, state: "installing" });
    assert.deepEqual([r3.stop, r3.reason], [true, "owner"]);
  } finally {
    globalThis.fetch = real;
  }
});

test("studio callback route needs no cookie but the owner endpoints do", async () => {
  const e = await env();
  const r = await app(new Request("https://n.dev/panel/api/studio/callback", { method: "POST", body: JSON.stringify({ secret: "x", session: "y", state: "ready" }) }), e);
  assert.equal(r.status, 403);
  const r2 = await app(new Request("https://n.dev/panel/api/studio/start", { method: "POST", headers: { "X-Nexus": "1" }, body: "{}" }), e);
  assert.equal(r2.status, 401);
});

test("gateway proxy: adds the bearer, strips cookies, rewrites tunnel redirects", async () => {
  let seen;
  const real = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen = { url: String(url), headers: init.headers };
    return new Response("ok", { status: 302, headers: { Location: "https://abc.trycloudflare.com/studio", "Set-Cookie": "a=b" } });
  };
  try {
    const req = new Request("https://n.dev/api/projects?x=1", { headers: { Cookie: "nx_s=secret", Authorization: "Bearer evil" } });
    const res = await studio.proxy(req, { url: "https://abc.trycloudflare.com", token: "T0K" });
    assert.equal(seen.url, "https://abc.trycloudflare.com/api/projects?x=1");
    assert.equal(seen.headers.get("Authorization"), "Bearer T0K");
    assert.equal(seen.headers.get("Cookie"), null);
    assert.equal(res.headers.get("Location"), "/studio");
    assert.equal(res.headers.get("Set-Cookie"), null);
  } finally {
    globalThis.fetch = real;
  }
});

test("studio: a session stuck in 'starting' for 15 min can be started again", async () => {
  const e = await env();
  const calls = [];
  const real = globalThis.fetch;
  globalThis.fetch = fakeKaggle(calls);
  try {
    await studio.start(e, { origin: "https://n.dev" });
    await assert.rejects(studio.start(e, { origin: "https://n.dev" }), /شغال بالفعل/);
    await e.DB.prepare("UPDATE studio SET last_seen = ? WHERE id = 1").bind(Date.now() - 16 * 60 * 1000).run();
    const s = await studio.start(e, { origin: "https://n.dev" });
    assert.equal(s.state, "starting");
  } finally {
    globalThis.fetch = real;
  }
});

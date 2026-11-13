// Fast tests, no network: node --test test/
import test from "node:test";
import assert from "node:assert/strict";
import { makeD1 } from "./d1shim.js";
import * as sec from "../src/security.js";
import * as kaggle from "../src/kaggle.js";
import * as jobs from "../src/jobs.js";
import { app, sqlDb } from "../src/index.js";
const worker = { fetch: (r, e) => app(r, e) };

const PW = "correct-horse-battery-staple";
const env = () => ({ OWNER_PASSWORD: PW, DB: makeD1(), MAX_RUNTIME_S: "10800", MIN_GPU_RESERVE_S: "3600", ASSETS: { fetch: () => new Response("ui") } });

function req(path, { method = "GET", body, cookie, csrf = true } = {}) {
  const h = { "Content-Type": "application/json", "CF-Connecting-IP": "1.2.3.4" };
  if (csrf) h["X-Nexus"] = "1";
  if (cookie) h.Cookie = cookie;
  return new Request(`https://nexus.example.workers.dev${path}`, { method, headers: h, body: body && JSON.stringify(body) });
}

test("sessions: valid, tampered, expired, other password", async () => {
  const e = env();
  const c = (await sec.issueSession(e)).split(";")[0];
  assert.equal(await sec.hasSession(e, req("/", { cookie: c })), true);
  assert.equal(await sec.hasSession(e, req("/", { cookie: c.slice(0, -2) + "xx" })), false);
  assert.equal(await sec.hasSession({ ...e, OWNER_PASSWORD: "another-password-123" }, req("/", { cookie: c })), false);
  const old = (await sec.issueSession(e, Date.now() - 8 * 86400e3)).split(";")[0];
  assert.equal(await sec.hasSession(e, req("/", { cookie: old })), false);
});

test("short or missing OWNER_PASSWORD disables login", async () => {
  const e = { ...env(), OWNER_PASSWORD: "short" };
  const r = await worker.fetch(req("/api/login", { method: "POST", body: { password: "short" } }), e);
  assert.equal(r.status, 503);
});

test("login, CSRF, lockout", async () => {
  const e = env();
  assert.equal((await worker.fetch(req("/api/login", { method: "POST", body: { password: PW }, csrf: false }), e)).status, 403);
  const ok = await worker.fetch(req("/api/login", { method: "POST", body: { password: PW } }), e);
  assert.equal(ok.status, 200);
  assert.match(ok.headers.get("Set-Cookie"), /HttpOnly; Secure; SameSite=Strict/);
  assert.equal((await worker.fetch(req("/api/jobs"), e)).status, 401);
  for (let i = 0; i < 10; i++) assert.equal((await worker.fetch(req("/api/login", { method: "POST", body: { password: "nope" } }), e)).status, 401);
  assert.equal((await worker.fetch(req("/api/login", { method: "POST", body: { password: PW } }), e)).status, 429);
});

test("vault: encrypted at rest, unreadable after password change", async () => {
  const e = env();
  await sec.putSecret(e, "kaggle", { token: "KGAT_supersecretvalue" });
  const raw = await sec.getSetting(e.DB, "secret:kaggle");
  assert.ok(!raw.includes("supersecret"));
  assert.equal((await sec.getSecret(e, "kaggle")).token, "KGAT_supersecretvalue");
  assert.deepEqual(await sec.getSecret({ ...e, OWNER_PASSWORD: "a-different-password" }, "kaggle"), { undecryptable: true });
});

test("credentials parsing", () => {
  assert.equal(kaggle.parseCredentials("KGAT_abcdefgh123").kind, "bearer");
  const j = kaggle.parseCredentials('{"username":"me_1","key":"0123456789abcdef"}');
  assert.deepEqual([j.username, j.token, j.kind], ["me_1", "0123456789abcdef", "basic"]);
  assert.throws(() => kaggle.parseCredentials(""));
  assert.throws(() => kaggle.parseCredentials("bad token with spaces"));
  assert.equal(kaggle.cleanRef("/code/me/nexus-x"), "me/nexus-x");
  assert.equal(kaggle.normaliseStatus("KernelWorkerStatus.COMPLETE"), "complete");
  assert.equal(kaggle.seconds("20141.994s"), 20141.994);
});

test("worker script embeds the bundle and the real NEXUS worker", () => {
  const s = jobs.buildScript({ job_id: "x", task: "probe", params: { q: "عربي" } });
  const b64 = s.match(/_EMBEDDED_BUNDLE_B64 = "([^"]+)"/)[1];
  assert.deepEqual(JSON.parse(Buffer.from(b64, "base64").toString("utf8")).params, { q: "عربي" });
  assert.ok(jobs.TASKS.includes("probe") && jobs.TASKS.includes("batch_llm"));
});

test("validation: unknown task, params type, runtime cap, GPU forced for GPU-only tasks", () => {
  const e = env();
  assert.throws(() => jobs.validateSubmission({ task: "rm_rf" }, e), /غير معروفة/);
  assert.throws(() => jobs.validateSubmission({ task: "probe", params: [] }, e));
  assert.equal(jobs.validateSubmission({ task: "probe", max_runtime_s: 999999 }, e).max, 43200);
  assert.equal(jobs.validateSubmission({ task: "batch_llm", gpu: false }, e).gpu, true);
});

function fakeKaggle({ status = "COMPLETE", result = { status: "succeeded", duration_s: 1, artifacts: [], checkpoint: null }, quotaLeft = 100000 } = {}) {
  const calls = [];
  const f = async (url, init) => {
    const u = String(url);
    calls.push({ u, body: init?.body ? JSON.parse(init.body) : null, auth: init?.headers?.Authorization });
    const ok = (j) => new Response(JSON.stringify(j), { status: 200, headers: { "Content-Type": "application/json" } });
    if (u.endsWith("/GetAcceleratorQuotaStatistics")) return ok({ gpuQuota: { timeUsed: `${108000 - quotaLeft}s`, totalTimeAllowed: "108000s" } });
    if (u.endsWith("/SaveKernel")) return ok({ ref: `/code/owner/${JSON.parse(init.body).newTitle}`, url: "https://www.kaggle.com/code/owner/k", versionNumber: 1 });
    if (u.endsWith("/GetKernelSessionStatus")) return ok({ status });
    if (u.endsWith("/ListKernelSessionOutput")) return ok({ files: [{ fileName: "result.json", url: "https://www.kaggleusercontent.com/r" }, { fileName: "artifacts/probe.json", url: "https://x/p" }], log: "[]" });
    if (u === "https://www.kaggleusercontent.com/r") return ok(result);
    return new Response("nope", { status: 404 });
  };
  return { f, calls };
}

test("job lifecycle against a fake Kaggle: submit -> poll -> succeeded; retry resumes from checkpoint", async () => {
  const e = env();
  await sec.putSecret(e, "kaggle", { token: "KGAT_tok_for_tests", username: "", kind: "bearer" });
  const fk = fakeKaggle();
  const realFetch = globalThis.fetch;
  globalThis.fetch = fk.f;
  try {
    const j = await jobs.submit(e, { task: "probe", gpu: true });
    assert.equal(j.status, "queued");
    assert.equal(j.kernel_ref, `owner/nexus-${j.id}`);
    const push = fk.calls.find((c) => c.u.endsWith("/SaveKernel"));
    assert.equal(push.body.isPrivate, true);
    assert.equal(push.body.machineShape, "NvidiaTeslaT4");
    assert.equal(push.auth, "Bearer KGAT_tok_for_tests");
    const creds = await jobs.requireCreds(e);
    assert.equal(await jobs.refreshJob(e, creds, await jobs.getJob(e.DB, j.id), Date.now(), fk.f), true);
    const done = jobs.publicJob(await jobs.getJob(e.DB, j.id));
    assert.equal(done.status, "succeeded");
    assert.equal(done.files.length, 2);

    const fk2 = fakeKaggle({ result: { status: "lost", error: "worker lost", checkpoint: { seq: 3, step: "chunk_2" } } });
    globalThis.fetch = fk2.f;
    const j2 = await jobs.submit(e, { task: "chunked_transcode", params: { input_url: "https://x/v.mp4" } });
    await jobs.refreshJob(e, creds, await jobs.getJob(e.DB, j2.id), Date.now(), fk2.f);
    const lost = jobs.publicJob(await jobs.getJob(e.DB, j2.id));
    assert.equal(lost.status, "failed");
    assert.equal(lost.result.has_checkpoint, true);
    const again = await jobs.retry(e, j2.id);
    assert.equal(again.attempt, 2);
    assert.equal(again.kernel_ref, `owner/nexus-${j2.id}-a2`);
    const pushed = fk2.calls.filter((c) => c.u.endsWith("/SaveKernel")).at(-1).body.text;
    const bundle = JSON.parse(Buffer.from(pushed.match(/_EMBEDDED_BUNDLE_B64 = "([^"]+)"/)[1], "base64").toString());
    assert.deepEqual([bundle.attempt, bundle.checkpoint.step], [2, "chunk_2"]);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("GPU quota guard refuses new GPU jobs near the weekly limit", async () => {
  const e = env();
  await sec.putSecret(e, "kaggle", { token: "KGAT_tok_for_tests", username: "", kind: "bearer" });
  const realFetch = globalThis.fetch;
  globalThis.fetch = fakeKaggle({ quotaLeft: 600 }).f;
  try {
    await assert.rejects(jobs.submit(e, { task: "probe", gpu: true }), /رصيد GPU/);
    const cpu = await jobs.submit(e, { task: "probe", gpu: false });
    assert.equal(cpu.status, "queued");
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("Kaggle errors never echo the token", async () => {
  const f = async () => new Response(JSON.stringify({ code: 401, message: "bad token KGAT_secret_value_x" }), { status: 401, headers: { "Content-Type": "application/json" } });
  await assert.rejects(kaggle.quota({ token: "KGAT_secret_value_x", kind: "bearer" }, f), (e) => e.auth && !e.message.includes("KGAT_secret_value_x"));
});

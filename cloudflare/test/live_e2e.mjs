// LIVE check against the real Kaggle API (not part of `npm test`): runs the Worker's job code end to end with a CPU
// `probe` job (no GPU quota used; creates one private kernel). Usage:
//   KAGGLE_API_TOKEN=KGAT_... node test/live_e2e.mjs
import { makeD1 } from "./d1shim.js";
import * as sec from "../src/security.js";
import * as jobs from "../src/jobs.js";
import * as kaggle from "../src/kaggle.js";

const env = { OWNER_PASSWORD: "live-e2e-password-xyz", DB: makeD1(), MAX_RUNTIME_S: "900", MIN_GPU_RESERVE_S: "3600" };
await sec.putSecret(env, "kaggle", kaggle.parseCredentials(process.env.KAGGLE_API_TOKEN || "KGAT_placeholder_injected_by_proxy", process.env.KAGGLE_USERNAME));
const creds = await jobs.requireCreds(env);
console.log("quota", await kaggle.quota(creds));
const j = await jobs.submit(env, { task: "probe", title: "live e2e", gpu: false, max_runtime_s: 900 });
console.log("submitted", j.status, j.kernel_ref, j.error || "");
if (j.status === "failed") process.exit(1);
for (let i = 0; i < 40; i++) {
  await new Promise((r) => setTimeout(r, 15000));
  const row = await jobs.getJob(env.DB, j.id);
  await jobs.refreshJob(env, creds, row);
  const p = jobs.publicJob(await jobs.getJob(env.DB, j.id));
  console.log(new Date().toISOString(), p.status, p.remote_status);
  if (!["submitting", "queued", "running"].includes(p.status)) {
    console.log(JSON.stringify({ status: p.status, error: p.error, files: p.files, accelerator: p.result?.accelerator, duration_s: p.result?.duration_s }, null, 1));
    const url = await jobs.fileUrl(env, j.id, "artifacts/probe.json");
    const probe = await (await fetch(url)).json();
    console.log("probe.json keys:", Object.keys(probe).join(", "));
    process.exit(p.status === "succeeded" ? 0 : 1);
  }
}
process.exit(2);

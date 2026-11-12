"""Remote compute (Account 1, sub-agent B): dispatcher, providers, worker kit, pull protocol. No network, no GPU.

Kaggle is exercised through a fake CLI runner that behaves like `kaggle kernels push/status/output` but executes the
pushed script locally, so the real worker, the real bundle embedding and the real verification code all run.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nexus import db, vault
from nexus.cloud import events, worker_kit
from nexus.cloud.compute import dispatcher as dsp
from nexus.cloud.compute.base import RemoteStatus
from nexus.cloud.compute.kaggle import KaggleProvider
from nexus.cloud.compute.local import LocalProvider
from nexus.contracts import ProductionJobManifest

HAS_FFMPEG = shutil.which("ffmpeg") is not None
FAKE_TOKEN = "KGAT_" + "0123456789abcdef" * 2  # EXAMPLE token-shaped value, never a real one


class FakeKaggleCLI:
    """Mimics the official CLI's observable behaviour. mode: ok | quota | tamper | running | leak."""

    def __init__(self, root: Path, mode: str = "ok"):
        self.root, self.mode, self.calls = root, mode, []

    def __call__(self, args, cwd, timeout):
        self.calls.append(list(args))
        if args[:2] == ["config", "view"]:
            return 0, "Configuration values from /x\n- username: tester\n- auth_method: ACCESS_TOKEN\n", ""
        if args[:2] == ["kernels", "push"]:
            d = Path(args[args.index("-p") + 1])
            meta = json.loads((d / "kernel-metadata.json").read_text())
            self.last_meta, self.last_script = meta, (d / "nexus_job.py").read_text()
            if self.mode == "quota":
                return 1, "", "429 Client Error: You have exceeded your weekly GPU quota"
            if self.mode == "leak":
                return 1, "", f"500 Server Error for token {FAKE_TOKEN}"
            out = self.root / meta["id"].split("/")[1]
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True)
            if self.mode != "running":
                subprocess.run([sys.executable, "-I", str(d / "nexus_job.py"), "--out", str(out)], check=True, capture_output=True)
                if self.mode == "tamper":
                    for a in (out / "artifacts").iterdir():
                        b = a.read_bytes()
                        a.write_bytes(bytes([b[0] ^ 1]) + b[1:])  # same size, different content
            return 0, f"Kernel version 1 successfully pushed.  Please check progress at https://www.kaggle.com/code/{meta['id']}", ""
        if args[:2] == ["kernels", "status"]:
            st = "RUNNING" if self.mode == "running" else "COMPLETE"
            return 0, f'{args[2]} has status "KernelWorkerStatus.{st}"', ""
        if args[:2] == ["kernels", "output"]:
            src = self.root / args[2].split("/")[1]
            dest = Path(args[args.index("-p") + 1])
            shutil.copytree(src, dest, dirs_exist_ok=True)
            (dest / f"{args[2].split('/')[1]}.log").write_text(json.dumps([{"stream_name": "stdout", "time": 1, "data": "worker log line\n"}]))
            return 0, "Output file downloaded", ""
        return 2, "", f"unexpected {args}"


@pytest.fixture()
def env(home, tmp_path):
    fake = FakeKaggleCLI(tmp_path / "kaggle_remote")
    d = dsp.Dispatcher({"kaggle": KaggleProvider(runner=fake), "local": LocalProvider()}, poll_min=0, poll_max=0)
    dsp.set_dispatcher(d)
    yield d, fake
    dsp.set_dispatcher(None)


@pytest.fixture()
def clip(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    p = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def _types(jid):
    return [e["type"] for e in reversed(events.events(job_id=jid))]


def test_worker_kit_builds_single_file_with_embedded_bundle():
    assert {"probe", "ffmpeg_transcode", "chunked_transcode", "torch_bench"} <= set(worker_kit.registered_worker_tasks())
    s = worker_kit.build_script({"job_id": "rjob_x", "task": "probe"})
    assert '_EMBEDDED_BUNDLE_B64 = ""' not in s
    compile(s, "nexus_job.py", "exec")
    assert worker_kit.worker_version().count(".") == 2


def test_probe_end_to_end_through_fake_kaggle(env):
    d, fake = env
    vault.put("kaggle_primary", FAKE_TOKEN)
    row, created = d.submit(ProductionJobManifest(task="probe", requires={"providers": ["kaggle"]}, max_runtime_s=120))
    assert created
    r = d.run_until_done(row["id"], timeout=120, interval=0.05)
    assert r["status"] == "succeeded", r["error"]
    assert fake.last_meta["is_private"] is True and fake.last_meta["enable_gpu"] is True
    assert FAKE_TOKEN not in fake.last_script                       # the token never travels in the kernel
    push = next(c for c in fake.calls if c[:2] == ["kernels", "push"])
    assert "--accelerator" in push and "-t" in push
    arts = dsp.job_artifacts(row["id"])
    assert [a["name"] for a in arts] == ["probe.json"]
    p = dsp.artifact_path(arts[0])
    assert dsp._sha(p) == arts[0]["sha256"]
    t = _types(row["id"])
    assert t[:2] == ["job.submitted", "job.dispatched"] and t[-1] == "job.succeeded"
    cap = d.providers["kaggle"].capability()
    assert cap.measured is True and cap.cpus
    res = json.loads(r["result"])
    assert res["final"]["metrics"]["worker_version"] == worker_kit.worker_version()
    assert res["attempts"][0]["outcome"] == "succeeded"
    assert d.providers["kaggle"].quota().used >= 0


def test_idempotency_key_returns_existing_job(env):
    d, _ = env
    a, c1 = d.submit(ProductionJobManifest(task="probe", idempotency_key="k-1"))
    b, c2 = d.submit(ProductionJobManifest(task="probe", idempotency_key="k-1"))
    assert c1 and not c2 and a["id"] == b["id"]
    assert db.query("SELECT COUNT(*) n FROM remote_jobs")[0]["n"] == 1
    with pytest.raises(ValueError):
        d.submit(ProductionJobManifest(task="not_registered"))


class RecordingProvider(LocalProvider):
    def __init__(self):
        super().__init__("local")
        self.order = []

    def submit(self, job_id, attempt, bundle, gpu, timeout_s):
        self.order.append(bundle["params"]["n"])
        return f"{job_id}-a{attempt}"

    def poll(self, ref):
        return RemoteStatus("running")


def test_priority_order(home):
    rec = RecordingProvider()
    d = dsp.Dispatcher({"local": rec}, poll_min=0)
    for n, prio in ((1, 5), (2, 0), (3, 9), (4, 0)):
        d.submit(ProductionJobManifest(task="probe", params={"n": n}, priority=prio))
    for _ in range(4):  # local runs one at a time: each tick dispatches the most urgent queued job
        d.tick()
        db.execute("UPDATE remote_jobs SET status='succeeded' WHERE status IN ('dispatched','running')")
    assert rec.order == [2, 4, 1, 3]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_chunked_transcode_resumes_from_checkpoint(env, clip):
    d, _ = env
    art = dsp.register_local_file(clip, "source", rights="owned")
    m = ProductionJobManifest(task="chunked_transcode", inputs=[art.artifact_id], requires={"providers": ["local"]},
                              params={"chunks": 4, "encoder": "x264", "simulate_loss_after": 2}, max_retries=2)
    row, _ = d.submit(m)
    r = d.run_until_done(row["id"], timeout=180, interval=0.1)
    assert r["status"] == "succeeded", r["error"]
    res = json.loads(r["result"])
    a1, a2 = res["attempts"][0], res["attempts"][1]
    assert a1["outcome"] == "lost" and a1["metrics"]["chunks_run_this_attempt"] == [0, 1]
    assert a2["outcome"] == "succeeded" and a2["metrics"]["chunks_skipped"] == [0, 1] and a2["metrics"]["chunks_run_this_attempt"] == [2, 3]
    assert [c["seq"] for c in dsp.checkpoints(row["id"])] == [1, 2, 3, 4]
    names = sorted(a["name"] for a in dsp.job_artifacts(row["id"]))
    assert names == ["chunk_000.mp4", "chunk_001.mp4", "chunk_002.mp4", "chunk_003.mp4", "joined.mp4"]
    assert res["postprocess"]["concat"] == "ok"
    t = _types(row["id"])
    assert "worker.lost" in t and "job.retrying" in t and t[-1] == "job.succeeded"


def test_quota_error_pauses_kaggle_and_fails_over_to_local(env):
    d, fake = env
    fake.mode = "quota"
    row, _ = d.submit(ProductionJobManifest(task="probe", max_runtime_s=120))          # gpu "preferred": CPU fallback allowed
    only, _ = d.submit(ProductionJobManifest(task="probe", requires={"gpu": True, "providers": ["kaggle"]}))
    r = d.run_until_done(row["id"], timeout=120, interval=0.05)
    assert r["status"] == "succeeded" and r["provider"] == "local"
    k = d.providers["kaggle"]
    assert k.paused()[0] and k.health().status == "quota_exhausted" and k.quota().exhausted
    assert "quota.exhausted" in _types(row["id"])
    pushes = [c for c in fake.calls if c[:2] == ["kernels", "push"]]
    assert len(pushes) == 1                                                             # paused: not retried, no other account
    d.tick()
    o = dsp._row(only["id"])
    assert o["status"] == "queued" and "waiting" in o["error"]
    with pytest.raises(ValueError):
        KaggleProvider("kaggle_secondary")                                              # no multi-account rotation


def test_cancel_queued_and_running(env):
    d, fake = env
    fake.mode = "running"
    q, _ = d.submit(ProductionJobManifest(task="probe", requires={"providers": ["kaggle"]}))
    q2, _ = d.submit(ProductionJobManifest(task="probe", requires={"providers": ["kaggle"]}))
    d.tick()
    assert dsp._row(q["id"])["status"] in ("dispatched", "running")
    r = d.cancel(q2["id"])
    assert r["status"] == "cancelled"
    r = d.cancel(q["id"])
    assert r["status"] == "cancelled" and "no command to stop" in r["error"]
    n = len([c for c in fake.calls if c[:2] == ["kernels", "push"]])
    d.tick()
    d.tick()
    assert len([c for c in fake.calls if c[:2] == ["kernels", "push"]]) == n            # nothing started after cancel
    assert d.retry(q["id"])["status"] == "queued"


def test_artifact_checksum_mismatch_is_rejected(env):
    d, fake = env
    fake.mode = "tamper"
    row, _ = d.submit(ProductionJobManifest(task="probe", requires={"providers": ["kaggle"]}, max_retries=0))
    r = d.run_until_done(row["id"], timeout=120, interval=0.05)
    assert r["status"] == "failed" and "sha256" in r["error"]
    assert dsp.job_artifacts(row["id"]) == []


def test_secrets_are_redacted_everywhere(env):
    d, fake = env
    vault.put("kaggle_primary", FAKE_TOKEN)
    fake.mode = "leak"
    row, _ = d.submit(ProductionJobManifest(task="probe", requires={"providers": ["kaggle"]}, max_retries=0))
    d.run_until_done(row["id"], timeout=30, interval=0.05)
    blob = json.dumps(dsp._row(row["id"])) + json.dumps(events.events(limit=1000)) + json.dumps(events.notifications())
    assert FAKE_TOKEN not in blob and "[secret:kaggle_primary]" in blob
    from fastapi.testclient import TestClient
    from nexus.api import app
    body = TestClient(app).get(f"/api/v1/compute/jobs/{row['id']}").text
    assert FAKE_TOKEN not in body


def test_pull_worker_protocol(env, monkeypatch):
    from fastapi.testclient import TestClient
    from nexus.api import app
    from nexus.cloud.compute.pull import PullProvider
    d, _ = env
    d.providers["user_gpu"] = PullProvider("user_gpu")
    c = TestClient(app)
    reg = c.post("/api/v1/compute/workers/register", json={"kind": "user_gpu", "label": "my pc"}).json()
    tok = reg["token"]
    assert vault.get(f"worker-{reg['worker_id']}") != tok.split(".", 1)[1]               # only a hash is stored
    assert c.get("/api/v1/compute/workers/next", headers={"Authorization": "Bearer " + tok + "x"}).status_code == 401
    h = {"Authorization": "Bearer " + tok}
    assert c.get("/api/v1/compute/workers/next", params={"hardware": json.dumps({"cpus": 2, "gpus": []})}, headers=h).status_code == 204
    job = c.post("/api/v1/compute/jobs", json={"task": "probe", "preferred_providers": ["user_gpu"], "requires": {"providers": ["user_gpu"]}}).json()
    jid = job["job"]["id"]
    d.tick()
    assert dsp._row(jid)["status"] == "dispatched"
    nxt = c.get("/api/v1/compute/workers/next", headers=h)
    assert nxt.status_code == 200
    work = Path(dsp.compute_dir("pulltest"))
    (work / "w.py").write_text(nxt.json()["script"])
    (work / "b.json").write_text(json.dumps(nxt.json()["bundle"]))
    subprocess.run([sys.executable, "-I", str(work / "w.py"), "--bundle", str(work / "b.json"), "--out", str(work / "out")], check=True, capture_output=True)
    files = [("files", (f"{s}/{p.name}", p.read_bytes())) for s in ("artifacts", "checkpoints") for p in (work / "out" / s).glob("*")]
    up = c.post(f"/api/v1/compute/workers/{reg['worker_id']}/result", headers=h,
                data={"result": (work / "out" / "result.json").read_text(), "ref": nxt.json()["ref"]}, files=files)
    assert up.status_code == 200, up.text
    bad = c.post(f"/api/v1/compute/workers/{reg['worker_id']}/result", headers=h,
                 data={"result": "{}", "ref": nxt.json()["ref"]}, files=[("files", ("../../etc/x", b"1"))])
    assert bad.status_code == 422
    d.tick()
    assert dsp._row(jid)["status"] == "succeeded"
    detail = c.get(f"/api/v1/compute/jobs/{jid}").json()
    assert detail["artifacts"][0]["name"] == "probe.json" and detail["events"]
    provs = {p["name"]: p for p in c.get("/api/v1/compute/providers").json()}
    assert provs["user_gpu"]["health"]["status"] == "ok"
    nb = c.get(f"/api/v1/compute/workers/{reg['worker_id']}/notebook", params={"api_base": "https://nexus.example"}).json()
    assert tok.split(".", 1)[1] not in json.dumps(nb)
    q = c.get("/api/v1/compute/quota").json()
    assert q[0]["source"] == "measured" and q[0]["resource"] == "gpu_seconds_week"


def test_kaggle_status_parsing(home):
    outs = iter([(0, 'u/s has status "KernelWorkerStatus.RUNNING"', ""), (0, 'u/s has status "complete"', ""),
                 (0, 'u/s has status "KernelWorkerStatus.ERROR"', ""), (1, "", "404 Not Found")])
    k = KaggleProvider(runner=lambda a, c, t: next(outs))
    assert [k.poll("u/s").state for _ in range(4)] == ["running", "complete", "error", "unknown"]


def test_local_worker_env_is_scrubbed(monkeypatch):
    from nexus.cloud.compute.local import scrubbed_env
    monkeypatch.setenv("KAGGLE_API_TOKEN", "x")
    monkeypatch.setenv("NEXUS_VAULT_KEY", "y")
    e = scrubbed_env()
    assert "KAGGLE_API_TOKEN" not in e and "NEXUS_VAULT_KEY" not in e


def test_lost_attempt_without_result_is_retried(env):
    d, _ = env

    class Vanishing(LocalProvider):
        def submit(self, job_id, attempt, bundle, gpu, timeout_s):
            self.n = getattr(self, "n", 0) + 1
            return super().submit(job_id, attempt, bundle, gpu, timeout_s) if self.n > 1 else f"{job_id}-gone"

        def poll(self, ref):
            return RemoteStatus("lost", "session ended") if ref.endswith("-gone") else super().poll(ref)

    d.providers["local"] = Vanishing()
    row, _ = d.submit(ProductionJobManifest(task="probe", requires={"providers": ["local"]}, max_retries=1))
    r = d.run_until_done(row["id"], timeout=60, interval=0.05)
    assert r["status"] == "succeeded" and r["attempts"] == 2
    assert "worker.lost" in _types(row["id"])

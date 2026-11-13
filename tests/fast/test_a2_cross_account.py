"""Account 2 <-> Account 1 / Account 3 integration on a merged tree (skipped on a branch without their packages).

  * Account 1's dispatcher runs Account 2's compiled render steps on its local worker; the master comes back as an
    Account 1 artifact whose SHA-256 equals Account 2's ArtifactRef, and matches Account 2's own render frame-for-frame
    in length and geometry;
  * a Wan video job (GPU required) waits queued with no attempt spent when no GPU worker is authorised;
  * Account 2 documents (RenderJobManifest, QualityReport, GeneratedMediaManifest) validate through the shared entry
    point; an Account 2 ArtifactRef is accepted by Account 3's publishing engine, which keeps it non-public until the
    owner approves.
"""
import json
import shutil
import subprocess

import pytest

pytest.importorskip("nexus.cloud.compute.dispatcher")
pytest.importorskip("nexus.intelligence.publishing.engine")


def _clip(path, colour, audio=True, d=3):
    a = ["-f", "lavfi", "-i", f"sine=f=330:d={d}"] if audio else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=25:d={d}", *a,
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", *(["-c:a", "aac"] if audio else []),
                    "-shortest", str(path)], check=True)
    return path


@pytest.fixture()
def disp(home):
    from nexus import workflows  # noqa: F401  (registers Account 2's tasks with Account 1's dispatcher)
    from nexus.cloud.compute import dispatcher as dsp
    from nexus.cloud.compute.local import LocalProvider
    d = dsp.Dispatcher({"local": LocalProvider()}, poll_min=0, poll_max=0)
    dsp.set_dispatcher(d)
    yield d
    dsp.set_dispatcher(None)


def test_account1_worker_renders_account2_steps_and_stores_the_master(disp, tmp_path):
    from nexus import contracts, project as prj
    from nexus.cloud.compute import dispatcher as dsp
    from nexus.contracts import ProductionJobManifest
    from nexus.media import ff
    from nexus.timeline import ops, remote_render, render
    a = _clip(tmp_path / "a.mp4", "red")
    b = _clip(tmp_path / "b.mp4", "blue", audio=False)
    tl = prj.new_timeline(320, 180)
    tl["subtitles"] = []
    tl["clips"] = [ops.video_clip(str(a), 0.2, 1.4), ops.video_clip(str(b), 0.5, 2.0), ops.video_clip(str(a), 1.0, 2.6)]
    steps, inputs, expect = remote_render.worker_steps(tl)
    ids = {}
    for name, src in inputs.items():  # the worker receives inputs under their artifact names
        shutil.copyfile(src, tmp_path / name)
        ids[name] = dsp.register_local_file(tmp_path / name, "source", rights="owned").artifact_id
    m = ProductionJobManifest(**{**remote_render.production_manifest(tl, ids, "prj_x"), "requires": {"providers": ["local"]}})
    row, _ = disp.submit(m)
    r = disp.run_until_done(row["id"], timeout=180, interval=0.1)
    assert r["status"] == "succeeded", r["error"]
    res = json.loads(r["result"])
    met = res["final"]["metrics"]
    assert met["matches_expectation"] and met["width"] == 320 and met["height"] == 180
    arts = [x for x in dsp.job_artifacts(row["id"]) if x["name"] == "master.mp4"]
    assert len(arts) == 1
    path = dsp.artifact_path(arts[0])
    ref = contracts.artifact(path)  # Account 2's ArtifactRef of the same file
    assert ref["sha256"] == arts[0]["sha256"] and contracts.validate(ref, "ArtifactRef") == []
    local = render.render(tl, tmp_path / "local.mp4", tmp_path / "w")  # Account 2's own renderer
    assert abs(ff.probe(path)["duration"] - local["duration"]) <= 2 / 25 and abs(expect["duration"] - local["duration"]) <= 1 / 25


def test_rich_timelines_are_not_sent_to_a_plain_worker(tmp_path):
    from nexus import project as prj
    from nexus.timeline import ops, remote_render
    a = _clip(tmp_path / "a.mp4", "red")
    tl = prj.new_timeline(320, 180)
    tl["clips"] = [ops.video_clip(str(a), 0, 1, effects=[{"type": "vignette"}])]
    with pytest.raises(remote_render.NotRemoteRenderable):
        remote_render.worker_steps(tl)


def test_wan_job_waits_for_an_authorised_gpu_and_spends_no_attempt(home):
    from nexus import workflows  # noqa: F401
    from nexus.cloud.compute import dispatcher as D
    from nexus.contracts import ProductionJobManifest
    from nexus.generate import remote
    from nexus.timeline import remote_render
    job = remote.job_manifest("text_to_video", "wan21-t2v-1.3b", prompt="a red planet rising over a desert, slow push-in")
    fields = remote_render.wan_manifest(job)
    assert fields["requires"]["gpu"] is True and fields["requires"]["vram_gb"] >= 8.19
    fields["requires"]["providers"] = ["kaggle", "user_gpu", "colab"]
    r = D.submit(ProductionJobManifest(**fields))
    d = D.get()
    for _ in range(3):
        d.tick()
    s = D.status(r["job_id"])
    assert s["status"] == "queued" and s["attempts"] == 0


def test_account2_documents_and_artifacts_cross_into_account3_publishing(home, tmp_path):
    from nexus import contracts
    from nexus.generate import classify
    from nexus.intelligence.publishing import engine
    f = _clip(tmp_path / "master.mp4", "green")
    ref = contracts.artifact(f, role="master", media_type="video/mp4")
    qr = contracts.make("QualityReport", output=str(f), status="PASSED_AUTOMATED_CHECKS", stages=[], score=90.0, human_approval="PENDING")
    assert contracts.validate_any(qr) == []
    try:  # a "Wan output" whose compute record names no GPU is never labelled generated-video
        wan = classify.manifest("text_to_video", {"model": "wan21-t2v-1.3b"}, f, compute={"device": "cpu"})
        assert wan["classification"] != "generated-video"
    except classify.ClassificationError:
        pass
    gm = classify.manifest("image", {"model": "bk-sdm-tiny", "task": "image"}, f, compute={"device": "cpu"}, path_check=False)
    assert contracts.validate_any(gm) == [] and gm["classification"] != "generated-video"
    pub = engine.create("prj_x", {**ref, "uri": ref.get("uri") or f.resolve().as_uri()}, "youtube", {"title": "t", "description": "d"},
                        visibility="public")
    assert pub["visibility"] != "public" and pub["status"] == "awaiting_approval"

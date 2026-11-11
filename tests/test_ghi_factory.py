"""Tests G (content factory), H (zero-spend validation), I (resource failure recovery)."""
import ast
import json
import re
from pathlib import Path

import pytest

from nexus import compute, costguard, db, factory, project
from nexus.media import ff
from nexus.timeline import ops, render

ROOT = Path(__file__).resolve().parent.parent


def test_g_content_factory(results):
    plan = factory.schedule(5, languages=["ar", "de"], platforms=["youtube", "shorts", "tiktok", "instagram-reels"],
                            topics=["AI in radiology", "mRNA vaccines", "CRISPR explained", "Wearable ECGs", "AI drug discovery"])
    cap = plan["capacity"]
    (results / "G_factory.json").write_text(json.dumps(plan, indent=1, default=str))
    assert len(plan["jobs"]) == 5 and all(j["worker"] == "local-0" for j in plan["jobs"])
    assert all(j["quality_gates"][-1] == "HUMAN APPROVAL" for j in plan["jobs"])
    by = {r["profile"]: r for r in cap["profiles"]}
    # realistic reporting: the generative-cinematic profile must NOT be reported as achievable without GPU quota
    assert not by["generative-cinematic"]["meets_target"] and "GPU" in by["generative-cinematic"]["bottleneck"]
    assert by["narrated-explainer"]["minutes_per_master_all_versions"] > 3
    assert cap["human_review_hours_per_day"] > 0 and cap["verdict"]
    # capacity responds to resources: with GPU quota the generative profile produces some output, but still below 5/day at 30 h/week
    with_gpu = factory.capacity(5, 2, 4, free_gpu_hours_per_week=30)
    g = next(r for r in with_gpu["profiles"] if r["profile"] == "generative-cinematic")
    assert 0 < g["max_per_day"] < 5


def test_h_zero_spend(results):
    man = costguard.manifest()
    report = {"components": {}, "paid_sdk_imports": [], "paid_hosts_in_code": [], "blocked_demo": None}
    # 1) every component declares all zero-spend fields, costs nothing, needs no card
    for cid, c in man.items():
        v = costguard.violations(c)
        report["components"][cid] = {"cost_usd": c["monetary_cost_usd"], "card_required": c["payment_method_required"],
                                     "account_required": c["account_required"], "license": c["license"], "violations": v}
        assert not v, (cid, v)
    # 2) no paid SDK is imported and no paid endpoint appears anywhere in the code base
    for py in list((ROOT / "nexus").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for m in mods:
                if any(m == s or m.startswith(s + ".") for s in costguard.PAID_SDK_IMPORTS):
                    report["paid_sdk_imports"].append(f"{py.name}: {m}")
        if py.name != "costguard.py" and "tests" not in py.parts:  # product code only; this test names paid hosts to prove they are blocked
            report["paid_hosts_in_code"] += [f"{py.name}: {h}" for h in costguard.PAID_HOSTS if h in py.read_text()]
    reqs = (ROOT / "requirements.txt").read_text().lower()
    assert not report["paid_sdk_imports"] and not report["paid_hosts_in_code"]
    assert not any(re.search(rf"^{s}\b", reqs, re.M) for s in ("openai", "anthropic", "elevenlabs", "replicate", "boto3", "stripe"))
    # 3) a paid operation is blocked, a free alternative is found and used instead
    with pytest.raises(costguard.CostGuardError):
        costguard.authorize("elevenlabs", "tts", ["narration"])
    with pytest.raises(costguard.CostGuardError):
        costguard.check_host("https://api.openai.com/v1/chat/completions")
    with pytest.raises(costguard.CostGuardError):
        costguard.check_host("https://some-unknown-service.example.com/api")
    used, value = costguard.run_with_fallback("elevenlabs", ["narration"], {"piper": lambda: "spoken locally", "elevenlabs": lambda: 1 / 0})
    assert used == "piper" and value == "spoken locally"
    report["blocked_demo"] = {"requested": "elevenlabs", "used_instead": used}
    # 4) a manifest entry that needs a card or costs money is rejected even if someone adds it
    assert costguard.violations({**man["ffmpeg"], "payment_method_required": True})
    assert costguard.violations({**man["ffmpeg"], "monetary_cost_usd": 0.01})
    # 5) quota rules: remote providers are single-account, official-API only, and Colab is never automated
    fed = compute.Federation()
    colab = next(w for w in fed.workers if w.provider == "colab")
    assert not colab.automated and not colab.available()[0]
    src = (ROOT / "nexus" / "compute.py").read_text()
    assert "accounts" not in re.sub(r"#.*|\"\"\".*?\"\"\"", "", src, flags=re.S).lower(), "no multi-account logic may exist"
    spend = costguard.spend_report()
    assert spend["actual_spend_usd"] == 0
    report["ledger"] = {"actual_spend_usd": spend["actual_spend_usd"], "decisions": spend["decisions"]}
    (results / "H_zero_spend.json").write_text(json.dumps(report, indent=1))


def test_i_failure_recovery(results):
    p = project.Project.create("Test I - failover", "test")
    tl = project.new_timeline(640, 360)
    tl["clips"] = [ops.color_clip(1.5, c, text=[{"text": f"Scene {i + 1}", "style": "title"}]) for i, c in
                   enumerate(["0x203050", "0x503020", "0x205030", "0x502050", "0x505020", "0x205050"])]
    calls = []

    def seg(i):
        def run(state):
            calls.append(i)
            path, _ = render.render_segment(tl["clips"][i], tl, p.dir / "work" / "segs")
            return str(path)
        return (f"scene-{i + 1}", run)

    def assemble(state):
        out = p.dir / "renders" / "failover.mp4"
        lst = p.dir / "work" / "list.txt"
        lst.write_text("".join(f"file '{state['done'][f'scene-{i + 1}']}'\n" for i in range(6)))
        ff.run(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
        return str(out)

    steps = [seg(i) for i in range(6)] + [("assemble", assemble)]
    remote = compute.SimulatedRemoteGPU("sim-gpu-0", fail_after=3)
    fed = compute.Federation([remote, compute.LocalWorker("local-0")])
    # 'video-generation' prefers the GPU worker; it dies after 3 scenes
    r = fed.run_job("generate-scenes", "video-generation", steps, p.id)
    ev = [e["event"] for e in r["state"]["events"]]
    assert r["status"] == "done" and r["worker"] == "local-0"
    assert ev == ["assigned", "worker-lost", "cpu-fallback", "assigned"], ev
    assert calls == [0, 1, 2, 3, 4, 5], "finished scenes must not be re-rendered after the failure"
    lost = next(e for e in r["state"]["events"] if e["event"] == "worker-lost")
    assert lost["progress_saved"] == ["scene-1", "scene-2", "scene-3"]
    out = r["state"]["done"]["assemble"]
    assert abs(ff.duration(out) - 9.0) < 0.2 and not ff.decode_check(out)
    row = db.query("SELECT status, progress, worker FROM jobs WHERE id=?", (r["job"],))[0]
    assert row["status"] == "done" and row["progress"] == 1.0
    # when NO eligible worker exists and CPU fallback is not acceptable, the job is deferred - never sent anywhere paid
    fed2 = compute.Federation([compute.SimulatedRemoteGPU("sim-gpu-1", fail_after=0), compute.LocalWorker("local-0")])
    d = fed2.run_job("generate-scenes", "video-generation", steps[:1], p.id, cpu_fallback_ok=False)
    assert d["status"] == "deferred" and d["reasons"]
    (results / "I_failover.json").write_text(json.dumps({"events": r["state"]["events"], "final_worker": r["worker"], "scenes_rendered_order": calls,
                                                        "output_duration": ff.duration(out), "deferred_case": {"status": d["status"], "reasons": d["reasons"]},
                                                        "limitation": "after failover the remaining scenes ran on CPU: correct but slower; a GPU-only model would instead be deferred"},
                                                       indent=1, default=str))

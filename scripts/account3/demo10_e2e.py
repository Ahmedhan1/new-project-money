"""Account 3 Demo 10: research -> original strategy -> Strategy Handoff -> Account 2 render with the quality gates, and
evidence that each gate REJECTS what Demo 4 shipped.

Every step uses the real system on this machine (local LLM, Wikimedia Commons, the renderer, Tesseract). The output
is docs/results/account3/demo10_e2e.json + demo10_frames.jpg. Nothing is uploaded; publishing is exercised up to a
private draft manifest (dedupe check) with no account connected.

    python scripts/account3/demo10_e2e.py [--topic "Aswan High Dam"] [--skip-render]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "results" / "account3"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="Aswan High Dam")
    ap.add_argument("--niche", default="history")
    ap.add_argument("--scenes", type=int, default=4)
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--reuse-research", action="store_true", help="reuse the cached research of a previous run (local cache)")
    a = ap.parse_args()
    from nexus import assets, create
    from nexus.intelligence import handoff, strategy
    from nexus.intelligence.language import dubbing, readiness
    from nexus.intelligence.market import assess_v2
    from nexus.intelligence.market.pipeline import analyse
    from nexus.intelligence.market.sources import Ctx
    from nexus.intelligence.publishing import engine
    from nexus.intelligence.quality import scene_plan, typography, visual_context

    rep: dict = {"schema": "nexus.Demo10/1", "topic": a.topic, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": {}, "gates": {}}
    S, G = rep["steps"], rep["gates"]

    def save():
        (OUT / "demo10_e2e.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    # 1 research (real sources) + V2 assessment
    cache = ROOT / ".nexus" / "cache" / f"demo10_research_{a.topic.replace(' ', '_')}.json"
    if a.reuse_research and cache.exists():
        research = json.loads(cache.read_text(encoding="utf-8"))
    else:
        t = time.time()
        research = analyse(Ctx(min_interval=2.0, rate_limit_retries=4), a.topic, a.niche, "en")
        research["runtime_s"] = round(time.time() - t, 1)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(research, ensure_ascii=False, default=sorted), encoding="utf-8")
    asm = assess_v2.assess(research)
    S["research"] = {"seconds": research["runtime_s"], "score": research["opportunity"]["score"], "confidence": research["opportunity"]["confidence"],
                     "claims": len(research["brief"]["claims"]), "assessment_verdict": asm["verdict"], "uncertainty": asm["uncertainty"],
                     "credibility": asm["credibility"]["by_tier"], "cross_source": asm["cross_source"].get("value"),
                     "geography": asm["geography"]["countries"]}
    log("research", S["research"])
    save()

    # 2 original strategy (local LLM via the router) + handoff
    t = time.time()
    brief = strategy.claims_from_research(research["brief"])  # numbered claims + source URL, conflicts dropped
    script = strategy.write_script(a.topic, brief, None, scenes=a.scenes, language="en")
    board = strategy.storyboard(script)
    h = handoff.build(script, board, brief)
    S["strategy"] = {"seconds": round(time.time() - t, 1), "written_by": script["originality"]["written_by"], "title": script["title"],
                     "checks": h["checks"], "approved": h["approved"], "scenes": [s["narration"] for s in script["scenes"]],
                     "visual_queries": [s["visual_query"] for s in h["create_params"]["script"]["scenes"]],
                     "editorial_plan_decisions": len(h["editorial_plan"]["decisions"]), "render_job": h["render_job"]["requirements"]}
    log("strategy", S["strategy"]["checks"], S["strategy"]["title"])
    save()

    # 3 gate: unsupported sensational claim (the Demo 4 hook)
    bad = json.loads(json.dumps(script))
    bad["originality"]["hook"] = "The dam has a secret they never told you"
    bad["scenes"][0]["narration"] = "The dam has a secret they never told you. " + bad["scenes"][0]["narration"]
    bad["originality"]["framing"] = strategy.framing({"title": bad["title"], "hook": bad["originality"]["hook"], "scenes": bad["scenes"]}, brief)
    hb = handoff.build(bad, board, brief)
    try:
        handoff.submit(hb)
        refused = None
    except ValueError as e:
        refused = str(e)
    G["sensational_claim"] = {"rejected": refused is not None, "framing": bad["originality"]["framing"], "reason": refused}

    # 4 gate: geographic / historical imagery (real Commons candidates, refused before download)
    ctx = visual_context.scene_context(a.topic, "The Aswan Governorate in southern Egypt today", "Aswan governorate")
    cands = []
    for q in ("Aswan governorate", "governorate map", "Belgorod governorate 1745"):
        try:
            cands += assets.search_commons(q, n=6, min_width=600)
        except Exception as e:  # network
            G.setdefault("geo_errors", []).append(f"{q}: {e}")
    rows = []
    for c in cands:
        r = visual_context.check(c, ctx)
        rows.append({"title": c.get("title"), "ok": r["ok"], "findings": [f["check"] + ": " + f["detail"] for f in r["findings"]]})
    G["geographic_and_historical_imagery"] = {"candidates": len(rows), "refused": sum(not r["ok"] for r in rows),
                                              "accepted": [r["title"] for r in rows if r["ok"]][:6], "refusals": [r for r in rows if not r["ok"]][:10]}
    log("geo gate", G["geographic_and_historical_imagery"]["refused"], "of", len(rows), "refused")
    save()

    # 5 gate: missing glyphs + mixed-script rendering (rendered frames read back by OCR)
    credits = "Image: Lake Nasser by محمد شرف الدين, licensed CC BY-SA 4.0"
    tl = {"clips": [{"text": [{"text": credits}, {"text": "Hieroglyph 𓂀 caption"}]}]}
    st = typography.render_check(credits, "en")
    G["missing_glyphs"] = {"stage": scene_plan.typography_stage(tl, "en", ocr=False), "credits_render": {k: st.get(k) for k in ("recall", "ocr", "ok")},
                           "per_language_ocr": {lg: typography.render_check(readiness.SAMPLES[lg], lg).get("recall") for lg in ("ar", "en", "zh", "hi", "ru", "de")}}

    # 6 gate: excessive title cards
    plan = [{"scene": i, "kind": "title_card_fallback"} for i in range(3)] + [{"scene": 3, "kind": "photo"}]
    G["title_cards"] = {"three_of_four": scene_plan.visual_stage(plan)["passed"], "approved_by_owner": scene_plan.visual_stage(plan, True)["passed"]}

    # 7 gate: unsupported translation / dubbing
    try:
        v = dubbing.select_voice("ar-Gulf", commercial=True)
        vr = {"refused": False, "voice": v}
    except Exception as e:
        vr = {"refused": True, "reason": str(e)}
    g = readiness.assess("ar-Gulf")
    G["unsupported_translation"] = {"gulf_dubbing": vr, "gulf_readiness": {"score": g["score"], "level": g["level"], "blockers": g["blockers"][:4]}}

    # 8 gate: duplicate publishing (private draft only, nothing uploaded)
    art = OUT / "demo4_frames.jpg"
    meta = {"title": script["title"], "description": "draft"}
    a1 = engine.create("demo10", {"uri": str(art), "sha256": "0" * 64, "bytes": art.stat().st_size, "media_type": "video/mp4"}, "youtube", meta)
    a2 = engine.create("demo10", {"uri": str(art), "sha256": "0" * 64, "bytes": art.stat().st_size, "media_type": "video/mp4"}, "youtube", meta)
    G["duplicate_publishing"] = {"same_manifest": a1["id"] == a2["id"], "deduplicated": bool(a2.get("deduplicated")),
                                 "visibility": a1["visibility"], "status": a1["status"]}
    log("gates", {k: (v.get("rejected") if isinstance(v, dict) and "rejected" in v else "see json") for k, v in G.items()})
    save()

    # 9 real render: the approved script verbatim through Account 2 with the visual + typography stages
    if not a.skip_render and h["approved"]:
        t = time.time()
        out = create.create_video(a.topic, scenes=a.scenes, lang="en", script=h["create_params"]["script"], name="A3 demo10 " + a.topic[:40])
        from nexus import project as prj
        p = prj.Project(out["project"])
        stages = {s["stage"]: {"passed": s.get("passed"), "failed": [c["check"] + ": " + str(c["detail"])[:160] for c in s.get("checks", []) if not c["ok"]]}
                  for s in out["report"].get("stages", [])}
        S["render"] = {"seconds": round(time.time() - t, 1), "output": out["output"], "status": out["report"]["status"], "stages": stages,
                       "visual_plan": [{k: x.get(k) for k in ("scene", "kind", "reason")} | {"rejected": len(x.get("rejected", []))}
                                       for x in p.data.get("visual_plan", [])],
                       "narration_verbatim": [s["narration"] for s in p.data["script"]["scenes"]] == [s["narration"] for s in h["create_params"]["script"]["scenes"]]}
        try:
            import subprocess
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", out["output"], "-vf", "fps=1/6,scale=480:-2,tile=3x3", "-frames:v", "1",
                            str(OUT / "demo10_frames.jpg")], check=True, timeout=300)
            S["render"]["frames"] = "docs/results/account3/demo10_frames.jpg"
        except Exception as e:
            S["render"]["frames_error"] = str(e)
        log("render", S["render"]["status"], S["render"]["seconds"])
    rep["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save()


if __name__ == "__main__":
    main()

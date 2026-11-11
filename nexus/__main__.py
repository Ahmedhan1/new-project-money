"""Command line: python -m nexus <command>"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    ap = argparse.ArgumentParser(prog="nexus", description="NEXUS Video AI - zero-spend local video production")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the local app (UI + API)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    sub.add_parser("doctor", help="check installed tools, models, hardware and the zero-spend manifest")
    m = sub.add_parser("models", help="model cache")
    m.add_argument("action", choices=["fetch", "list"])
    c = sub.add_parser("create", help="create a video from an idea")
    c.add_argument("topic")
    c.add_argument("--scenes", type=int, default=5)
    e = sub.add_parser("edit", help="analyse a video, propose an edit, render it")
    e.add_argument("video")
    e.add_argument("--goal", default="tighten", choices=["tighten", "highlights"])
    e.add_argument("--title")
    k = sub.add_parser("capacity", help="daily production capacity from measured throughput")
    k.add_argument("--target", type=int, default=5)
    k.add_argument("--languages", type=int, default=2)
    k.add_argument("--gpu-hours-week", type=float, default=0)
    a = ap.parse_args()

    if a.cmd == "serve":
        import uvicorn
        print(f"NEXUS running at http://{a.host}:{a.port}  (local only, zero spend)")
        uvicorn.run("nexus.api:app", host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "doctor":
        from . import compute, costguard, tools
        ok = True
        for r in tools.health():
            state = "OK " if r["healthy"] else "-- " if r["healthy"] is None else "BAD"
            ok &= r["healthy"] is not False
            print(f"[{state}] {r['name']:<52} {r['version']}")
        hw = compute.detect_hardware()
        print(f"\nHardware: {hw['cpu']} | {hw['threads']} threads | {hw['ram_gb']} GB RAM | GPU: {hw['gpu'] or 'none'} | {hw['disk_free_gb']} GB free")
        print("Recommended:", json.dumps(hw["recommended"]))
        bad = {k: v["violations"] for k, v in costguard.spend_report()["components"].items() if v["violations"]}
        print("Zero-spend manifest:", "all components cost $0 and need no card" if not bad else f"VIOLATIONS {bad}")
        sys.exit(0 if ok and not bad else 1)
    elif a.cmd == "models":
        from . import models
        if a.action == "fetch":
            models.fetch_all()
        else:
            print(json.dumps(models.installed(), indent=1))
    elif a.cmd == "create":
        from . import create
        r = create.create_video(a.topic, scenes=a.scenes)
        print(json.dumps({"output": r["output"], "status": r["report"]["status"], "issues": r["report"]["known_issues"]}, indent=1))
    elif a.cmd == "edit":
        from . import edit, project, qa
        from .media import analyze
        p = project.Project.create(a.video, "edit")
        m_ = p.import_media(a.video)
        an = analyze.analyze(m_["path"])
        prop = edit.propose(an, a.goal)
        print("Proposal:", json.dumps({k: prop[k] for k in ("actions", "removed", "proposed_duration")}, indent=1))
        tl = edit.build_timeline(m_["path"], an, prop, title=a.title)
        p.save_timeline(tl, "accepted proposal")
        stats, rep = qa.render_with_qa(p, tl, "edit", {m_["path"]: an})
        print(json.dumps({"output": stats["output"], "status": rep["status"], "issues": rep["known_issues"]}, indent=1))
    elif a.cmd == "capacity":
        from . import factory
        c_ = factory.capacity(a.target, a.languages, free_gpu_hours_per_week=a.gpu_hours_week)
        for r in c_["profiles"]:
            print(f"{r['profile']:<22} {r['minutes_per_master_all_versions']:>6} min/master  max {r['max_per_day']:>3}/day  {'OK ' if r['meets_target'] else 'NO '} bottleneck: {r['bottleneck']}")
        print("\n".join("- " + v for v in c_["verdict"]))


if __name__ == "__main__":
    main()

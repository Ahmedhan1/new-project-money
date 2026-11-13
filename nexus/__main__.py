"""Command line: python -m nexus <command>"""
from __future__ import annotations

import argparse
import json
import sys


def _p(x) -> None:
    print(json.dumps(x, indent=1, default=str, ensure_ascii=False))


def _start(a):
    from . import launcher
    return launcher.start(a.port, not a.no_browser)


def _stop(a):
    from . import launcher
    return launcher.stop()


def _status(a):
    import time
    from . import launcher, state, storage
    sv = launcher.service_status()
    s = state.build_state()
    g = s["git"]
    print(f"NEXUS {s['version']}  -  {'RUNNING at ' + sv['url'] if sv['healthy'] else 'not running (NEXUS_START to start)'}")
    print(f"  api: {'up' if sv['api'] else 'down'}   worker: {'up' if sv['worker'] else 'down'}   data: {sv['home']}")
    print(f"\nGit: {g['branch']} @ {g['commit']}  changed files: {g['dirty_files']}  unpushed commits: {g['unpushed_commits']}")
    print("\nJobs:", s["jobs"]["counts"] or "none")
    for j in s["jobs"]["active"]:
        print(f"  {j['id']} {j['kind']:<18} {j['status']:<11} stage={j['stage']} retries={j['retries']} {j.get('recovery') or ''}")
    b = s["backup"]["last"]
    print("\nBackup:", "NEVER - run NEXUS_BACKUP" if not b else
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(b['ts']))}  verified copies: {b['copies']}" + (f"  WARNING: {b['warning']}" if b.get("warning") else ""))
    rd = s["backup"]["restore_drill"]
    print("  restore drill:", "never run (NEXUS_BACKUP --drill)" if not rd else f"{'PASSED' if rd['ok'] else 'FAILED'} {time.strftime('%Y-%m-%d %H:%M', time.localtime(rd['ts']))}")
    for d in storage.report():
        print(f"  [{'OK ' if d['available'] else '-- '}] {d['name']:<14} {d['provider']:<14} {d['detail']}" + (f"  {d['used_gb']} GB used" if d["used_gb"] is not None else ""))
    print("\nDevelopment tasks:", s["tasks"]["by_status"])
    for t in s["next_tasks"][:3]:
        print(f"  next: {t['id']} [{t['status']}] {t['title']}")


def _worker(a):
    from . import jobs
    print(f"NEXUS worker {jobs.worker_id()} started")
    jobs.run_worker()


def _jobs(a):
    from . import jobs
    if a.action == "list":
        s = jobs.summary()
        print(s["counts"])
        for j in s["active"]:
            print(f"{j['id']} {j['kind']:<18} {j['status']:<11} stage={j['stage']} retries={j['retries']} {j.get('recovery') or ''}")
    elif a.action == "recover":
        _p(jobs.recover())
    elif not a.id:
        print("job id required")
        return 2
    elif a.action == "show":
        _p(jobs.get(a.id))
    else:
        ok = jobs.retry(a.id) if a.action == "retry" else jobs.cancel(a.id)
        print("ok" if ok else "not possible in the job's current state")
        return 0 if ok else 1


def _print_backup(out) -> None:
    for r in out["results"]:
        if r["ok"]:
            print(f"[OK  ] {r['destination']:<14} snapshot {r['snapshot']}  {r['files']} files, {r['bytes'] / 1e6:.1f} MB, {r['new_blobs']} new objects, "
                  f"verified {r['verify']['objects']} objects ({r['verify']['checksums_compared']} checksums){' encrypted' if r['encrypted'] else ''}")
            for sk in r["skipped_over_cap"]:
                print(f"       NOT BACKED UP (free allowance full): {sk['path']}")
        else:
            print(f"[{'BLOCK' if r.get('blocked') else 'FAIL '}] {r['destination']:<14} {r.get('error') or r['verify']['problems'][:3]}")
    if out["warning"]:
        print("WARNING:", out["warning"])


def _backup(a):
    from . import backup
    out = backup.run_backup(a.tiers.split(",") if a.tiers else None, a.to)
    _print_backup(out)
    if a.drill and out["ok"]:
        d = backup.restore_drill(a.to)
        for r in d["results"]:
            print(f"[{'OK  ' if r['ok'] else 'FAIL'}] restore drill {r['destination']}: " + (f"{r['files']} files restored and verified" if r["ok"] else str(r["problems"][:3])))
        return 0 if d["ok"] else 1
    return 0 if out["ok"] else 1


def _open_dest(name):
    from . import storage
    last = None
    for d in storage.destinations():
        if d.get("enabled", True) and (not name or d["name"] == name):
            try:
                b = storage.open_destination(d)
                ok, why = b.available()
                if ok:
                    return b
                last = why
            except storage.StorageError as e:
                last = str(e)
    raise SystemExit(f"no usable storage destination{' named ' + name if name else ''}: {last}")


def _verify(a):
    from . import backup
    r = backup.verify(_open_dest(a.to), pw=backup.passphrase())
    _p(r)
    return 0 if r["ok"] else 1


def _restore(a):
    from . import backup
    r = backup.restore(_open_dest(a.src), a.target, a.snapshot, backup.passphrase())
    _p({k: r[k] for k in ("ok", "snapshot", "target", "files_restored", "files_expected", "problems", "paths_rewritten", "use")})
    print("references:", {k: (len(v) if isinstance(v, list) else v) for k, v in r["references"].items()})
    return 0 if r["ok"] else 1


def _storage(a):
    from . import storage
    print("Configured destinations:")
    for d in storage.report():
        print(f"  [{'OK ' if d['available'] else '-- '}] {d['name']:<14} {d['provider']:<14} {d['detail']}  used={d['used_gb']} GB cap={d['cap_gb']} GB tiers={d['tiers']}")
    print("\nKnown providers (registry/storage.yaml):")
    for pid, p in storage.providers().items():
        ok, why = storage.eligibility(pid)
        print(f"  [{'ELIGIBLE' if ok else 'BLOCKED '}] {pid:<20} free {p['free_gb'] or '-':>4} GB  {why}")


def _checkpoint(a):
    from . import state
    r = state.checkpoint(a.message, not a.no_push)
    if not r["ok"]:
        print("CHECKPOINT BLOCKED - nothing was committed:")
        for b in r["blocked"]:
            print("  -", b)
        print(r["fix"])
        return 1
    print(f"state saved | committed: {r['committed']} ({r['commit']}, {r['files']} file(s)) | pushed: {r['pushed']}")
    if not r["pushed"] and r.get("push"):
        print("  push:", r["push"]["reason"])
    return 0


def _resume(a):
    from . import state
    print(state.resume_brief())


def _tasks(a):
    from . import state
    ts = state.load_tasks()
    errs = state.validate_tasks(ts)
    for t in sorted(ts, key=lambda t: (t["priority"], t["id"])) if a.all else state.next_tasks(ts, 10):
        print(f"P{t['priority']} {t['id']:<8} {t['status']:<12} {t['title']}\n      next: {t['next']}")
    if errs:
        print("REGISTRY ERRORS:", errs)
        return 1


def _profile(a):
    from . import db
    rows = db.query("SELECT name, COUNT(*) n, AVG(value) avg, MIN(value) mn, MAX(value) mx FROM metrics WHERE name LIKE 'stage_seconds.%' GROUP BY name ORDER BY name")
    if not rows:
        print("no measurements yet - produce a video first")
    for r in rows:
        print(f"{r['name'][14:]:<44} n={r['n']:<3} avg {r['avg']:8.1f}s  min {r['mn']:8.1f}s  max {r['mx']:8.1f}s")


_OPS = {"start": _start, "stop": _stop, "status": _status, "worker": _worker, "jobs": _jobs, "backup": _backup, "verify": _verify,
        "restore": _restore, "storage": _storage, "checkpoint": _checkpoint, "resume": _resume, "tasks": _tasks, "profile": _profile}


def main() -> None:
    ap = argparse.ArgumentParser(prog="nexus", description="NEXUS Video AI - zero-spend local video production")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the local app (UI + API)")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--host", default=None)
    st = sub.add_parser("start", help="one-command start: checks, worker, API, health check, browser")
    st.add_argument("--port", type=int, default=None)
    st.add_argument("--no-browser", action="store_true")
    sub.add_parser("stop", help="stop the app (running jobs resume from their checkpoint next start)")
    sub.add_parser("status", help="services, jobs, backups, git state, next development tasks")
    sub.add_parser("worker", help="run a production worker (durable job ledger)")
    j = sub.add_parser("jobs", help="production job ledger")
    j.add_argument("action", choices=["list", "show", "retry", "cancel", "recover"])
    j.add_argument("id", nargs="?")
    b = sub.add_parser("backup", help="verified backup to every enabled storage destination")
    b.add_argument("--tiers", help="comma list: state,metadata,production,assets,originals,exports")
    b.add_argument("--to", help="only this destination")
    b.add_argument("--drill", action="store_true", help="after the backup, prove it restores (scratch directory)")
    v = sub.add_parser("verify", help="re-read a destination and compare every checksum")
    v.add_argument("--to")
    r = sub.add_parser("restore", help="restore the latest (or given) snapshot into a NEW directory")
    r.add_argument("target")
    r.add_argument("--from", dest="src", help="destination name (default: first available)")
    r.add_argument("--snapshot")
    sub.add_parser("storage", help="storage destinations, free allowances and zero-spend eligibility")
    cp = sub.add_parser("checkpoint", help="save project state, commit safe files, push (never forces)")
    cp.add_argument("-m", "--message")
    cp.add_argument("--no-push", action="store_true")
    sub.add_parser("resume", help="what a new session needs first: state, next tasks, jobs needing attention")
    t = sub.add_parser("tasks", help="development task registry")
    t.add_argument("--all", action="store_true")
    sub.add_parser("profile", help="measured time per production stage")
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
    from . import cli_a2  # Account 2 tools: disk, library, archive
    cli_a2.add_parsers(sub)
    a = ap.parse_args()

    if a.cmd == "serve":
        import uvicorn
        from . import config
        st_ = config.settings()
        host, port = a.host or st_["host"], a.port or st_["port"]
        print(f"NEXUS running at http://{host}:{port}  (local only, zero spend)")
        uvicorn.run("nexus.api:app", host=host, port=port, log_level="warning")
    elif a.cmd in _OPS:
        sys.exit(_OPS[a.cmd](a) or 0)
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
        from . import launcher, state, storage
        print("\nStartup checks:")
        for c in launcher.preflight():
            ok &= c["ok"]
            print(f"[{'OK ' if c['ok'] else 'BAD'}] {c['check']:<16} {c['detail']}" + ("" if c["ok"] else f"  -> {c['fix']}"))
        print("\nStorage destinations:")
        for d in storage.report():
            print(f"[{'OK ' if d['available'] else '-- '}] {d['name']:<14} {d['provider']:<14} {d['detail']}")
        st = state.status()
        print("\nProject memory:", "NEXUS_STATE.json valid" if st["state_file_valid"] else "NEXUS_STATE.json MISSING/INVALID - run `nexus checkpoint`",
              "| task registry", "valid" if not st["task_errors"] else st["task_errors"])
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


def _a2_ops() -> None:
    from . import cli_a2
    _OPS.update(cli_a2.OPS)


_a2_ops()


if __name__ == "__main__":
    main()

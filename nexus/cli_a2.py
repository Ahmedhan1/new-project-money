"""Account 2 command-line tools: `nexus disk`, `nexus library`, `nexus archive`.

Kept out of nexus/__main__.py so the shared CLI file only needs two hook lines (fewer merge conflicts between accounts).
"""
from __future__ import annotations


def _disk(a):
    import json as _j
    from . import diskman
    if a.action == "status":
        r = diskman.report()
        st = r["status"]
        print(f"[{st['level'].upper()}] free {st['free_gb']} GB on {r['usage']['disk']['path']}; caches {st['cache_gb']} GB")
        for m in st["messages"]:
            print("  " + m)
        for k, v in sorted(r["usage"]["categories"].items(), key=lambda kv: -kv[1]["bytes"]):
            print(f"  {k:<18} {v['bytes'] / 2 ** 30:8.3f} GB  {v['files']:>7} files")
    elif a.action == "plan":
        p = diskman.plan(a.rule or None)
        for i in p["items"][:200]:
            print(f"  {i['bytes'] / 2 ** 20:9.2f} MB  {i['rule']:<26} {i['path']}")
        print(f"plan {p['plan_id']}: {len(p['items'])} files, {p['bytes'] / 2 ** 30:.3f} GB; {p['protected_skipped_count']} protected files skipped")
        print(f"to delete exactly these: python -m nexus disk clean --plan {p['plan_id']}" + "".join(f" --rule {r}" for r in a.rule or []))
    elif a.action == "clean":
        if not a.plan:
            print("refusing: run `nexus disk plan` first and pass its --plan id")
            return 2
        print(_j.dumps(diskman.execute(a.plan, a.rule or None), indent=1))
    elif a.action == "settings":
        print(_j.dumps(diskman.settings(), indent=1))


def _library(a):
    from . import assetlib
    if a.action == "list":
        for e in assetlib.catalog(a.category, a.query):
            print(f"  {e['status']:<13} {e['id']:<34} {e['license'] or '':<14} {e['name']}")
    elif a.action == "install":
        for i in a.ids:
            r = assetlib.install(i)
            print(f"  {i}: {r.get('status') or 'installed'} {r.get('sha256', '')[:16]}")
    elif a.action == "verify":
        r = assetlib.verify_all()
        print(f"{r['ok']}/{r['checked']} verified; missing in timelines: {len(assetlib.missing())}")
        for f in r["failed"]:
            print("  FAILED", f["id"], f["detail"])
        return 0 if not r["failed"] else 1
    elif a.action == "summary":
        import json as _j
        print(_j.dumps(assetlib.summary(), indent=1))


def _archive(a):
    import json as _j
    from . import archive
    if a.action == "list":
        for r in archive.browse():
            print(f"  {r['state']:<9} {r['id']:<44} {r['size'] / 2 ** 20:8.1f} MB  local={'yes' if r['local_exists'] else 'no '}  {r['name']}")
    elif a.action == "add":
        print(_j.dumps(archive.archive_output(a.project, a.render), indent=1, default=str))
    elif a.action == "retrieve":
        print(archive.retrieve(a.record))
    elif a.action == "recover":
        print(_j.dumps(archive.recover(), indent=1))
    elif a.action == "status":
        print(_j.dumps(archive.summary(), indent=1, default=str))


OPS = {"disk": _disk, "library": _library, "archive": _archive}


def add_parsers(sub) -> None:
    dk = sub.add_parser("disk", help="disk usage by category, cleanup plan / execute, settings")
    dk.add_argument("action", choices=["status", "plan", "clean", "settings"])
    dk.add_argument("--plan")
    dk.add_argument("--rule", action="append")
    lb = sub.add_parser("library", help="asset library: list, install, verify, summary")
    lb.add_argument("action", choices=["list", "install", "verify", "summary"])
    lb.add_argument("ids", nargs="*")
    lb.add_argument("--category")
    lb.add_argument("--query")
    ar = sub.add_parser("archive", help="daily output archive: list, add, retrieve, recover, status")
    ar.add_argument("action", choices=["list", "add", "retrieve", "recover", "status"])
    ar.add_argument("--project")
    ar.add_argument("--render")
    ar.add_argument("--record")

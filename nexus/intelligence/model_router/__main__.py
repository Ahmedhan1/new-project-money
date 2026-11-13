"""python -m nexus.intelligence.model_router status|explain|health|capabilities|bench

  status [--privacy public|internal|private]   route per task + why other providers are excluded
  explain <task> [--privacy P] [--chars N]     route_explain() as JSON (what the UI shows)
  health                                       per-provider counts, p50/p95 latency, quotas, circuit
  capabilities                                 AIProviderCapability/v1 documents for Account 1's scheduler
  bench [...]                                  the reproducible benchmark (see bench.py --help)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import router


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "bench":
        from . import bench
        return bench.main(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m nexus.intelligence.model_router")
    ap.add_argument("cmd", choices=["status", "explain", "health", "capabilities"])
    ap.add_argument("task", nargs="?", default="general")
    ap.add_argument("--privacy", default="public", choices=list(router.PRIVACY_RANK))
    ap.add_argument("--chars", type=int, default=2000)
    a = ap.parse_args(argv)
    if a.cmd == "status":
        for task, r in router.status(a.privacy).items():
            route = " > ".join(r["route"]) or "NONE (queued / needs a human)"
            print(f"{task:26s} {route}")
        excluded = sorted({w for r in router.status(a.privacy).values() for w in r["excluded"]})
        print(f"\nprivacy={a.privacy}; excluded (deduplicated):")
        for w in excluded:
            print("  -", w)
    elif a.cmd == "explain":
        print(json.dumps(router.route_explain(a.task, a.privacy, a.chars), indent=1, ensure_ascii=False))
    elif a.cmd == "health":
        print(json.dumps(router.health(), indent=1, ensure_ascii=False))
    else:
        print(json.dumps(router.account1_capabilities(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

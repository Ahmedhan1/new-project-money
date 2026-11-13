"""Benchmark of GET /api/library (Asset Library page) and of a proposed one-function fix, without editing Account 2's
nexus/assetlib (QA does not touch that module): the fix is applied at runtime, and the output is checked identical.

Cause found by profiling: assetlib._registry() parses registry/asset_library.yaml with pure-Python PyYAML on EVERY
call, and license_check() -> policy() -> _registry() runs once per asset (183x per catalog, again for summary).

    NEXUS_HOME=<any home> python scripts/qa/bench_assetlib.py --out docs/results/final_qa/perf_assetlib.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

os.environ.setdefault("NEXUS_EMBEDDED_WORKER", "0")
import yaml  # noqa: E402

from nexus import assetlib, config  # noqa: E402


def library_payload() -> dict:            # exactly what nexus/api.py api_library() returns
    return {"assets": assetlib.catalog(None, None, None), "summary": assetlib.summary(),
            "categories": sorted(assetlib.CATEGORY_DIRS), "optional_future": assetlib.optional_future()}


def timed(n: int) -> tuple[list[float], dict]:
    xs, out = [], None
    for _ in range(n):
        t = time.perf_counter()
        out = library_payload()
        xs.append(round(time.perf_counter() - t, 4))
    return xs, out


def proposed_registry():
    """Proposed fix: parse once per file version (mtime + size), with the C loader when PyYAML has libyaml."""
    cache: dict = {}
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

    def _registry() -> dict:
        p = config.REGISTRY_DIR / "asset_library.yaml"
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size)
        if cache.get("key") != key:
            cache["data"] = yaml.load(p.read_text(encoding="utf-8"), Loader=loader)  # noqa: S506 - safe loader
            cache["key"] = key
        return cache["data"]
    return _registry


def invalidation_check() -> dict:
    """Edit a copy of the registry (rename one asset): the cached loader must return the new name on the next call."""
    import shutil
    import tempfile
    orig = config.REGISTRY_DIR
    tmp = Path(tempfile.mkdtemp())
    try:
        for f in Path(orig).iterdir():
            if f.is_file():
                shutil.copy2(f, tmp / f.name)
        config.REGISTRY_DIR = tmp
        first = assetlib.catalog(None, None, None)
        reg = tmp / "asset_library.yaml"
        data = yaml.safe_load(reg.read_text(encoding="utf-8"))
        entries = next(v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0])
        target = entries[0]["id"]
        entries[0]["name"] = "QA renamed asset"
        time.sleep(0.01)
        reg.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        second = assetlib.catalog(None, None, None)
        name = next((a["name"] for a in second if a["id"] == target), None)
        return {"edited_asset": target, "name_after_edit": name, "reloaded": name == "QA renamed asset",
                "unchanged_before_edit": any(a["id"] == target for a in first)}
    finally:
        config.REGISTRY_DIR = orig
        shutil.rmtree(tmp, ignore_errors=True)


def thread_check(reference: str, n: int = 16) -> dict:
    """n threads call the payload at once (FastAPI runs sync routes in a thread pool): every result must equal the reference."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(n) as ex:
        outs = list(ex.map(lambda _: json.dumps(library_payload(), sort_keys=True, default=str), range(n)))
    return {"threads": n, "identical": sum(o == reference for o in outs), "of": n}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    before, ref = timed(a.runs)
    assetlib._registry = proposed_registry()
    after, new = timed(a.runs)
    same = json.dumps(ref, sort_keys=True, default=str) == json.dumps(new, sort_keys=True, default=str)
    inval = invalidation_check()
    threads = thread_check(json.dumps(new, sort_keys=True, default=str))
    res = {"schema": "nexus.PerfBench/1", "what": "GET /api/library payload (catalog + summary + categories + optional_future)",
           "assets": len(ref["assets"]), "runs": a.runs,
           "before_s": {"median": statistics.median(before), "runs": before},
           "after_s": {"median": statistics.median(after), "runs": after},
           "speedup": round(statistics.median(before) / max(statistics.median(after), 1e-6), 1),
           "identical_output": same, "invalidation": inval, "concurrency": threads,
           "libyaml": bool(getattr(yaml, "__with_libyaml__", False)),
           "fix": "nexus/assetlib/__init__.py _registry(): cache the parsed YAML keyed by (mtime_ns, size); use CSafeLoader when available",
           "owner": "Account 2 (nexus/assetlib) via Account 1 integration"}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("assets", "before_s", "after_s", "speedup", "identical_output", "invalidation", "concurrency")}))


if __name__ == "__main__":
    main()

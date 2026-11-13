"""Deployed Tools Manager + Evolution Engine (discover -> verify -> compare; never self-modifies)."""
from __future__ import annotations

import csv
import importlib
import importlib.metadata
import shutil
import subprocess
import time

from . import config, costguard, db, models, net

CATALOG = config.REGISTRY_DIR / "catalog.tsv"


def health() -> list[dict]:
    """Version + health of every declared component (what is really installed on this machine)."""
    out = []
    for cid, c in costguard.manifest().items():
        row = {"id": cid, "name": c["name"], "kind": c["kind"], "status": c["status"], "license": c["license"],
               "weights_license": c.get("weights_license"), "source_url": c["source_url"], "install": c.get("install"),
               "capability": c.get("capability", []), "version": None, "healthy": None, "cost_usd": c["monetary_cost_usd"]}
        try:
            need = [b for b in c.get("requires_bin") or [] if not shutil.which(b)]
            if need:  # QA-D9: a component that needs a missing binary is not healthy, whatever its Python side says
                row["healthy"] = False
                row["version"] = f"unavailable: needs {', '.join(need)}"
                out.append(row)
                continue
            if c.get("version_cmd"):
                r = subprocess.run(c["version_cmd"], capture_output=True, text=True, timeout=30)
                row["version"] = (r.stdout or r.stderr).strip().splitlines()[0][:80]
                row["healthy"] = r.returncode == 0
            elif c.get("version_py"):
                importlib.import_module(c["version_py"])
                dist = {"faster_whisper": "faster-whisper", "scenedetect": "scenedetect", "piper": "piper-tts", "llama_cpp": "llama-cpp-python",
                        "PIL": "pillow", "ctranslate2": "ctranslate2", "manim": "manim", "numpy": "numpy"}[c["version_py"]]
                row["version"] = importlib.metadata.version(dist)
                row["healthy"] = True
            elif c["status"] == "adapter":
                row["healthy"] = None
                row["version"] = "adapter only - needs your own free account; not tested"
            else:
                row["healthy"] = True
                row["version"] = "remote service"
        except Exception as e:  # not installed / broken
            row["healthy"] = False
            row["version"] = f"unavailable: {type(e).__name__}"
        out.append(row)
    return out


def installed_models() -> dict:
    return models.installed()


def catalog() -> list[dict]:
    with open(CATALOG, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def evolution_check(limit: int = 20) -> dict:
    """Re-verify catalogued repositories against the live GitHub API and report what changed.

    Read-only: it reports newly archived / stale / revived projects and proposes re-evaluation.
    It never installs, upgrades or replaces anything by itself (no uncontrolled self-modification).
    """
    costguard.authorize("github-api", "evolution-check")
    rows = [r for r in catalog() if r["repo"].count("/") == 1][:limit]
    findings, checked, errors = [], 0, []
    for r in rows:
        try:
            d = net.get_json(f"https://api.github.com/repos/{r['repo']}", retries=1)
        except Exception as e:  # rate limit (60/h unauthenticated) or offline: report, do not guess
            errors.append(f"{r['repo']}: {e}")
            if len(errors) >= 3:
                break
            continue
        checked += 1
        pushed = (d.get("pushed_at") or "")[:10]
        if d.get("archived") and r["archived"] != "true":
            findings.append({"repo": r["repo"], "change": "ARCHIVED since last check", "action": "find a maintained replacement before the next upgrade"})
        if pushed and pushed > r["pushed"]:
            findings.append({"repo": r["repo"], "change": f"new activity {r['pushed']} -> {pushed}",
                             "action": "re-benchmark in a sandbox if this component is deployed" if r["decision"].startswith("DEPLOYED") else "none"})
        if pushed and pushed < time.strftime("%Y-%m-%d", time.gmtime(time.time() - 540 * 86400)) and r["decision"].startswith(("DEPLOYED", "NEXT")):
            findings.append({"repo": r["repo"], "change": f"no commits since {pushed}", "action": "treat as stale; evaluate alternatives"})
    report = {"date": time.strftime("%Y-%m-%d"), "checked": checked, "findings": findings, "errors": errors,
              "process": "DISCOVER > VERIFY > LICENSE > SECURITY > SANDBOX INSTALL > TEST > BENCHMARK > COMPARE > INTEGRATE > REVIEW > APPROVE > DEPLOY > MONITOR",
              "auto_applied": "nothing - upgrades require a tested alternative, a backup, a rollback path and human approval"}
    db.put_doc("evolution", report, report["date"])
    return report

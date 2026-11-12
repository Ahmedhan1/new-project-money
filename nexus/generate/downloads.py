"""Model download controls. Nothing here runs automatically: every fetch needs an explicit consent naming the model,
the size the user saw, and acceptance of the weights licence. Downloads come only from huggingface.co (pinned revision,
sha256-verified) and go to NEXUS_HOME/models/gen/<model id>/.

    plan(model_id) -> {"model", "files": [{"repo","revision","file","bytes","sha256"}], "total_bytes", "total_gb",
                       "licence": {...}, "disk_free_gb", "fits_disk", "within_cap", "cap_gb", "installed"}
    consent(model_id, accepted_gb, licence_accepted=True) -> dict        (what the UI sends after showing plan())
    fetch(model_id, consent=...) -> {"model", "paths": {file: path}, "bytes", "seconds"}
    installed(model_id) / path(model_id, file) / remove(model_id)

Per-model size cap: settings.json "generation": {"max_download_gb": N}; default 1.5 GB (larger models are routed to a
remote GPU worker through requirements.models instead of being downloaded onto the user's PC).
"""
from __future__ import annotations

import hashlib
import re
import json
import shutil
import time
from pathlib import Path

from .. import config, costguard
from . import catalog

DEFAULT_CAP_GB = 1.5
HOST = "https://huggingface.co"


class ConsentRequired(PermissionError):
    def __init__(self, msg: str, plan: dict):
        super().__init__(msg)
        self.plan = plan


def root(model_id: str) -> Path:
    return config.MODELS / "gen" / model_id


def cap_gb() -> float:
    s = ((config.settings() or {}).get("generation") or {})
    try:
        return float(s.get("max_download_gb", DEFAULT_CAP_GB))
    except (TypeError, ValueError):
        return DEFAULT_CAP_GB


def _files(m: dict) -> list[dict]:
    out = []
    for f in m.get("fetch") or []:
        for item in f.get("files") or []:
            out.append({"repo": f["repo"], "revision": f.get("revision", "main"), "file": item["file"],
                        "bytes": int(item.get("bytes") or 0), "sha256": item.get("sha256")})
        if not f.get("files"):  # legacy entry (allow_patterns for the diffusers worker): size from download_gb only
            out.append({"repo": f["repo"], "revision": f.get("revision", "main"), "file": None, "patterns": f.get("allow_patterns"),
                        "bytes": 0, "sha256": None})
    return out


def plan(model_id: str) -> dict:
    m = catalog.model(model_id)
    files = _files(m)
    total = sum(f["bytes"] for f in files)
    if not total:  # registry gives only a (possibly approximate, e.g. "~17 (...)") size: take its first number
        for k in ("download_gb", "repo_gb"):
            mt = re.search(r"\d+(?:\.\d+)?", str(m.get(k) or ""))
            if mt:
                total = int(float(mt.group()) * 2**30)
                break
    free = shutil.disk_usage(config.HOME if config.HOME.exists() else config.ROOT).free
    return {"model": model_id, "name": m["name"], "files": files, "total_bytes": total, "total_gb": round(total / 2**30, 3),
            "licence": {"class": catalog.licence_class(m), "weights_licence": m.get("weights_licence"),
                        "commercial_use": m.get("commercial_use"), "conditions": m.get("conditions"),
                        "source_url": m.get("source_url")},
            "disk_free_gb": round(free / 2**30, 1), "fits_disk": free > total * 1.2 + 2**30,
            "cap_gb": cap_gb(), "within_cap": total <= cap_gb() * 2**30, "installed": installed(model_id),
            "exact_files": all(f["file"] for f in files)}


def consent(model_id: str, accepted_gb: float, licence_accepted: bool = True, by: str = "user") -> dict:
    return {"model": model_id, "accepted_gb": float(accepted_gb), "licence_accepted": bool(licence_accepted), "by": by,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def _check_consent(p: dict, c: dict | None) -> None:
    if not c or c.get("model") != p["model"]:
        raise ConsentRequired(f"downloading {p['name']} ({p['total_gb']} GB) needs the user's explicit consent", p)
    if not c.get("licence_accepted"):
        raise ConsentRequired(f"the weights licence ({p['licence']['weights_licence']}) was not accepted", p)
    if float(c.get("accepted_gb", 0)) * 2**30 < p["total_bytes"]:
        raise ConsentRequired(f"consent covers {c.get('accepted_gb')} GB but the download is {p['total_gb']} GB", p)
    if p["licence"]["class"] == "excluded":
        raise catalog.LicenceRefused(f"{p['name']}: licence class excluded - never downloaded")
    if not p["within_cap"]:
        raise ConsentRequired(f"{p['total_gb']} GB exceeds the per-model cap of {p['cap_gb']} GB on this machine; run it on a "
                              "remote GPU worker (requirements.models) or raise settings.generation.max_download_gb", p)
    if not p["fits_disk"]:
        raise ConsentRequired(f"not enough free disk ({p['disk_free_gb']} GB) for {p['total_gb']} GB", p)
    if not p["exact_files"]:
        raise ConsentRequired("this registry entry has no pinned file list; use the generation environment's own fetch", p)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_path(model_id: str) -> Path:
    return root(model_id) / "installed.json"


def installed(model_id: str) -> bool:
    mp = _manifest_path(model_id)
    if not mp.exists():
        return False
    try:
        rec = json.loads(mp.read_text(encoding="utf-8"))
    except ValueError:
        return False
    return all((root(model_id) / f).exists() for f in rec.get("files", {}))


def path(model_id: str, file: str | None = None) -> Path:
    if not installed(model_id):
        raise FileNotFoundError(f"{model_id} is not installed (generate.downloads.plan('{model_id}') shows what it needs)")
    rec = json.loads(_manifest_path(model_id).read_text(encoding="utf-8"))
    f = file or next(iter(rec["files"]))
    return root(model_id) / f


def fetch(model_id: str, *, consent: dict | None = None, opener=None) -> dict:  # noqa: A002 - public API name
    """Download the pinned files after consent. `opener(url) -> file-like` is injectable for tests."""
    p = plan(model_id)
    _check_consent(p, consent)
    if p["installed"]:
        return {"model": model_id, "paths": {f["file"]: str(root(model_id) / f["file"]) for f in p["files"]}, "bytes": 0,
                "seconds": 0.0, "already_installed": True}
    if opener is None:
        from .. import net
        opener = net._open  # costguard.check_host runs inside
    t0 = time.time()
    got = 0
    paths = {}
    for f in p["files"]:
        url = f"{HOST}/{f['repo']}/resolve/{f['revision']}/{f['file']}"
        costguard.check_host(url)
        dest = root(model_id) / f["file"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with opener(url) as r, tmp.open("wb") as w:
            while chunk := r.read(1 << 20):
                w.write(chunk)
                got += len(chunk)
        if f["sha256"] and _sha256(tmp) != f["sha256"]:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"sha256 mismatch for {f['repo']}/{f['file']} - download discarded")
        tmp.replace(dest)
        paths[f["file"]] = str(dest)
    rec = {"model": model_id, "files": {f["file"]: {"sha256": f["sha256"], "bytes": f["bytes"], "repo": f["repo"],
                                                    "revision": f["revision"]} for f in p["files"]},
           "consent": consent, "licence": p["licence"], "fetched": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _manifest_path(model_id).write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return {"model": model_id, "paths": paths, "bytes": got, "seconds": round(time.time() - t0, 1)}


def remove(model_id: str) -> bool:
    """Delete a downloaded model (the user's explicit action)."""
    r = root(model_id)
    if r.exists():
        shutil.rmtree(r)
        return True
    return False

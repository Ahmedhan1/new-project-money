"""Disk management: measure, estimate, preflight, and clean only what is provably safe to clean.

Why this exists: the Account 2 demo run filled the development disk (render caches, benchmark homes and test outputs
accumulated with no bound). See docs/DISK_MANAGEMENT.md. The rules here:

* Measurement by category (render cache, media cache, temporary, previews, project sources, renders, archive metadata,
  assets, models, benchmarks, logs, backups) - `usage()`, `status()`.
* A render estimates the space it needs and is refused BEFORE it starts when it cannot finish (`preflight`). Before
  refusing, the least recently used cache entries that are not protected are evicted to make room.
* Cleanup is a two-step plan -> execute. Every candidate is a FILE inside an explicit allowlist of NEXUS_HOME
  directories, matched by a named rule; nothing is ever deleted by "remove this folder". `execute(plan_id)` refuses a
  plan that changed since it was shown, and re-validates every file just before deleting it.
* Never deleted by this module: project source media, anything referenced by a timeline or an asset manifest, final
  renders and exports (only nexus/archive.py may release a final copy, and only after a verified external copy),
  files of an active job's project, cache entries used after the oldest running render started, models (except
  abandoned partial downloads), backups, the database, settings, vault keys and secrets, and anything outside
  NEXUS_HOME (user documents).

Settings: NEXUS_HOME/disk.json (never committed), defaults in DEFAULTS.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from . import config

GB = 2 ** 30
DEFAULTS = {
    "warn_free_gb": 10.0,          # show a warning below this much free space
    "min_free_gb": None,           # never start a job that would leave less than this (None = settings.min_free_disk_gb)
    "cache_quota_gb": 20.0,        # render + media caches together (LRU-evicted above this)
    "asset_cache_quota_gb": 10.0,  # downloaded, re-downloadable library assets (nexus/assetlib)
    "preview_max_age_days": 7,     # draft pieces / preview renders older than this are stale
    "tmp_max_age_hours": 12,       # partial downloads, temp files and abandoned render work older than this
    "job_log_max_age_days": 30,    # logs of FINISHED jobs older than this
    "log_max_mb": 20,              # rotation size for the disk manager's own journal helpers
    "log_keep": 3,                 # generations kept by rotate_log() callers in Account 2 code
    "render_margin": 1.25,         # safety factor on the render estimate
}
RENDER_CACHE = ("segments", "pieces", "pieces_draft", "audio", "overlays")
MEDIA_CACHE = ("proxies", "thumbs", "visual", "media", "tts", "music", "sfx", "ambience", "broll", "generate", "manim",
               "blender", "previews")
TEMP_CACHE = ("uploads", "transfers", "manifests")
PARTIAL = (".part", ".partial", ".download", ".tmp", ".crdownload")
NEVER = ("secrets", "vault", "vault.key", "nexus.db", "nexus.db-wal", "nexus.db-shm", "settings.json", "storage.json",
         "storage_rules.json", "disk.json", "backups")


class InsufficientDisk(RuntimeError):
    """A job was refused because it cannot finish in the free space (no data was touched)."""


class UnsafePath(ValueError):
    """A deletion was refused because the path is outside the allowlist or protected."""


# ------------------------------------------------------------------ settings
def settings() -> dict:
    s = dict(DEFAULTS)
    f = config.HOME / "disk.json"
    if f.exists():
        try:
            s.update({k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items() if k in DEFAULTS})
        except ValueError:
            pass
    if s["min_free_gb"] is None:
        s["min_free_gb"] = float(config.settings().get("min_free_disk_gb", 2.0))
    return s


def save_settings(new: dict) -> dict:
    bad = [k for k in new if k not in DEFAULTS]
    if bad:
        raise ValueError(f"unknown disk settings: {bad}")
    for k, v in new.items():
        if v is not None and (not isinstance(v, (int, float)) or v < 0):
            raise ValueError(f"{k} must be a non-negative number")
    cur = {}
    f = config.HOME / "disk.json"
    if f.exists():
        cur = json.loads(f.read_text(encoding="utf-8"))
    cur.update(new)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    return settings()


# ------------------------------------------------------------------ measuring
def _walk(root: Path) -> tuple[int, int]:
    total = n = 0
    if not root.exists() or root.is_symlink():  # a linked folder (e.g. shared models) is not stored here
        return 0, 0
    if root.is_file():
        return root.stat().st_size, 1
    for dp, dns, fns in os.walk(root, followlinks=False):
        for fn in fns:
            try:
                st = os.lstat(os.path.join(dp, fn))
            except OSError:
                continue
            total += st.st_size
            n += 1
    return total, n


def _files(root: Path):
    if not root.exists():
        return
    for dp, _dns, fns in os.walk(root, followlinks=False):
        for fn in fns:
            p = Path(dp) / fn
            if not p.is_symlink():
                yield p


def categories() -> dict[str, list[Path]]:
    H, C = config.HOME, config.CACHE
    known = set(RENDER_CACHE) | set(MEDIA_CACHE) | set(TEMP_CACHE)
    other_cache = [p for p in (C.iterdir() if C.exists() else []) if p.name not in known]
    projects = sorted(config.PROJECTS.iterdir()) if config.PROJECTS.exists() else []
    return {
        "render_cache": [C / n for n in RENDER_CACHE],
        "media_cache": [C / n for n in MEDIA_CACHE] + other_cache,
        "temporary": [C / n for n in TEMP_CACHE] + [H / "tmp"] + [p / "work" for p in projects],
        "project_sources": [p / "media" for p in projects],
        "renders": [p / d for p in projects for d in ("renders", "exports")],
        "project_other": [p / d for p in projects for d in ("timelines", "reports", "audio")],
        "archive_local": [H / "archive"],
        "assets": [config.ASSETS],
        "models": [config.MODELS],
        "benchmarks": [H / "benchmarks"],
        "logs": [config.LOGS],
        "backups": [config.BACKUPS],
        "database": [config.DB_PATH],
    }


def usage() -> dict:
    """Bytes per category + the disk the working storage is on. Measured, never estimated."""
    out = {}
    for cat, roots in categories().items():
        b = n = 0
        for r in roots:
            x, y = _walk(r)
            b, n = b + x, n + y
        out[cat] = {"bytes": b, "files": n}
    work = config.CACHE if config.CACHE.exists() else config.HOME
    while not work.exists() and work != work.parent:
        work = work.parent
    du = shutil.disk_usage(work)
    return {"categories": out, "disk": {"path": str(work), "total": du.total, "used": du.used, "free": du.free},
            "home": str(config.HOME), "work_dir": str(config.CACHE), "measured_at": time.time()}


def status(u: dict | None = None) -> dict:
    """ok / warning / critical with messages a person can act on."""
    u = u or usage()
    s = settings()
    free = u["disk"]["free"] / GB
    cat = u["categories"]
    cache = (cat["render_cache"]["bytes"] + cat["media_cache"]["bytes"]) / GB
    msgs, level = [], "ok"
    if free < s["min_free_gb"]:
        level = "critical"
        msgs.append(f"Only {free:.1f} GB free (minimum {s['min_free_gb']:.1f} GB): renders and downloads are refused until space is freed.")
    elif free < s["warn_free_gb"]:
        level = "warning"
        msgs.append(f"{free:.1f} GB free (warning below {s['warn_free_gb']:.1f} GB).")
    if cache > s["cache_quota_gb"]:
        level = "warning" if level == "ok" else level
        msgs.append(f"Caches use {cache:.1f} GB, above the {s['cache_quota_gb']:.1f} GB quota; the oldest unused entries can be evicted.")
    pend = _pending_archive_bytes()
    if pend:
        msgs.append(f"{pend / GB:.2f} GB of final videos wait for a verified external archive copy; they stay on this disk until then.")
    return {"level": level, "free_gb": round(free, 2), "cache_gb": round(cache, 2), "messages": msgs or ["Disk space is fine."],
            "settings": s}


def _pending_archive_bytes() -> int:
    try:
        from . import archive
        return sum(r.get("size", 0) for r in archive.records() if r.get("state") not in ("released", "verified"))
    except Exception:  # noqa: BLE001 - archive module optional at this point
        return 0


# ------------------------------------------------------------------ estimating + preflight
def estimate_render(tl: dict, draft: bool = False) -> dict:
    """Bytes a render of `tl` needs on the working disk: final file + segment cache + piece cache + audio + overlays.

    Calibrated on Demo 1 (228 s, 1080p30, libx264 veryfast CRF 20: 133 MB final = 4.7 Mbit/s) and checked against a real
    render in tests/fast/test_a2_diskman.py (the estimate must cover the measured growth)."""
    dur = float(tl.get("duration") or 0) or sum(float(c.get("_duration") or c.get("duration") or
                                                    (float(c.get("out", 0)) - float(c.get("in", 0))) or 0) for c in tl.get("clips", []))
    w, h = int(tl.get("width", 1920)), int(tl.get("height", 1080))
    px = (w * h) / (1920 * 1080)
    if draft:
        px /= 4
    fps = float(tl.get("fps", config.FPS))
    mbit = max(0.6, 4.7 * px * (fps / 30) ** 0.5) + 0.2  # video + 192 kbit/s audio
    final = dur * mbit * 1e6 / 8
    n_clips = len(tl.get("clips", []))
    layers = sum(len(t.get("items", [])) for t in tl.get("tracks", []) if t.get("kind") != "audio")
    intermediates = final * 2.2 + (n_clips + layers) * 2e6  # segments ~1x, pieces ~1x, tmp copies, overlays/PNGs
    need = (final + intermediates) * settings()["render_margin"] + 64e6
    return {"duration_s": round(dur, 2), "final_bytes": int(final), "intermediate_bytes": int(intermediates),
            "required_bytes": int(need), "draft": draft}


def preflight(required_bytes: int, what: str = "job", path: Path | None = None, evict: bool = True) -> dict:
    """Refuse (InsufficientDisk) a job that would leave less than min_free_gb. Before refusing, evict unprotected
    least-recently-used cache entries. Returns the decision with the numbers."""
    s = settings()
    target = Path(path or config.CACHE)
    while not target.exists() and target != target.parent:
        target = target.parent
    floor = s["min_free_gb"] * GB
    free = shutil.disk_usage(target).free
    evicted = None
    if free - required_bytes < floor and evict:
        evicted = evict_cache(bytes_needed=int(required_bytes + floor - free))
        free = shutil.disk_usage(target).free
    ok = free - required_bytes >= floor
    d = {"ok": ok, "what": what, "free_bytes": free, "required_bytes": int(required_bytes), "min_free_bytes": int(floor),
         "evicted": evicted}
    if not ok:
        raise InsufficientDisk(
            f"not enough disk space for {what}: needs {required_bytes / GB:.2f} GB and must leave {s['min_free_gb']:.1f} GB free, "
            f"but only {free / GB:.2f} GB is free on {target}. Nothing was changed. Free space (Storage page -> clean up, "
            f"or archive finished videos), or point the working directory (NEXUS_WORK_DIR) at a bigger disk, then retry.")
    return d


# ------------------------------------------------------------------ protection
def _allowed_roots() -> list[Path]:
    H, C = config.HOME, config.CACHE
    roots = [C / n for n in RENDER_CACHE + MEDIA_CACHE + TEMP_CACHE] + [H / "tmp", config.LOGS / "jobs", config.LOGS,
                                                                        config.MODELS, config.ASSETS]
    if config.PROJECTS.exists():
        roots += [p / "work" for p in config.PROJECTS.iterdir()]
        roots += [p / "renders" for p in config.PROJECTS.iterdir()]  # draft previews only (rule-checked)
    if C.exists():
        known = set(RENDER_CACHE) | set(MEDIA_CACHE) | set(TEMP_CACHE)
        roots += [p for p in C.iterdir() if p.is_dir() and p.name not in known]
    return [r.resolve() for r in roots]


def _active_jobs() -> list[dict]:
    try:
        from . import db
        return db.query("SELECT id,kind,project_id,status,started,created,params FROM jobs WHERE status IN ('queued','running')")
    except Exception:  # noqa: BLE001
        return []


def _timeline_refs(tl: dict) -> set[str]:
    refs: set[str] = set()

    def add(x):
        if isinstance(x, str) and x and not x.startswith(("lavfi", "color", "asset:")):
            refs.add(x)

    for c in tl.get("clips", []):
        add(c.get("src"))
        if isinstance(c.get("pip"), dict):
            add(c["pip"].get("src"))
        for t in c.get("titles") or []:
            add(t.get("image") if isinstance(t, dict) else None)
    for t in tl.get("tracks", []):
        for it in t.get("items", []):
            add(it.get("src"))
    for a in tl.get("audio", []):
        add(a.get("src"))
    if isinstance(tl.get("music"), dict):
        add(tl["music"].get("src"))
    if isinstance(tl.get("lut"), str):
        add(tl["lut"])
    for o in tl.get("overlays", []) or []:
        add(o.get("src") if isinstance(o, dict) else None)
    return refs


def protected() -> dict:
    """Everything cleanup must not touch: {paths: set[str], dirs: set[str], since: float|None (cache entries used after
    this time are in use)}."""
    paths: set[str] = set()
    dirs: set[str] = set()
    for n in NEVER:
        dirs.add(str((config.HOME / n).resolve()))
    if config.PROJECTS.exists():
        for p in config.PROJECTS.iterdir():
            dirs.add(str((p / "media").resolve()))
            dirs.add(str((p / "exports").resolve()))
            rdir = p / "renders"  # finals: only nexus/archive.py may release one (after a verified external copy)
            for f in (rdir.iterdir() if rdir.exists() else []):
                if "_draft" not in f.name:
                    paths.add(str(f.resolve()))
            tdir = p / "timelines"
            for f in (tdir.glob("*.json") if tdir.exists() else []):
                try:
                    tl = json.loads(f.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                for r in _timeline_refs(tl):
                    q = Path(r)
                    q = q if q.is_absolute() else p / q
                    paths.add(str(q.resolve()))
    act = _active_jobs()
    since = None
    for j in act:
        if j.get("project_id"):
            dirs.add(str((config.PROJECTS / j["project_id"]).resolve()))
        if j["kind"] in ("render", "shorts", "direct", "deliver", "export", "clip-process", "animate", "remix", "localize"):
            t = j.get("started") or j.get("created") or time.time()
            since = t if since is None else min(since, t)
        if j["kind"] in ("model-download", "visual-install", "asset-install"):
            dirs.add(str(config.MODELS.resolve()))
            dirs.add(str(config.ASSETS.resolve()))
    try:  # asset library: installed files are managed by nexus/assetlib (its own quota, references, favourites)
        from . import assetlib
        paths |= {str(Path(x).resolve()) for x in assetlib.installed_files()}
    except Exception:  # noqa: BLE001
        pass
    try:  # final videos without a verified external copy: the only copy
        from . import archive
        for r in archive.records():
            if r.get("state") != "released" and r.get("local_path"):
                paths.add(str(Path(r["local_path"]).resolve()))
    except Exception:  # noqa: BLE001
        pass
    return {"paths": paths, "dirs": dirs, "since": (since - 60) if since else None}


def check_deletable(path: str | Path, prot: dict | None = None) -> Path:
    """Resolve + validate one file for deletion; raises UnsafePath with the reason."""
    p = Path(path)
    if p.is_symlink():
        raise UnsafePath(f"{p}: symbolic link (never followed or deleted)")
    rp = p.resolve()
    home = config.HOME.resolve()
    roots = _allowed_roots()
    if not any(rp == r or r in rp.parents for r in roots):
        raise UnsafePath(f"{rp}: outside the cleanup allowlist")
    if home not in rp.parents and config.CACHE.resolve() not in rp.parents:
        raise UnsafePath(f"{rp}: outside NEXUS_HOME and the working directory")
    if not rp.is_file():
        raise UnsafePath(f"{rp}: not a regular file")
    prot = prot if prot is not None else protected()
    if str(rp) in prot["paths"]:
        raise UnsafePath(f"{rp}: referenced by a project, an asset manifest or an unarchived final video")
    for d in prot["dirs"]:
        if rp == Path(d) or str(rp).startswith(d.rstrip(os.sep) + os.sep):
            raise UnsafePath(f"{rp}: inside a protected directory ({d})")
    if prot.get("since") and rp.stat().st_mtime >= prot["since"] and config.CACHE.resolve() in rp.parents:
        raise UnsafePath(f"{rp}: used by a render that is still running")
    return rp


def safe_delete(path: str | Path, prot: dict | None = None) -> int:
    rp = check_deletable(path, prot)
    n = rp.stat().st_size
    rp.unlink()
    _prune_empty(rp.parent)
    return n


def _prune_empty(d: Path) -> None:
    stop = {config.HOME.resolve(), config.CACHE.resolve(), config.PROJECTS.resolve(), config.MODELS.resolve(), config.ASSETS.resolve()}
    while d not in stop and d.exists():
        try:
            d.rmdir()  # only succeeds when empty
        except OSError:
            return
        d = d.parent


# ------------------------------------------------------------------ cleanup rules
def _cand(p: Path, rule: str, reason: str) -> dict | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return {"path": str(p.resolve()), "bytes": st.st_size, "mtime": st.st_mtime, "rule": rule, "reason": reason}


def _rule_partials(now: float, s: dict) -> list[dict]:
    age = s["tmp_max_age_hours"] * 3600
    out = []
    for root in (config.CACHE, config.MODELS, config.ASSETS, config.HOME / "tmp"):
        for p in _files(root):
            n = p.name.lower()
            if (n.endswith(PARTIAL) or ".tmp." in n) and now - p.stat().st_mtime > age:
                c = _cand(p, "incomplete_download", f"partial/temporary file untouched for {s['tmp_max_age_hours']} h")
                if c:
                    out.append(c)
    return out


def _rule_temp(now: float, s: dict) -> list[dict]:
    age = s["tmp_max_age_hours"] * 3600
    out = []
    roots = [config.CACHE / n for n in TEMP_CACHE] + [config.HOME / "tmp"]
    if config.PROJECTS.exists():
        roots += [p / "work" for p in config.PROJECTS.iterdir()]
    for r in roots:
        for p in _files(r):
            if now - p.stat().st_mtime > age:
                c = _cand(p, "abandoned_temporary", f"temporary/render-work file older than {s['tmp_max_age_hours']} h with no active job")
                if c:
                    out.append(c)
    return out


def _rule_previews(now: float, s: dict) -> list[dict]:
    age = s["preview_max_age_days"] * 86400
    out = []
    for p in _files(config.CACHE / "pieces_draft"):
        if now - p.stat().st_mtime > age:
            c = _cand(p, "stale_preview", f"draft piece unused for {s['preview_max_age_days']} days")
            if c:
                out.append(c)
    if config.PROJECTS.exists():
        for pr in config.PROJECTS.iterdir():
            for p in (pr / "renders").glob("*_draft_*") if (pr / "renders").exists() else []:
                if p.is_file() and now - p.stat().st_mtime > age:
                    c = _cand(p, "stale_preview", f"draft preview render older than {s['preview_max_age_days']} days (finals are never touched)")
                    if c:
                        out.append(c)
    return out


def _rule_logs(now: float, s: dict) -> list[dict]:
    age = s["job_log_max_age_days"] * 86400
    active = {j["id"] for j in _active_jobs()}
    out = []
    for p in _files(config.LOGS / "jobs"):
        if p.stem not in active and now - p.stat().st_mtime > age:
            c = _cand(p, "old_job_log", f"log of a finished job older than {s['job_log_max_age_days']} days")
            if c:
                out.append(c)
    # Rotated service logs (server.log.1 ...) are owned by the launcher's rotation (Account 1 keeps 5 generations):
    # the disk manager does not delete them.
    return out


def _cache_files() -> list[Path]:
    return [p for n in RENDER_CACHE + MEDIA_CACHE for p in _files(config.CACHE / n)]


def _rule_cache_quota(now: float, s: dict, bytes_needed: int = 0) -> list[dict]:
    files = []
    for p in _cache_files():
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))
    total = sum(f[1] for f in files)
    excess = max(total - s["cache_quota_gb"] * GB, bytes_needed)
    out = []
    for mt, size, p in sorted(files):  # least recently used first
        if excess <= 0:
            break
        out.append({"path": str(p.resolve()), "bytes": size, "mtime": mt, "rule": "cache_lru",
                    "reason": "least recently used cache entry (rebuilt automatically when needed)"})
        excess -= size
    return out


def _rule_after_export(now: float, s: dict) -> list[dict]:
    """Intermediate segments/pieces of a final render whose output has a verified external archive copy, unless another
    project's render checkpoint still lists them."""
    try:
        from . import archive
        done = {(r.get("project_id"), Path(r.get("source_path") or r.get("local_path") or "").stem)
                for r in archive.records() if r.get("state") in ("verified", "released") and r.get("kind") == "master"}
    except Exception:  # noqa: BLE001
        return []
    if not done or not config.PROJECTS.exists():
        return []
    keep: set[str] = set()
    drop: dict[str, str] = {}
    for pr in config.PROJECTS.iterdir():
        for cpf in (pr / "work").glob("checkpoint_*.json") if (pr / "work").exists() else []:
            try:
                cp = json.loads(cpf.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            stems = set(cp.get("segments_done", [])) | set(cp.get("pieces_done", []))
            if (pr.name, Path(cp.get("output", "")).stem) in done and cp.get("stage") == "done":
                for st in stems:
                    drop[st] = f"{pr.name}/{Path(cp.get('output', '')).name}"
            else:
                keep |= stems
    out = []
    for n in ("segments", "pieces"):
        for p in _files(config.CACHE / n):
            if p.stem in drop and p.stem not in keep:
                c = _cand(p, "intermediate_after_archive", f"intermediate of {drop[p.stem]}, whose final has a verified archive copy")
                if c:
                    out.append(c)
    return out


RULES = {"incomplete_download": _rule_partials, "abandoned_temporary": _rule_temp, "stale_preview": _rule_previews,
         "old_job_log": _rule_logs, "intermediate_after_archive": _rule_after_export, "cache_lru": _rule_cache_quota}


def plan(rules: list[str] | None = None, now: float | None = None) -> dict:
    """Candidates of the selected rules, each re-checked against the protection set. Nothing is deleted."""
    now = now or time.time()
    s = settings()
    prot = protected()
    seen, items, refused = set(), [], []
    for r in rules or list(RULES):
        if r not in RULES:
            raise ValueError(f"unknown cleanup rule {r}; have {sorted(RULES)}")
        for c in RULES[r](now, s):
            if c["path"] in seen:
                continue
            seen.add(c["path"])
            try:
                check_deletable(c["path"], prot)
                items.append(c)
            except UnsafePath as e:
                refused.append({**c, "refused": str(e)})
    pid = hashlib.sha256(json.dumps([(i["path"], i["bytes"], round(i["mtime"], 3)) for i in items]).encode()).hexdigest()[:16]
    by_rule: dict[str, int] = {}
    for i in items:
        by_rule[i["rule"]] = by_rule.get(i["rule"], 0) + i["bytes"]
    return {"plan_id": pid, "items": items, "bytes": sum(i["bytes"] for i in items), "by_rule": by_rule,
            "protected_skipped": refused[:200], "protected_skipped_count": len(refused), "rules": rules or list(RULES)}


def execute(plan_id: str, rules: list[str] | None = None) -> dict:
    """Delete exactly the files of a plan the person saw. Refuses if the plan changed (new/used/protected files)."""
    p = plan(rules)
    if p["plan_id"] != plan_id:
        raise ValueError("the cleanup plan changed since it was shown (files were used, created or protected meanwhile); "
                         "review the new plan")
    prot = protected()
    freed, deleted, errors = 0, 0, []
    for it in p["items"]:
        try:
            freed += safe_delete(it["path"], prot)
            deleted += 1
        except (UnsafePath, OSError) as e:
            errors.append(f"{it['path']}: {e}")
    _record("cleanup", {"plan_id": plan_id, "deleted": deleted, "freed": freed, "errors": errors[:20], "by_rule": p["by_rule"]})
    return {"plan_id": plan_id, "deleted": deleted, "freed_bytes": freed, "errors": errors}


def evict_cache(bytes_needed: int = 0) -> dict:
    """LRU eviction of render/media cache down to the quota (or by `bytes_needed`), skipping protected entries."""
    s = settings()
    prot = protected()
    freed = n = 0
    for c in _rule_cache_quota(time.time(), s, bytes_needed):
        if freed >= bytes_needed and bytes_needed:
            break
        try:
            freed += safe_delete(c["path"], prot)
            n += 1
        except (UnsafePath, OSError):
            continue
    if n:
        _record("evict", {"files": n, "freed": freed, "needed": bytes_needed})
    return {"files": n, "freed_bytes": freed}


def rotate_log(path: Path, max_mb: float | None = None, keep: int | None = None) -> bool:
    """Size-based rotation: app.log -> app.log.1 -> ... (keep generations). Returns True when rotated."""
    s = settings()
    max_b = (max_mb if max_mb is not None else s["log_max_mb"]) * 2 ** 20
    keep = keep if keep is not None else int(s["log_keep"])
    path = Path(path)
    if not path.exists() or path.stat().st_size < max_b:
        return False
    for i in range(keep, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        if i == keep:
            src.unlink(missing_ok=True)
        elif src.exists():
            os.replace(src, path.with_name(f"{path.name}.{i + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))
    return True


def _record(kind: str, data: dict) -> None:
    f = config.LOGS / "diskman.jsonl"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(f, 2, 2)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "kind": kind, **data}) + "\n")
    except OSError:
        pass


def report() -> dict:
    """Everything the Storage page shows."""
    u = usage()
    st = status(u)
    arch = {}
    try:
        from . import archive
        arch = archive.summary()
    except Exception as e:  # noqa: BLE001
        arch = {"error": f"{type(e).__name__}: {e}"}
    active = [j for j in _active_jobs() if j["kind"] == "render"]
    return {"usage": u, "status": st, "active_renders": len(active), "archive": arch, "rules": sorted(RULES)}

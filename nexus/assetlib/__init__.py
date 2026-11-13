"""Professional asset library: a manifest-driven catalog of effects, transitions, LUTs, frames, overlays, titles,
templates, motion presets, fonts, sound effects, ambiences and music beds.

* The catalog is nexus/registry/asset_library.yaml plus every transition of nexus/fx/transitions.py.
* Nothing large is installed up front. `install(id)` generates (own work) or downloads (pinned URL + SHA-256, HTTP
  range resume, licence gate) one asset into NEXUS_HOME/assets/<category>/; `installed.json` records its SHA-256.
* `verify`, duplicate detection (by SHA-256), `uninstall` (refused while a timeline uses the file), favourites, user
  import (licence fields required), pack export/import (zip + manifest with checksums), previews (cached),
  missing-asset detection and repair, an LRU quota for re-creatable assets, and `apply` to a clip or the whole timeline
  (a new timeline version, so undo works).
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import yaml

from .. import config

CATEGORY_DIRS = {"effects": "effects", "transitions": "transitions", "luts": "luts", "frames": "frames", "overlays": "overlays",
                 "titles": "titles", "templates": "templates", "animations": "animations", "fonts": "fonts",
                 "sound_effects": "sound_effects", "music": "music"}
EXTRA_DIRS = ("thumbnails", "manifests", "tmp")
LIB_VERSION = 1


class AssetError(RuntimeError):
    pass


class LicenseRefused(AssetError):
    pass


# ------------------------------------------------------------------ paths + state
def root() -> Path:
    return config.ASSETS


def ensure_dirs() -> None:
    for d in list(CATEGORY_DIRS.values()) + list(EXTRA_DIRS):
        (root() / d).mkdir(parents=True, exist_ok=True)


def _state_file(name: str) -> Path:
    return root() / "manifests" / name


def _read(name: str, default):
    f = _state_file(name)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            return default
    return default


def _write(name: str, data) -> None:
    f = _state_file(name)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def installed() -> dict:
    return _read("installed.json", {})


def favorites() -> list[str]:
    return _read("favorites.json", [])


def set_favorite(asset_id: str, on: bool = True) -> list[str]:
    entry(asset_id)
    fav = [x for x in favorites() if x != asset_id] + ([asset_id] if on else [])
    _write("favorites.json", fav)
    return fav


def sha256_file(p: str | Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ catalog
_REG_LOCK = threading.Lock()
_REG_CACHE: dict = {}
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _registry() -> dict:
    """Parsed registry/asset_library.yaml, cached per file version (QA-D3: it was re-parsed for every asset, 17.7 s per
    /api/library). The cache key is (path, mtime_ns, size, sha256 of the bytes), so an edit is always picked up; each
    caller gets its own deep copy, so a caller that mutates the result cannot corrupt the cache. Thread-safe."""
    f = config.REGISTRY_DIR / "asset_library.yaml"
    st = f.stat()
    meta = (str(f), st.st_mtime_ns, st.st_size)
    with _REG_LOCK:
        hit = _REG_CACHE.get("entry")
        if hit is None or hit[0] != meta:
            raw = f.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if hit is None or hit[1] != digest:
                hit = (meta, digest, yaml.load(raw.decode("utf-8"), Loader=_YAML_LOADER))
            else:
                hit = (meta, digest, hit[2])
            _REG_CACHE["entry"] = hit
        data = hit[2]
    return copy.deepcopy(data)


TRANSITION_GROUPS = [  # (category label, predicate) - first match wins; every transition gets one
    ("film burn", lambda n: "burn" in n), ("luma", lambda n: n.startswith("luma")), ("shape", lambda n: n.startswith("shape") or n in ("circlecrop", "rectcrop", "circleopen", "circleclose", "radial")),
    ("spin", lambda n: n == "spin"), ("whip pan", lambda n: n.startswith("whip")), ("zoom", lambda n: "zoom" in n or n.startswith("squeeze")),
    ("blur", lambda n: "blur" in n), ("glitch", lambda n: "glitch" in n or n == "pixelize"),
    ("push", lambda n: n.startswith("cover") or n.startswith("reveal")), ("slide", lambda n: n.startswith("slide") or n.startswith("smooth")),
    ("wipe", lambda n: n.startswith("wipe") or n.startswith("diag") or n.endswith("slice") or n.endswith("wind") or n.startswith(("vert", "horz"))),
    ("dissolve", lambda n: True)]
TRANSITION_DESC = {"fade": "Cross-dissolve.", "fadeblack": "Dip to black.", "fadewhite": "Dip to white (flash).", "dissolve": "Noise dissolve.",
                   "distance": "Distance-field blend.", "fadegrays": "Fade through greyscale.", "fadefast": "Fast crossfade.", "fadeslow": "Slow crossfade.",
                   "pixelize": "Pixelate out and in (glitch).", "hblur": "Blur through.", "spin": "Spins and shrinks out, the next shot spins in.",
                   "whip_pan_left": "Fast motion-blurred pan to the left.", "whip_pan_right": "Fast motion-blurred pan to the right.",
                   "zoom_blur": "Zooms through the frame with radial blur.", "blur_dissolve": "Dissolve through a blur.",
                   "glitch_slices": "Displaced horizontal slices with RGB split.", "film_burn": "Organic burn-through with a hot orange edge.",
                   "luma_radial": "Soft radial luma wipe.", "luma_clouds": "Cloud-shaped luma wipe.", "luma_diagonal": "Soft diagonal luma wipe.",
                   "luma_clock": "Clock-hand sweep.", "luma_ripple": "Rippling circular luma wipe.", "luma_bars": "Wavy vertical bars.",
                   "shape_diamond": "Diamond iris.", "shape_star": "Star iris.", "shape_heart": "Heart iris.", "shape_cross": "Cross iris.",
                   "shape_hexagon": "Hexagon iris."}


def _transition_entries(defaults: dict) -> list[dict]:
    from ..fx import transitions as T
    out = []
    for n in T.ALL:
        group = next(g for g, f in TRANSITION_GROUPS if f(n))
        custom = n in T.CUSTOM
        out.append({**defaults, "id": f"transition.{n}", "name": n.replace("_", " ").capitalize(), "category": "transitions",
                    "kind": "builtin", "group": group, "apply": {"transition": n, "duration": 0.6},
                    "description": TRANSITION_DESC.get(n, f"FFmpeg xfade '{n}' ({group}).") +
                    (" Custom expression (FFmpeg >= 6); falls back to " + T.FALLBACK[n] + " on older FFmpeg." if custom else ""),
                    "source_url": "https://github.com/Ahmedhan1/new-project-money/blob/main/nexus/fx/transitions.py"
                    if custom else "https://ffmpeg.org/ffmpeg-filters.html#xfade"})
    return out


def _entries() -> list[dict]:
    reg = _registry()
    d = reg.get("defaults", {})
    out = []
    for a in reg["assets"]:
        base = d.get(a["kind"], {}) if a["kind"] in ("generated", "builtin") else {}
        out.append({**base, **a})
    out += _transition_entries(d.get("builtin", {}))
    for u in _read("user_assets.json", []):
        out.append({**u, "kind": "user"})
    return out


def policy() -> dict:
    return _registry().get("policy", {})


def optional_future() -> list[dict]:
    return _registry().get("optional_future", [])


def entry(asset_id: str) -> dict:
    for e in _entries():
        if e["id"] == asset_id:
            return e
    raise KeyError(f"unknown asset {asset_id}")


def license_check(e: dict) -> tuple[bool, str]:
    pol = policy()
    lic = e.get("license")
    if not lic:
        return False, "no licence recorded"
    if lic not in pol.get("allowed_licenses", []):
        return False, f"licence {lic} is not in the allowed list"
    if e.get("commercial_use") is not True:
        return False, "commercial use is not confirmed"
    for f in ("author", "source_url", "attribution"):
        if not e.get(f):
            return False, f"missing {f}"
    if lic in pol.get("attribution_required", []) and "none" in str(e.get("attribution", "")).lower():
        return False, f"{lic} requires attribution but none is recorded"
    if e["kind"] in ("download", "pack"):
        if not (e.get("url") and e.get("sha256")):
            return False, "download without a pinned url + sha256"
        if lic in pol.get("bundle_license_text", []) and not (e.get("license_url") and e.get("license_sha256")) and e["kind"] == "download":
            return False, f"{lic} requires the licence text to be kept with the file; no pinned license_url"
    return True, "ok"


def path_for(e: dict) -> Path:
    if e["kind"] == "pack":
        return root() / CATEGORY_DIRS[e["category"]] / e["id"]
    if e["kind"] == "user":
        return Path(e["path"])
    return root() / CATEGORY_DIRS[e["category"]] / f"{e['id']}{e.get('ext', '')}"


def status(e: dict, inst: dict | None = None) -> str:
    inst = installed() if inst is None else inst
    if e["kind"] == "builtin":
        return "built-in"
    rec = inst.get(e["id"])
    if not rec:
        return "not installed"
    files = rec.get("files") or [str(path_for(e))]
    return "installed" if all(Path(f).exists() for f in files) else "missing"


def catalog(category: str | None = None, query: str | None = None, only: str | None = None) -> list[dict]:
    inst, fav = installed(), set(favorites())
    out = []
    q = (query or "").lower().strip()
    for e in _entries():
        if category and e["category"] != category:
            continue
        if q and q not in f"{e['id']} {e['name']} {e.get('description', '')} {e.get('group', '')}".lower():
            continue
        st = status(e, inst)
        if only == "installed" and st not in ("installed", "built-in"):
            continue
        if only == "favorites" and e["id"] not in fav:
            continue
        ok, why = license_check(e)
        rec = inst.get(e["id"], {})
        out.append({k: e.get(k) for k in ("id", "name", "category", "kind", "group", "description", "license", "author",
                                          "source_url", "commercial_use", "attribution", "size", "apply", "params")}
                   | {"status": st, "favorite": e["id"] in fav, "license_ok": ok, "license_note": why,
                      "sha256": rec.get("sha256") or e.get("sha256"), "installed_size": rec.get("size"),
                      "path": rec.get("path"), "files": len(rec.get("files") or []) if rec else 0,
                      "duplicate_of": rec.get("duplicate_of")})
    return out


def summary() -> dict:
    cat = catalog()
    by: dict[str, int] = {}
    for e in cat:
        by[e["category"]] = by.get(e["category"], 0) + 1
    inst = installed()
    return {"assets": len(cat), "by_category": by, "installed": sum(1 for e in cat if e["status"] == "installed"),
            "builtin": sum(1 for e in cat if e["status"] == "built-in"), "installed_bytes": sum(r.get("size", 0) for r in inst.values()),
            "license_ok": sum(1 for e in cat if e["license_ok"]), "license_refused": [e["id"] for e in cat if not e["license_ok"]],
            "quota_bytes": _quota_bytes(), "root": str(root())}


# ------------------------------------------------------------------ install / download
def _preflight(n: int, what: str) -> None:
    try:
        from .. import diskman
        diskman.preflight(n, what, path=root(), evict=True)
    except ImportError:
        pass


def download(url: str, dest: Path, sha256: str, size: int | None = None, progress=None, chunk: int = 1 << 16) -> Path:
    """Resumable download: bytes go to dest.part; a restart continues with an HTTP Range request. The file is moved
    into place only when its SHA-256 matches the pinned value."""
    from .. import costguard
    costguard.check_host(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size and have > size:
        part.unlink()
        have = 0
    if not (size and have == size):
        headers = {"User-Agent": config.USER_AGENT, "Accept": "*/*"}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            mode = "ab" if have and r.status == 206 else "wb"
            if mode == "wb":
                have = 0
            total = (have + int(r.headers.get("Content-Length") or 0)) or size
            with open(part, mode) as f:
                while True:
                    b = r.read(chunk)
                    if not b:
                        break
                    f.write(b)
                    have += len(b)
                    if progress:
                        progress(have, total)
    got = sha256_file(part)
    if got != sha256:
        part.unlink(missing_ok=True)
        raise AssetError(f"checksum mismatch for {url}: got {got[:12]}..., pinned {sha256[:12]}... (file discarded)")
    os.replace(part, dest)
    return dest


def _record(e: dict, files: list[Path], extra: dict | None = None) -> dict:
    inst = installed()
    shas = [sha256_file(f) for f in files]
    size = sum(f.stat().st_size for f in files)
    main_sha = shas[0] if len(shas) == 1 else hashlib.sha256("".join(shas).encode()).hexdigest()
    dup = next((k for k, r in inst.items() if r.get("sha256") == main_sha and k != e["id"]), None)
    rec = {"id": e["id"], "path": str(path_for(e)), "files": [str(f) for f in files], "file_sha256": shas, "sha256": main_sha,
           "size": size, "installed_at": time.time(), "lib_version": LIB_VERSION, "kind": e["kind"], "license": e.get("license"),
           "source_url": e.get("source_url"), "duplicate_of": dup, **(extra or {})}
    inst[e["id"]] = rec
    _write("installed.json", inst)
    return rec


def install(asset_id: str, progress=None) -> dict:
    """Install one asset (idempotent). Builtins need nothing; generated assets are made here; downloads are pinned."""
    e = entry(asset_id)
    ok, why = license_check(e)
    if not ok:
        raise LicenseRefused(f"{asset_id}: {why}")
    if e["kind"] in ("builtin", "user"):
        return {"id": asset_id, "status": status(e)}
    ensure_dirs()
    if status(e) == "installed":
        return installed()[asset_id]
    dest = path_for(e)
    if e["kind"] == "generated":
        from . import generators
        _preflight(64 * 2 ** 20, f"generating {asset_id}")
        tmp = dest.with_name(f".{dest.stem}.{os.getpid()}.tmp{dest.suffix}")
        try:
            generators.GENERATORS[e["gen"]](tmp, e.get("params") or {})
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return _record(e, [dest], {"generator": e["gen"], "params": e.get("params")})
    if e["kind"] == "download":
        _preflight(int(e.get("size") or 0) * 2 + 2 ** 20, f"downloading {asset_id}")
        download(e["url"], dest, e["sha256"], e.get("size"), progress)
        files = [dest]
        if e.get("license_url"):
            lic = dest.with_name(f"{e['id']}.LICENSE.txt")
            download(e["license_url"], lic, e["license_sha256"])
            files.append(lic)
        return _record(e, files)
    if e["kind"] == "pack":
        _preflight(int(e.get("size") or 0) * 4 + 2 ** 20, f"downloading {asset_id}")
        z = root() / "tmp" / f"{e['id']}.zip"
        download(e["url"], z, e["sha256"], e.get("size"), progress)
        files = _extract(z, dest, e.get("members", "*"))
        z.unlink(missing_ok=True)  # the extracted members are what is used; the archive is not kept
        return _record(e, files, {"members": [f.name for f in files]})
    raise AssetError(f"cannot install kind {e['kind']}")


def _extract(z: Path, dest: Path, pattern: str) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    with zipfile.ZipFile(z) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name  # flattened: no directories, so no path traversal
            if not name or name.startswith(".") or not fnmatch.fnmatch(name.lower(), pattern.lower()):
                continue
            if "/" in info.filename and info.filename.split("/")[0] in ("__MACOSX",):
                continue
            target = dest / name
            with zf.open(info) as src, open(target, "wb") as f:
                shutil.copyfileobj(src, f)
            out.append(target)
    if not out:
        raise AssetError(f"no members matching {pattern} in {z.name}")
    return sorted(out)


def verify(asset_id: str) -> dict:
    e = entry(asset_id)
    if e["kind"] == "builtin":
        return {"id": asset_id, "ok": True, "detail": "built-in"}
    rec = installed().get(asset_id)
    if not rec:
        return {"id": asset_id, "ok": False, "detail": "not installed"}
    bad = []
    for f, s in zip(rec["files"], rec["file_sha256"]):
        if not Path(f).exists():
            bad.append(f"{Path(f).name}: missing")
        elif sha256_file(f) != s:
            bad.append(f"{Path(f).name}: SHA-256 changed")
    if e["kind"] == "download" and rec["file_sha256"][0] != e["sha256"]:
        bad.append("does not match the pinned SHA-256")
    return {"id": asset_id, "ok": not bad, "detail": "; ".join(bad) or "SHA-256 verified"}


def verify_all() -> dict:
    res = [verify(k) for k in installed()]
    return {"checked": len(res), "ok": sum(r["ok"] for r in res), "failed": [r for r in res if not r["ok"]]}


def installed_files() -> list[str]:
    return [f for r in installed().values() for f in r.get("files", [])] + [str(_state_file(n)) for n in
                                                                            ("installed.json", "favorites.json", "user_assets.json")]


def _references() -> dict[str, list[str]]:
    """{absolute file path: [project ids whose timelines use it]} for files inside the asset library."""
    refs: dict[str, list[str]] = {}
    lib = str(root().resolve())
    if not config.PROJECTS.exists():
        return refs
    for p in config.PROJECTS.iterdir():
        for f in (p / "timelines").glob("*.json") if (p / "timelines").exists() else []:
            try:
                txt = f.read_text(encoding="utf-8")
            except OSError:
                continue
            if "assets" not in txt:
                continue
            for s in _strings(json.loads(txt)):
                try:
                    rp = str(Path(s).resolve())
                except (OSError, ValueError):
                    continue
                if rp.startswith(lib):
                    refs.setdefault(rp, [])
                    if p.name not in refs[rp]:
                        refs[rp].append(p.name)
    return refs


def _strings(x):
    if isinstance(x, str):
        if len(x) < 1024 and ("/" in x or "\\" in x):
            yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def uninstall(asset_id: str, force: bool = False) -> dict:
    e = entry(asset_id)
    if e["kind"] == "builtin":
        raise AssetError("built-in assets are part of the renderer")
    inst = installed()
    rec = inst.get(asset_id)
    if not rec:
        return {"id": asset_id, "removed": 0}
    refs = _references()
    used = sorted({pid for f in rec["files"] for pid in refs.get(str(Path(f).resolve()), [])})
    if used and not force:
        raise AssetError(f"{asset_id} is used by project(s) {', '.join(used)}; remove it from their timelines first")
    n = 0
    lib = root().resolve()
    for f in rec["files"]:
        p = Path(f).resolve()
        if lib in p.parents and p.is_file():  # only files inside the library, only the ones this install wrote
            p.unlink()
            n += 1
    if e["kind"] == "pack":
        try:
            path_for(e).rmdir()
        except OSError:
            pass
    del inst[asset_id]
    _write("installed.json", inst)
    if e["kind"] == "user":
        _write("user_assets.json", [u for u in _read("user_assets.json", []) if u["id"] != asset_id])
    return {"id": asset_id, "removed": n}


# ------------------------------------------------------------------ quota (re-creatable assets only)
def _quota_bytes() -> int:
    try:
        from .. import diskman
        return int(diskman.settings()["asset_cache_quota_gb"] * 2 ** 30)
    except Exception:  # noqa: BLE001
        return 10 * 2 ** 30


def enforce_quota(quota_bytes: int | None = None) -> dict:
    """Evict least recently used generated/downloaded assets above the quota. Never: user imports, favourites, assets
    a timeline uses. Evicted assets are re-installed on demand (generated again or downloaded and re-verified)."""
    q = _quota_bytes() if quota_bytes is None else quota_bytes
    inst = installed()
    total = sum(r.get("size", 0) for r in inst.values())
    if total <= q:
        return {"evicted": [], "bytes": total, "quota": q}
    fav, refs = set(favorites()), _references()
    cands = []
    for k, r in inst.items():
        if r.get("kind") not in ("generated", "download", "pack") or k in fav:
            continue
        if any(str(Path(f).resolve()) in refs for f in r["files"]):
            continue
        used = max((Path(f).stat().st_mtime for f in r["files"] if Path(f).exists()), default=0)
        cands.append((r.get("last_used", used), k, r.get("size", 0)))
    ev = []
    for _t, k, size in sorted(cands):
        if total <= q:
            break
        uninstall(k)
        total -= size
        ev.append(k)
    return {"evicted": ev, "bytes": total, "quota": q}


def _touch(asset_id: str) -> None:
    inst = installed()
    if asset_id in inst:
        inst[asset_id]["last_used"] = time.time()
        _write("installed.json", inst)


# ------------------------------------------------------------------ user import / packs
def import_file(src: str | Path, *, category: str, name: str, license: str, author: str, source_url: str,
                commercial_use: bool, attribution: str, description: str = "") -> dict:
    """Bring in a file the user owns or has a licence for. Licence fields are required and checked; the same bytes
    imported twice are one asset."""
    src = Path(src)
    if category not in CATEGORY_DIRS:
        raise AssetError(f"unknown category {category}")
    if not src.is_file():
        raise AssetError(f"not a file: {src}")
    meta = {"name": name, "category": category, "license": license, "author": author, "source_url": source_url,
            "commercial_use": commercial_use, "attribution": attribution, "description": description, "kind": "user"}
    ok, why = license_check({**meta, "id": "user.check"})
    if not ok:
        raise LicenseRefused(why)
    sha = sha256_file(src)
    users = _read("user_assets.json", [])
    for u in users:
        if u["sha256"] == sha:
            return {**u, "duplicate": True}
    for k, r in installed().items():
        if r.get("sha256") == sha:
            return {"id": k, "duplicate": True, "duplicate_of": k}
    ensure_dirs()
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in src.name)[-80:]
    dest = root() / CATEGORY_DIRS[category] / f"user_{sha[:10]}_{safe}"
    _preflight(src.stat().st_size, f"importing {src.name}")
    shutil.copyfile(src, dest)
    u = {**meta, "id": f"user.{sha[:12]}", "path": str(dest), "sha256": sha, "size": dest.stat().st_size, "ext": dest.suffix}
    users.append(u)
    _write("user_assets.json", users)
    _record({**u, "kind": "user"}, [dest])
    return u


def export_pack(asset_ids: list[str], dest: str | Path) -> dict:
    """A zip with the files and a manifest (licence, author, source, SHA-256 per file). Builtins export their preset."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    items = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for aid in asset_ids:
            e = entry(aid)
            meta = {k: e.get(k) for k in ("id", "name", "category", "kind", "license", "author", "source_url", "commercial_use",
                                          "attribution", "description", "apply", "params")}
            files = []
            if e["kind"] != "builtin":
                rec = installed().get(aid) or install(aid)
                for f, s in zip(rec["files"], rec["file_sha256"]):
                    arc = f"files/{aid}/{Path(f).name}"
                    z.write(f, arc)
                    files.append({"path": arc, "sha256": s})
            items.append({**meta, "files": files})
        z.writestr("manifest.json", json.dumps({"format": "nexus-asset-pack/1", "created": time.time(), "assets": items}, indent=1))
    return {"path": str(dest), "assets": len(items), "sha256": sha256_file(dest)}


def import_pack(src: str | Path) -> dict:
    """Import a pack made by export_pack: every file is checked against the manifest's SHA-256 and licence."""
    src = Path(src)
    added, skipped = [], []
    tmpd = root() / "tmp" / f"pack_{os.getpid()}_{int(time.time() * 1000)}"
    tmpd.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(src) as z:
            man = json.loads(z.read("manifest.json"))
            if man.get("format") != "nexus-asset-pack/1":
                raise AssetError("not a NEXUS asset pack")
            for a in man["assets"]:
                if a["kind"] == "builtin" or not a["files"]:
                    skipped.append({"id": a["id"], "why": "built-in preset (already part of NEXUS)"})
                    continue
                for fi in a["files"]:
                    name = Path(fi["path"]).name
                    t = tmpd / name
                    with z.open(fi["path"]) as s, open(t, "wb") as f:
                        shutil.copyfileobj(s, f)
                    if sha256_file(t) != fi["sha256"]:
                        raise AssetError(f"{fi['path']}: SHA-256 does not match the pack manifest")
                    if name.endswith(".LICENSE.txt"):
                        continue
                    try:
                        u = import_file(t, category=a["category"], name=a["name"], license=a["license"], author=a["author"],
                                        source_url=a["source_url"], commercial_use=bool(a["commercial_use"]),
                                        attribution=a["attribution"], description=a.get("description") or "")
                        (skipped if u.get("duplicate") else added).append({"id": u["id"], "why": "duplicate" if u.get("duplicate") else ""})
                    except LicenseRefused as e:
                        skipped.append({"id": a["id"], "why": f"licence refused: {e}"})
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    return {"added": added, "skipped": skipped}


# ------------------------------------------------------------------ missing assets
def missing() -> list[dict]:
    """Library files that a project timeline uses but that are not on disk (deleted, evicted, or a restored project)."""
    out = []
    by_path = {}
    for k, r in installed().items():
        for f in r.get("files", []):
            by_path[str(Path(f).resolve())] = k
    lib = root().resolve()
    for e in _entries():
        if e["kind"] in ("generated", "download", "pack"):
            by_path.setdefault(str(path_for(e).resolve()), e["id"])
    for rp, pids in _references().items():
        if not Path(rp).exists():
            aid = by_path.get(rp)
            if aid is None:  # pack member: match the pack directory
                aid = next((k for k, r in installed().items() if Path(rp).parent == Path(r["path"]).resolve()), None)
            out.append({"path": rp, "projects": pids, "asset_id": aid, "repairable": bool(aid) and lib in Path(rp).parents})
    return out


def repair_missing() -> dict:
    fixed, failed = [], []
    for m in missing():
        if not m["repairable"]:
            failed.append({**m, "why": "not a catalog asset"})
            continue
        inst = installed()
        inst.pop(m["asset_id"], None)
        _write("installed.json", inst)
        try:
            install(m["asset_id"])
            fixed.append(m["asset_id"])
        except Exception as e:  # noqa: BLE001
            failed.append({**m, "why": f"{type(e).__name__}: {e}"})
    return {"fixed": sorted(set(fixed)), "failed": failed}


# ------------------------------------------------------------------ apply to clip / timeline
def _file_for(e: dict, member: str | None = None) -> str:
    rec = installed().get(e["id"]) or install(e["id"])
    if e["kind"] == "pack":
        fs = rec["files"]
        if member:
            m = [f for f in fs if Path(f).name == member]
            if not m:
                raise AssetError(f"{member} is not in {e['id']}")
            return m[0]
        return fs[0]
    return rec["files"][0]


def _starts(tl: dict) -> list[float]:
    """Programme start time of every storyline clip (crossfades overlap the previous clip)."""
    from ..timeline.render import clip_duration
    out, t = [], 0.0
    for i, c in enumerate(tl.get("clips", [])):
        if i and c.get("transition") == "crossfade":
            t -= float(c.get("transition_dur", 0.5))
        out.append(round(max(0.0, t), 3))
        t += clip_duration(c)
    return out


def _abs_kf(points: list, dur: float) -> list:
    return [[dur if p[0] == -1 else p[0], p[1], *(p[2:] or ["linear"])] for p in points]


def apply_to_timeline(tl: dict, asset_id: str, target: str = "clip", clip_id: str | None = None, at: float | None = None,
                      member: str | None = None, params: dict | None = None) -> tuple[dict, str]:
    """Return (new timeline, note). target: 'clip' (needs clip_id) or 'timeline' (every clip / programme-wide)."""
    from ..timeline import ops
    from ..timeline.render import clip_duration
    e = entry(asset_id)
    ap = {**(e.get("apply") or {}), **(params or {})}
    clips = tl.get("clips", [])
    if target == "clip":
        if not clip_id:
            raise AssetError("choose a clip first")
        idx = [ops.find(tl, clip_id)]
    else:
        idx = list(range(len(clips)))
    note = f"asset {asset_id} -> {'clip ' + clip_id if target == 'clip' else 'timeline'}"
    cat = e["category"]
    if "effect" in ap:
        for i in idx:
            c = clips[i]
            c["effects"] = [x for x in c.get("effects", []) if x.get("type") != ap["effect"]["type"]] + [dict(ap["effect"])]
    elif "process" in ap:
        raise AssetError(f"{e['name']} is a VFX processor: use 'Process this clip' in the clip inspector")
    elif "transition" in ap:
        for i in idx:
            if i == 0:
                continue
            clips[i].update(transition="crossfade", xfade=ap["transition"], transition_dur=float(ap.get("duration", 0.6)))
    elif "keyframes" in ap:
        for i in idx:
            d = clip_duration(clips[i])
            clips[i]["keyframes"] = {**clips[i].get("keyframes", {}), **{k: _abs_kf(v, d) for k, v in ap["keyframes"].items()}}
    elif "title_anim" in ap:
        n = 0
        for i in idx:
            for t in clips[i].get("titles") or []:
                t["anim"] = ap["title_anim"]
                n += 1
        if not n:
            raise AssetError("the selected clip(s) have no on-screen title to animate; add a title first")
    elif "captions" in ap:
        tl["captions"] = {**(tl.get("captions") or {}), **ap["captions"]}
    elif cat == "luts":
        path = _file_for(e)
        if target == "timeline":
            tl["lut"] = path
        else:
            clips[idx[0]]["lut"] = path
    elif cat in ("frames", "overlays") or (cat == "titles" and e.get("gen") == "overlay"):
        path = _file_for(e)
        st = float(at or 0.0)
        ov = {"type": "image", "src": path, "start": st, "end": float(ap.get("end", tl.get("duration") or 1e9)), "asset": asset_id}
        if target == "clip":
            starts = _starts(tl)
            i = idx[0]
            ov["start"], ov["end"] = starts[i], starts[i] + clip_duration(clips[i])
        tl["overlays"] = [o for o in tl.get("overlays") or [] if o.get("asset") != asset_id] + [ov]
    elif cat in ("sound_effects", "music"):
        path = _file_for(e, member)
        if cat == "music":
            tl["music"] = {**(tl.get("music") or {}), "src": path, "volume": (tl.get("music") or {}).get("volume", 0.18),
                           "duck": (tl.get("music") or {}).get("duck", "keyframes")}
        else:
            st = float(at) if at is not None else (_starts(tl)[idx[0]] if target == "clip" else 0.0)
            role = "ambience" if asset_id.startswith("ambience.") else "sfx"
            item = {"src": path, "start": st, "role": role, "volume": 0.25 if role == "ambience" else 0.8, "asset": asset_id}
            if role == "ambience" and target == "timeline":
                item["duration"] = float(tl.get("duration") or 30)
            tl["audio"] = tl.get("audio", []) + [item]
    elif cat == "fonts":
        path = _file_for(e)
        n = 0
        for i in idx:
            for t in clips[i].get("titles") or []:
                t["font"] = path
                n += 1
        if not n:
            raise AssetError("no on-screen titles on the selected clip(s) to set this font on")
    elif cat in ("templates", "titles"):
        tpl = json.loads(Path(_file_for(e)).read_text(encoding="utf-8"))
        i = idx[0] if target == "clip" else 0
        if tpl.get("reframe"):
            tl = ops.reframe(tl, **tpl["reframe"])
            clips = tl["clips"]
        if tpl.get("captions"):
            tl["captions"] = {**(tl.get("captions") or {}), **tpl["captions"]}
        if tpl.get("insert_color_clip") or tpl.get("append_color_clip"):
            spec = tpl.get("insert_color_clip") or tpl.get("append_color_clip")
            cc = ops.color_clip(float(spec["duration"]), spec.get("color", "0x0b1020"))
            if tpl.get("titles"):
                cc["titles"] = json.loads(json.dumps(tpl["titles"]))
            if tpl.get("credits"):
                cc["credits"] = tpl["credits"]
            tl = ops.insert(tl, cc, i if tpl.get("insert_color_clip") else None)
        elif tpl.get("titles"):
            clips[i]["titles"] = (clips[i].get("titles") or []) + json.loads(json.dumps(tpl["titles"]))
        if tpl.get("overlay"):
            tl, _ = apply_to_timeline(tl, tpl["overlay"], "timeline" if target == "timeline" or tpl.get("reframe") else "clip",
                                      clip_id=(tl["clips"][i]["id"] if target == "clip" and not tpl.get("insert_color_clip") else None) or clip_id)
    else:
        raise AssetError(f"don't know how to apply {asset_id}")
    _touch(asset_id)
    return tl, note


def apply(project_id: str, asset_id: str, target: str = "clip", clip_id: str | None = None, branch: str = "main",
          at: float | None = None, member: str | None = None, params: dict | None = None) -> dict:
    from .. import project as prj
    p = prj.Project(project_id)
    tl = p.timeline(branch)
    tl, note = apply_to_timeline(tl, asset_id, target, clip_id, at, member, params)
    vid = p.save_timeline(tl, note, branch)
    return {"version": vid, "note": note}


from .preview import preview  # noqa: E402,F401  (re-exported)

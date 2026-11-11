"""Asset discovery + licence ledger. Every downloaded asset gets a provenance record before it can be used."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from . import config, costguard, db, net

# Only licences that allow commercial use AND modification are accepted.
OK_LICENSES = {"cc0", "pdm", "by", "by-sa"}


def _attribution(title: str, creator: str, lic: str, ver: str, url: str) -> str:
    if lic in ("cc0", "pdm"):
        return f"\"{title}\" - public domain ({lic.upper()}). Source: {url}"
    return f"\"{title}\" by {creator or 'unknown'}, licensed CC {lic.upper()} {ver}. Source: {url}"


def search_images(query: str, n: int = 5, min_width: int = 1000) -> list[dict]:
    """Search Openverse for commercially usable, modifiable landscape images."""
    costguard.authorize("openverse", "search")
    data = net.get_json("https://api.openverse.org/v1/images/", {
        "q": query, "license_type": "commercial,modification", "page_size": min(20, max(n * 3, 10)),
        "aspect_ratio": "wide", "mature": "false", "source": "wikimedia"}, timeout=20, retries=1)  # files on upload.wikimedia.org (declared host)
    out = []
    for r in data.get("results", []):
        lic = (r.get("license") or "").lower()
        if lic not in OK_LICENSES or (r.get("width") or 0) < min_width:
            continue
        out.append({"id": f"openverse:{r['id']}", "title": r.get("title") or query, "url": r.get("foreign_landing_url"),
                    "file_url": r.get("url"), "creator": r.get("creator") or "", "source": r.get("source") or r.get("provider"),
                    "license": lic, "license_version": r.get("license_version") or "", "license_url": r.get("license_url"),
                    "width": r.get("width"), "height": r.get("height"), "kind": "image"})
        if len(out) >= n:
            break
    return out


def _strip_html(x: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x or "")).strip()


def search_commons(query: str, n: int = 5, min_width: int = 1000) -> list[dict]:
    """Search Wikimedia Commons directly; licence comes from each file's own metadata."""
    costguard.authorize("wikimedia-commons", "search")
    data = net.get_json("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": min(20, n * 4), "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime",
        "iiurlwidth": 1920}, timeout=20, retries=2)
    out = []
    pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        m = ii.get("extmetadata", {})
        short = (m.get("LicenseShortName", {}).get("value") or "").lower()
        lic = ("cc0" if "cc0" in short else "pdm" if "public domain" in short or short.startswith("pd") else
               "by-sa" if "by-sa" in short else "by" if re.match(r"cc[ -]by[ -]?\d", short) else None)
        w, h = ii.get("width") or 0, ii.get("height") or 1
        if lic is None or ii.get("mime") not in ("image/jpeg", "image/png") or w < min_width or w / h < 1.2:
            continue
        ver = (re.search(r"(\d\.\d)", short) or [None, ""])[1]
        out.append({"id": f"commons:{p['pageid']}", "title": p["title"].removeprefix("File:").rsplit(".", 1)[0],
                    "url": ii.get("descriptionurl"), "file_url": ii.get("thumburl") or ii.get("url"),
                    "creator": _strip_html(m.get("Artist", {}).get("value", ""))[:120], "source": "wikimedia-commons",
                    "license": lic, "license_version": ver, "license_url": m.get("LicenseUrl", {}).get("value"),
                    "width": w, "height": h, "kind": "image"})
        if len(out) >= n:
            break
    return out


def acquire(candidate: dict) -> dict | None:
    """Download a search result and write its licence record. Returns the asset row, or None if the host is not allowed."""
    try:
        costguard.check_host(candidate["file_url"])
    except costguard.CostGuardError:
        return None  # file is hosted somewhere we have not declared as zero-cost: skip, never bypass
    ext = (re.search(r"\.(jpe?g|png|webp|gif|tiff?|svg)(?:\?|$)", candidate["file_url"], re.I) or [None, "jpg"])[1].lower()
    aid = candidate["id"].replace(":", "_")
    dest = config.ASSETS / candidate["kind"] / f"{aid}.{ext}"
    if not dest.exists():
        try:
            net.download(candidate["file_url"], dest, timeout=30, retries=2)
        except net.RemoteUnavailable:
            return None
    sha = hashlib.sha256(dest.read_bytes()).hexdigest()
    lic = candidate["license"]
    row = {"id": candidate["id"], "title": candidate["title"], "url": candidate["url"], "creator": candidate["creator"],
           "source": candidate["source"], "license": f"CC {lic.upper()} {candidate['license_version']}".strip(),
           "license_url": candidate["license_url"],
           "attribution": _attribution(candidate["title"], candidate["creator"], lic, candidate["license_version"], candidate["url"]),
           "commercial": 1, "restrictions": "share-alike applies to adaptations" if lic == "by-sa" else
           ("attribution required" if lic == "by" else "none"),
           "downloaded": time.time(), "path": str(dest), "sha256": sha, "kind": candidate["kind"]}
    db.execute("INSERT OR REPLACE INTO assets VALUES(:id,:title,:url,:creator,:source,:license,:license_url,:attribution,"
               ":commercial,:restrictions,:downloaded,:path,:sha256,:kind)", row)
    return row


def record_use(asset_id: str, project_id: str) -> None:
    db.execute("INSERT INTO asset_usage VALUES(?,?,?)", (asset_id, project_id, time.time()))


def find_image(query: str, project_id: str | None = None, exclude: set[str] | None = None) -> dict | None:
    """Best usable image for a query (downloads it). None when nothing suitable/reachable is found."""
    cands = []
    for search in (search_commons, search_images):  # Commons first (authoritative licence data), Openverse second
        try:
            cands = search(query)
        except (net.RemoteUnavailable, costguard.CostGuardError, ValueError):
            cands = []
        if cands:
            break
    for c in cands:
        if exclude and c["id"] in exclude:
            continue
        row = acquire(c)
        if row and _usable_image(row["path"]):
            if project_id:
                record_use(row["id"], project_id)
            return row
    return None


def _usable_image(path: str) -> bool:
    from PIL import Image
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            return im.size[0] >= 800 and im.format in ("JPEG", "PNG", "WEBP")
    except Exception:
        return False


def credits(project_id: str) -> list[str]:
    rows = db.query("SELECT DISTINCT a.attribution FROM assets a JOIN asset_usage u ON a.id=u.asset_id WHERE u.project_id=?", (project_id,))
    return [r["attribution"] for r in rows]


def library() -> list[dict]:
    return db.query("SELECT a.*, (SELECT COUNT(*) FROM asset_usage u WHERE u.asset_id=a.id) uses FROM assets a ORDER BY downloaded DESC")

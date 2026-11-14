"""Free video library: search public-domain and openly licensed VIDEOS to edit, with their licence and credit.

Sources (all official public APIs, no account, no key, nothing billable):
  * NASA Image and Video Library  - public domain (NASA media; no endorsement implied)
  * Internet Archive (archive.org) - only items whose licence URL is Creative Commons or Public Domain Mark/CC0
  * Wikimedia Commons             - video files with their own licence metadata (CC BY / BY-SA / PD ...)
This is the lawful replacement for "download from any site": NEXUS never scrapes YouTube/TikTok/... pages (their terms
forbid it, and re-uploading other people's videos gets channels removed). Each result carries `license`, `credit` and
`page`; the import records them on the project's media so the credits can go in the description.
"""
from __future__ import annotations

import re
import urllib.parse

from . import net

UA_SAFE = re.compile(r"[^\w\s\-:.,'()&/]", re.U)


def _clean(text: str, n: int = 300) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text or ""))).strip()[:n]


def search_nasa(query: str, n: int = 12) -> list[dict]:
    from . import broll
    out = []
    for it in broll.search_nasa(query, n * 3):
        if it["kind"] != "video":
            continue
        out.append({"id": it["id"], "source": "NASA", "title": it["title"], "description": it["description"][:300],
                    "thumb": it.get("preview_url") if str(it.get("preview_url") or "").endswith(".jpg") else None,
                    "file_url": it.get("hd_url") or it["file_url"], "page": it["page"], "license": it["license"],
                    "credit": it["credit"], "rights_note": it.get("rights_note")})
        if len(out) >= n:
            break
    return out


ARCHIVE_OK = re.compile(r"creativecommons\.org/(licenses|publicdomain)/|publicdomain", re.I)


def _archive_file(ident: str) -> str | None:
    meta = net.get_json(f"https://archive.org/metadata/{urllib.parse.quote(ident)}", timeout=25, retries=1)
    files = meta.get("files") or []
    mp4 = [f for f in files if str(f.get("name", "")).lower().endswith(".mp4")]
    # prefer the derivative h.264 copy (small, plays everywhere), else the biggest mp4
    mp4.sort(key=lambda f: (str(f.get("format", "")).lower() not in ("h.264", "mpeg4", "h.264 ia"), -int(f.get("size") or 0)))
    if not mp4:
        return None
    return f"https://archive.org/download/{urllib.parse.quote(ident)}/{urllib.parse.quote(mp4[0]['name'])}"


def search_archive(query: str, n: int = 10) -> list[dict]:
    q = f"({query}) AND mediatype:movies AND licenseurl:*"
    qs = urllib.parse.urlencode([("q", q), *[("fl[]", f) for f in ("identifier", "title", "licenseurl", "description", "creator")],
                                 ("rows", n * 3), ("output", "json"), ("sort[]", "downloads desc")])
    d = net.get_json(f"https://archive.org/advancedsearch.php?{qs}", timeout=25, retries=1)
    out = []
    for doc in (d.get("response") or {}).get("docs") or []:
        lic = doc.get("licenseurl") or ""
        if isinstance(lic, list):
            lic = lic[0] if lic else ""
        if not ARCHIVE_OK.search(lic):
            continue
        ident = doc["identifier"]
        title = doc.get("title") if isinstance(doc.get("title"), str) else (doc.get("title") or [ident])[0]
        creator = doc.get("creator") if isinstance(doc.get("creator"), str) else ", ".join(doc.get("creator") or [])
        out.append({"id": f"archive:{ident}", "source": "Internet Archive", "title": _clean(title, 120),
                    "description": _clean(doc.get("description"), 300), "thumb": f"https://archive.org/services/img/{ident}",
                    "file_url": None, "ident": ident, "page": f"https://archive.org/details/{ident}", "license": lic,
                    "credit": f"{creator or 'Unknown'} - {_clean(title, 80)} (archive.org, {lic})", "rights_note": None})
        if len(out) >= n:
            break
    return out


def search_commons(query: str, n: int = 10) -> list[dict]:
    d = net.get_json("https://commons.wikimedia.org/w/api.php",
                     {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                      "gsrsearch": f"{query} filetype:video", "gsrlimit": n * 2, "prop": "imageinfo",
                      "iiprop": "url|extmetadata|mime", "iiurlwidth": 320}, timeout=25, retries=1)
    out = []
    for pg in ((d.get("query") or {}).get("pages") or {}).values():
        ii = (pg.get("imageinfo") or [{}])[0]
        if not str(ii.get("mime", "")).startswith("video/"):
            continue
        em = ii.get("extmetadata") or {}
        lic = _clean((em.get("LicenseShortName") or {}).get("value"), 60)
        if not lic or "fair use" in lic.lower() or "non-free" in lic.lower():
            continue
        artist = _clean((em.get("Artist") or {}).get("value"), 80)
        title = pg.get("title", "").removeprefix("File:")
        out.append({"id": f"commons:{pg.get('pageid')}", "source": "Wikimedia Commons", "title": title,
                    "description": _clean((em.get("ImageDescription") or {}).get("value"), 300),
                    "thumb": ii.get("thumburl"), "file_url": ii.get("url"), "page": ii.get("descriptionurl"),
                    "license": lic, "credit": f"{artist or 'Wikimedia Commons'} - {title} ({lic})", "rights_note": None})
        if len(out) >= n:
            break
    return out


SOURCES = {"nasa": search_nasa, "archive": search_archive, "commons": search_commons}


def search(query: str, sources: list[str] | None = None, n: int = 10) -> dict:
    """{"results": [...], "errors": {source: message}} - one source being down never hides the others."""
    query = query.strip()[:120]
    res, errors = [], {}
    for name in sources or list(SOURCES):
        fn = SOURCES.get(name)
        if not fn:
            continue
        try:
            res += fn(query, n)
        except Exception as e:  # noqa: BLE001
            errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
    return {"results": res, "errors": errors}


def resolve(item: dict) -> str:
    """The direct file URL to import (Internet Archive needs one metadata lookup)."""
    if item.get("file_url"):
        return item["file_url"]
    if item.get("ident"):
        url = _archive_file(item["ident"])
        if url:
            return url
    raise ValueError("this item has no downloadable video file")

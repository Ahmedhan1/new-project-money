"""Fetch the Account 2 benchmark set (scripts/benchmarks/media.yaml) into $NEXUS_HOME/benchmarks/<id>/source.mp4.

Usage: python scripts/benchmarks/fetch_media.py [id ...]
Licensed/public-domain sources only; the licence is read from the source for every item and written to provenance.json.
Full renditions are downloaded to a temp file, cut to max_s with a stream copy, and the temp file is deleted.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from nexus import config  # noqa: E402

UA = config.USER_AGENT


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def nasa(item: dict) -> tuple[str, dict]:
    nid = item["nasa_id"]
    files = json.loads(_get(f"https://images-assets.nasa.gov/video/{urllib.parse.quote(nid)}/collection.json"))
    url = next(f for f in files if f.endswith(f"~{item.get('rendition', 'small')}.mp4")).replace("http://", "https://")
    meta_url = next((f for f in files if f.endswith("metadata.json")), None)
    meta = json.loads(_get(urllib.parse.quote(meta_url.replace("http://", "https://"), safe=":/~"))) if meta_url else {}
    url = urllib.parse.quote(url, safe=":/~")
    return url, {"source": "NASA Image and Video Library", "id": nid, "page": f"https://images.nasa.gov/details/{urllib.parse.quote(nid)}",
                 "title": meta.get("AVAIL:Title") or nid, "center": meta.get("AVAIL:Center"), "date": meta.get("AVAIL:DateCreated"),
                 "license": "NASA media - generally not copyrighted in the US (NASA media usage guidelines apply)",
                 "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/", "commercial_use": True,
                 "restrictions": "no implied NASA endorsement; NASA insignia/logos restricted; third-party material inside a video may differ"}


def archive(item: dict) -> tuple[str, dict]:
    aid = item["archive_id"]
    md = json.loads(_get(f"https://archive.org/metadata/{aid}"))
    lic = (md.get("metadata") or {}).get("licenseurl") or ""
    if "publicdomain" not in lic and "/zero/" not in lic:
        raise RuntimeError(f"{aid}: licence {lic!r} is not public domain - refused")
    mp4 = [f["name"] for f in md["files"] if f["name"].endswith(".mp4")]
    name = next((n for n in mp4 if n.endswith("_512kb.mp4")), None) or sorted(mp4, key=len)[0]
    return (f"https://archive.org/download/{aid}/{urllib.parse.quote(name)}",
            {"source": "Internet Archive - Prelinger Archives", "id": aid, "page": f"https://archive.org/details/{aid}",
             "title": md["metadata"].get("title"), "date": md["metadata"].get("year") or md["metadata"].get("date"),
             "license": "Public Domain (item licenseurl)", "license_url": lic, "commercial_use": True, "restrictions": "none stated"})


def _download_prefix(url: str, tmp: Path, need_s: float) -> None:
    """Download only as much of the file as the cut needs when the MP4 index (moov) comes first; else the whole file."""
    head = tmp.with_suffix(".head")
    subprocess.run(["curl", "-sSL", "--fail", "-A", UA, "-r", "0-4000000", "-o", str(head), url], check=True)
    size = int(subprocess.run(["curl", "-sSIL", "-A", UA, url], capture_output=True, text=True).stdout.lower()
               .rsplit("content-length:", 1)[-1].split()[0])
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(head)], capture_output=True, text=True)
    head.unlink()
    try:
        dur = float(pr.stdout.strip())
    except ValueError:
        dur = 0.0  # index at the end of the file: a prefix is useless
    if dur > need_s * 1.2:
        end = int(size * min(1.0, need_s / dur * 1.15 + 0.02))
        subprocess.run(["curl", "-sSL", "--fail", "-A", UA, "-r", f"0-{end}", "-o", str(tmp), url], check=True)
    else:
        subprocess.run(["curl", "-sSL", "--fail", "-A", UA, "-o", str(tmp), url], check=True)


def fetch(item: dict, out_dir: Path) -> dict:
    d = out_dir / item["id"]
    d.mkdir(parents=True, exist_ok=True)
    src = d / "source.mp4"
    if src.exists() and (d / "provenance.json").exists():
        return json.loads((d / "provenance.json").read_text())
    url, prov = (nasa if item["source"] == "nasa" else archive)(item)
    tmp = d / "download.tmp"
    t0 = time.time()
    _download_prefix(url, tmp, item.get("start_s", 0) + item["max_s"])
    dl_s = time.time() - t0
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if item.get("start_s"):
        cmd += ["-ss", str(item["start_s"])]
    cmd += ["-i", str(tmp), "-t", str(item["max_s"]), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(src)]
    subprocess.run(cmd, check=True)
    size_dl = tmp.stat().st_size
    tmp.unlink()
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(src)],
                                      capture_output=True, text=True, check=True).stdout)
    v = next((s for s in probe["streams"] if s["codec_type"] == "video"), {})
    prov.update({"kind": item["kind"], "download_url": url, "downloaded": time.strftime("%Y-%m-%d"), "download_mb": round(size_dl / 1e6, 1),
                 "download_s": round(dl_s, 1), "cut": {"start_s": item.get("start_s", 0), "max_s": item["max_s"]},
                 "duration": float(probe["format"]["duration"]), "width": v.get("width"), "height": v.get("height"),
                 "has_audio": any(s["codec_type"] == "audio" for s in probe["streams"])})
    (d / "provenance.json").write_text(json.dumps(prov, indent=1))
    return prov


def main(ids: list[str]) -> None:
    items = yaml.safe_load((Path(__file__).with_name("media.yaml")).read_text())["items"]
    out = config.HOME / "benchmarks"
    for it in items:
        if ids and it["id"] not in ids:
            continue
        try:
            p = fetch(it, out)
            print(f"OK   {it['id']:<12} {p['width']}x{p['height']} {p['duration']:.0f}s  {p['license'][:40]}")
        except Exception as e:  # one failing source must not stop the rest
            print(f"FAIL {it['id']:<12} {type(e).__name__}: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])

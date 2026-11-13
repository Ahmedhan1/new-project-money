"""Account 3 / agent A demo: profile 4 public NASA/JPL videos of different formats, compare them, derive a style.

Downloads (once) the publisher's small "~mobile.mp4" rendition and its own .srt captions from the NASA Image and Video
Library (images-assets.nasa.gov, declared zero-cost host) into NEXUS_HOME/a3_refs/ (never Git). Captions are the
publisher's own (segment timings; word timings are spread evenly - marked in the profile). No Whisper run is needed.

    NEXUS_HOME=/home/user/new-project-money/.nexus python scripts/account3/creators_demo.py [--router]
Writes docs/results/account3/creators_profiles.json and creators_compare.json.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nexus import config, costguard  # noqa: E402
from nexus.intelligence import contracts as C  # noqa: E402
from nexus.intelligence.creators import analyze_reference, compare, style_from, to_account2_reference_style  # noqa: E402

NASA_TERMS = ("https://www.nasa.gov/nasa-brand-center/images-and-media/",
              "NASA images and video 'generally are not subject to copyright in the United States'; credit NASA.")
JPL_TERMS = ("https://www.jpl.nasa.gov/jpl-image-use-policy",
             "JPL images/video 'may be used for any purpose without prior permission' (special cases apply); credit 'Courtesy NASA/JPL-Caltech'.")
REFS = [
    {"nasa_id": "JPL-20260326-WHATSUf-0001-Whats_Up_April_2026", "format": "science_documentary", "category": "science_explainer",
     "creator": "NASA/JPL-Caltech", "rights": "licensed", "terms": JPL_TERMS},
    {"nasa_id": "iss061m2627771232_Live_Interviews_Jeanette_Epps_191004", "format": "interview_podcast", "category": "interview",
     "creator": "NASA JSC", "rights": "public-domain", "terms": NASA_TERMS},
    {"nasa_id": "KSC-2021110-VP-MWC01-001 CREW3-HIGHLIGHTS-3288522", "format": "event_highlights", "category": "highlights_montage",
     "creator": "NASA KSC", "rights": "public-domain", "terms": NASA_TERMS},
    {"nasa_id": "Explore our Home Planet and the Universe With NASA Podcasts", "format": "premium_ad", "category": "promo",
     "creator": "NASA HQ", "rights": "public-domain", "terms": NASA_TERMS},
]
NOT_FOUND = {"challenge_high_stakes": "no public-domain/CC challenge-format video found on NASA/Commons; not attempted",
             "arabic_edu_explainer": "no CC-licensed El Daheeh-style item found; the show itself is copyrighted (not downloaded)",
             "travel_vlog": "not attempted in this run (Commons search not done; time budget)"}


def get(url: str, dest: Path | None = None, limit: int = 100 * 1024 * 1024):
    costguard.check_host(url)
    url = url.replace("http://", "https://")
    with urllib.request.urlopen(urllib.parse.quote(url, safe=":/?=&~%"), timeout=120) as r:
        n = int(r.headers.get("Content-Length") or 0)
        if n > limit:
            raise RuntimeError(f"{url}: {n} bytes > limit")
        data = r.read()
    if dest:
        dest.write_bytes(data)
    return data


def srt_to_transcript(text: str) -> dict:
    def ts(s):
        h, m, rest = s.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    segs = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [x for x in block.strip().split("\n") if x.strip()]
        tl = next((i for i, x in enumerate(lines) if "-->" in x), None)
        if tl is None:
            continue
        a, b = (x.strip().split(" ")[0] for x in lines[tl].split("-->"))
        txt = " ".join(re.sub(r"<[^>]+>|\[[^\]]*\]|>>", "", x) for x in lines[tl + 1:]).strip()  # tags, [speaker], >>
        if txt:
            segs.append({"start": ts(a), "end": ts(b), "text": txt})
    return {"language": "en", "segments": segs, "source": "publisher .srt"}


def main(router: bool = False) -> None:
    store = config.HOME / "a3_refs"
    store.mkdir(parents=True, exist_ok=True)
    profiles, log = [], []
    for ref in REFS:
        nid = ref["nasa_id"]
        base = f"http://images-assets.nasa.gov/video/{nid}/{nid}"
        item_page = f"https://images.nasa.gov/details/{urllib.parse.quote(nid)}"
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", nid)[:80]
        mp4, srt = store / f"{safe}.mp4", store / f"{safe}.srt"
        t0 = time.time()
        try:
            if not mp4.exists():
                get(base + "~mobile.mp4", mp4)
            if not srt.exists():
                get(base + ".srt", srt)
            meta_json = json.loads(get(f"http://images-assets.nasa.gov/video/{nid}/metadata.json"))
            title = json.loads(get("https://images-api.nasa.gov/search?nasa_id=" + urllib.parse.quote(nid)))[
                "collection"]["items"][0]["data"][0]["title"]
            tr = srt_to_transcript(srt.read_text(encoding="utf-8", errors="replace"))
            p = analyze_reference(mp4, rights=ref["rights"], transcript=tr, use_router=router and not profiles,
                                  licence_url=ref["terms"][0],
                                  meta={"creator": ref["creator"], "format": ref["format"], "category": ref["category"],
                                        "title": title, "origin_url": item_page})
            p["evidence"].append(C.evidence("publisher licence terms", ref["terms"][0], quote=ref["terms"][1], kind="licence"))
            p["evidence"].append(C.evidence("item page + publisher metadata.json", item_page,
                                            quote=str(meta_json.get("XMP:Rights") or meta_json.get("XMP:Creator") or "")[:200],
                                            kind="licence_check"))
            p["source"]["rendition"] = "~mobile.mp4 (publisher's smallest full-length rendition)"
            C.check(p)
            profiles.append(p)
            log.append({"nasa_id": nid, "ok": True, "bytes": mp4.stat().st_size, "seconds": round(time.time() - t0, 1)})
        except Exception as e:  # noqa: BLE001 - report and continue with the others
            log.append({"nasa_id": nid, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})
        print(log[-1], flush=True)
    out = ROOT / "docs" / "results" / "account3"
    out.mkdir(parents=True, exist_ok=True)
    rep = compare(profiles) if profiles else {}
    style = style_from(profiles, name="NASA public references (demo)") if profiles else None
    (out / "creators_profiles.json").write_text(json.dumps(
        {"date": time.strftime("%Y-%m-%d"), "run": log, "formats_not_covered": NOT_FOUND, "profiles": profiles,
         "note": "media stored under NEXUS_HOME/a3_refs (not in Git); structure only"}, indent=1, ensure_ascii=False) + "\n")
    (out / "creators_compare.json").write_text(json.dumps(
        {"date": time.strftime("%Y-%m-%d"), "compare": rep, "style": style,
         "account2_reference_style": to_account2_reference_style(style, rights="public-domain") if style else None},
        indent=1, ensure_ascii=False) + "\n")
    print("profiles:", len(profiles), "->", out)


if __name__ == "__main__":
    main(router="--router" in sys.argv)

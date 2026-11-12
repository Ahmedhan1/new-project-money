"""B-roll / visual storytelling (BRL-1): turn a talking-head edit into a visually varied one WITHOUT changing what is said.

    opportunities(tl)                      where a cutaway helps (concrete nouns, places, objects, numbers) and where it
                                           must not be used (the opening hook, first-person/emotional statements, short clips)
    search(terms)                          licensed candidates with provenance: NASA Image and Video Library (public-domain
                                           NASA media), Openverse (CC0/PDM/BY/BY-SA, commercial + modification allowed)
    plan(tl, ...) -> {"inserts","decisions","share"}   ranked, relevance-gated, de-duplicated, never reusing an asset
    apply(tl, plan) -> (tl, report)         full-frame cutaways (clip["pip"]["full"]) over the speaker's own audio, so the
                                           narrative is unchanged; stills get Ken Burns motion; credits for every asset

No paid API and no key. Relevance is lexical (stemmed keyword overlap with titles/keywords/descriptions): without a local
vision-language model NEXUS cannot see the pictures, so weak matches are rejected rather than used.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path

from . import config, net
from .media import understand as und

EMOTIONAL = re.compile(r"\b(i feel|i felt|i love|i loved|my (mom|mother|dad|father|family|kids|husband|wife)|proud|cried|tears|"
                       r"emotional|scared|afraid|dream|grateful|thank)\b", re.I)
# Words so common in a source library that sharing them proves nothing (everything in NASA's library is about missions).
SOURCE_GENERIC = {"nasa": set("mission missions nasa space spacecraft launch program programme center centre source water "
                              "developed develop development agency science research test testing image photo view".split())}
RIGHTS_FLAGS = re.compile(r"(©|copyright|courtesy of|all rights reserved|used with permission|getty|reuters|ap photo)", re.I)
GENERIC = set("thing things time times people person way part lot lots year years today really actually going know think "
              "make made good great well want come came look looking start started talk talking because always mostly "
              "basically totally probably pretty little closer before after suspect being been find found other another "
              "every everything something anything someone everyone could would should might maybe sure kind sort able "
              "long short place places idea ideas stuff right still even also just only very much many most some "
              "board throughout goal goals hope office further human humans establishing presence back peaceful meantime "
              "couple prior knew whole different same next last first second third great".split())
CACHE_VERSION = 1


def _cache_dir() -> Path:
    d = config.CACHE / "broll"
    d.mkdir(parents=True, exist_ok=True)
    return d


def terms(text: str, k: int = 6) -> list[str]:
    """Concrete query terms: multi-word capitalised entities first, then frequent content words (no generic words)."""
    ents = [e.lower() for e in re.findall(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+|[A-Z]{2,}[a-z]?)\b", text or "")
            if e.lower() not in ("i", "nasa")]
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z-]{3,}", (text or "").lower())
             if w not in und.STOP and w not in GENERIC and not w.endswith("ly")]
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    out = []
    for t in ents + sorted(freq, key=lambda w: (-freq[w], -len(w))):
        if t not in out:
            out.append(t)
    return out[:k]


def _programme_text(tl: dict, a: float, b: float) -> str:
    return " ".join(s["text"] for s in tl.get("subtitles", []) if s["end"] > a + 0.1 and s["start"] < b - 0.1)


def opportunities(tl: dict, min_clip: float = 3.0) -> list[dict]:
    """Video clips where a cutaway can carry the picture while the speaker continues, with the reason for each decision."""
    from .timeline.render import clip_duration, clip_starts
    out = []
    for k, (c, st) in enumerate(zip(tl["clips"], clip_starts(tl))):
        if c["type"] != "video":
            continue
        dur = clip_duration(c)
        text = _programme_text(tl, st, st + dur)
        why_not = None
        if k == 0 or st < 4.0:
            why_not = "opening/hook: the speaker's face establishes who is talking"
        elif dur < min_clip:
            why_not = f"clip too short ({dur:.1f}s) for a readable cutaway"
        elif c.get("pip"):
            why_not = "clip already has an overlay"
        elif EMOTIONAL.search(text):
            why_not = "personal/emotional statement: the face carries the meaning"
        t = terms(text)
        if not why_not and len(t) < 2:
            why_not = "no concrete subject to illustrate"
        out.append({"clip": c["id"], "index": k, "start": round(st, 3), "end": round(st + dur, 3), "duration": round(dur, 3),
                    "text": text, "terms": t, "use": why_not is None, "why": why_not or f"concrete subject: {', '.join(t[:3])}"})
    return out


# ------------------------------------------------------------------ sources
def _cached_json(key: str, fetch) -> object:
    f = _cache_dir() / f"q_{hashlib.sha1(key.encode()).hexdigest()[:16]}_v{CACHE_VERSION}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    data = fetch()
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def search_nasa(query: str, n: int = 12) -> list[dict]:
    def fetch():
        d = net.get_json("https://images-api.nasa.gov/search", {"q": query, "media_type": "image,video", "page_size": 30},
                         timeout=25, retries=2)
        return d.get("collection", {}).get("items", [])
    out = []
    for it in _cached_json(f"nasa|{query}", fetch)[:40]:
        d = (it.get("data") or [{}])[0]
        nid, kind = d.get("nasa_id"), d.get("media_type")
        if not nid or kind not in ("image", "video"):
            continue
        desc = re.sub(r"<[^>]+>", " ", d.get("description") or "")
        rights_note = None
        if RIGHTS_FLAGS.search(desc) or RIGHTS_FLAGS.search(d.get("secondary_creator") or ""):
            rights_note = "description mentions third-party rights"
        q = urllib.parse.quote(nid)  # some NASA ids contain spaces
        base = f"https://images-assets.nasa.gov/{kind}/{q}/{q}"
        out.append({"id": f"nasa:{nid}", "source": "NASA Image and Video Library", "kind": kind, "title": d.get("title") or nid,
                    "description": desc[:600], "keywords": [k.lower() for k in d.get("keywords") or []],
                    "file_url": f"{base}~medium.jpg" if kind == "image" else f"{base}~mobile.mp4",
                    "page": f"https://images.nasa.gov/details/{nid}", "license": "public domain (NASA media; no endorsement implied)",
                    "credit": f"NASA ({d.get('center') or 'NASA'}) - {d.get('title') or nid}", "rights_note": rights_note,
                    "date": (d.get("date_created") or "")[:10]})
        if len(out) >= n:
            break
    return out


def search_openverse(query: str, n: int = 8) -> list[dict]:
    from . import assets
    try:
        res = _cached_json(f"openverse|{query}", lambda: assets.search_images(query, n=n, min_width=800))
    except Exception:  # noqa: BLE001 - rate limits etc.: the caller records the source as unavailable
        return []
    return [{"id": r["id"], "source": "Openverse", "kind": "image", "title": r["title"], "description": "", "keywords": [],
             "file_url": r["file_url"], "page": r["url"], "license": f"CC {r['license'].upper()} {r.get('license_version', '')}".strip(),
             "credit": f"\"{r['title']}\" by {r.get('creator') or 'unknown'} ({r['license'].upper()})", "rights_note": None}
            for r in res]


SOURCES = {"nasa": search_nasa, "openverse": search_openverse}
GENERAL_SOURCES = {"openverse"}  # not topic-specific: stricter subject check (a Paris street matched "Lemoine College")


def head_hits(cand: dict, query_terms: list[str]) -> list[str]:
    head = set(und.content(cand["title"] + " " + " ".join(cand.get("keywords") or [])))
    return [t for t in query_terms if set(und.content(t)) and set(und.content(t)) & head]


def relevance(cand: dict, query_terms: list[str]) -> tuple[float, list[str]]:
    """Stemmed overlap of the spoken terms with the candidate's title/keywords (x2) and description (x1)."""
    head = set(und.content(cand["title"] + " " + " ".join(cand.get("keywords") or [])))
    body = set(und.content(cand.get("description") or ""))
    score, hit = 0.0, []
    for t in query_terms:
        st = set(und.content(t))
        if not st:
            continue
        if st <= head:
            score, hit = score + 2.0, hit + [t]
        elif st & head:
            score, hit = score + 1.0, hit + [t]
        elif st <= body:
            score, hit = score + 0.75, hit + [t]
    return round(score, 2), hit


# ------------------------------------------------------------------ planning
def plan(tl: dict, *, max_share: float = 0.4, per_cut: float = 4.5, min_cut: float = 2.0, min_score: float = 2.0,
         sources: tuple = ("nasa", "openverse"), search=None, context: str = "") -> dict:
    """Choose cutaways for a built timeline. `search(source, query) -> [candidates]` can be injected (tests)."""
    search = search or (lambda src, q: SOURCES[src](q))
    from .timeline.render import total_duration
    total = total_duration(tl)
    ops = opportunities(tl)
    # the programme's overall subject: candidates must share it, so "humidity" in a cave story does not pull a flu study
    ctx = set(und.content(" ".join(terms(" ".join(s["text"] for s in tl.get("subtitles", [])), k=12)) + " " + (context or "")))
    decisions = [{"clip": o["clip"], "start": o["start"], "action": "skip", "why": o["why"]} for o in ops if not o["use"]]
    used, inserts, covered = set(), [], 0.0
    source_errors: dict[str, str] = {}
    for o in sorted((o for o in ops if o["use"]), key=lambda o: -len(o["terms"])):
        if covered >= max_share * total:
            decisions.append({"clip": o["clip"], "start": o["start"], "action": "skip", "why": f"B-roll share reached ({max_share:.0%})"})
            continue
        q_terms = o["terms"][:4]
        best, rejected = None, []
        for src in sources:
            for q in (" ".join(q_terms[:2]), " ".join(q_terms[:2]) + (f" {context}" if context else ""), q_terms[0]):
                try:
                    cands = search(src, q.strip())
                except Exception as e:  # noqa: BLE001 - source down/rate limited: record and continue
                    source_errors[src] = f"{type(e).__name__}: {str(e)[:120]}"
                    cands = []
                for c in cands:
                    if c["id"] in used:
                        continue
                    if c.get("rights_note"):
                        rejected.append(f"{c['id']}: {c['rights_note']}")
                        continue
                    sc, hit = relevance(c, q_terms)
                    weak = SOURCE_GENERIC.get(src, set())
                    hit = [h for h in hit if " " in h or not (set(und.content(h)) <= {w for t in weak for w in und.content(t)})]
                    sc = round(sc if hit else 0.0, 2)
                    # one generic shared word is not a match, and the asset's own title/keywords must name the subject
                    # (a term found only deep in a long description is how 'board, throughout' matched a random photo)
                    strong = (len(set(hit)) >= 2 or any(" " in h for h in hit)) and bool(head_hits(c, hit))
                    blob = set(und.content(c["title"] + " " + " ".join(c.get("keywords") or []) + " " + (c.get("description") or "")))
                    on_topic = not ctx or bool(ctx & blob)
                    if src in GENERAL_SOURCES and ctx:  # a general index: the title must share the programme's subject too
                        own = set(und.content(c["title"] + " " + " ".join(c.get("keywords") or [])))
                        on_topic = bool((ctx - set(und.content(" ".join(q_terms)))) & own)
                    if not on_topic:
                        rejected.append(f"{c['id']}: off the programme's subject ({', '.join(sorted(ctx)[:5])})")
                        continue
                    if sc < min_score or not strong:
                        rejected.append(f"{c['id']}: relevance {sc} ({', '.join(hit) or 'no shared terms'})")
                        continue
                    if best is None or sc > best[0] or (sc == best[0] and c["kind"] == "video" and best[1]["kind"] != "video"):
                        best = (sc, c, hit, q)
                if best and best[0] >= min_score + 1:
                    break
            if best:
                break
        if not best:
            decisions.append({"clip": o["clip"], "start": o["start"], "action": "skip",
                              "why": f"no relevant licensed candidate for '{' '.join(q_terms)}'", "rejected": rejected[:6]})
            continue
        sc, c, hit, q = best
        used.add(c["id"])
        a = min(1.0, o["duration"] * 0.2)
        length = max(min_cut, min(per_cut, o["duration"] - a - 0.6))
        if length < min_cut:
            decisions.append({"clip": o["clip"], "start": o["start"], "action": "skip", "why": "not enough room after the cut"})
            continue
        inserts.append({"clip": o["clip"], "local_start": round(a, 3), "local_end": round(a + length, 3), "asset": c,
                        "score": sc, "matched": hit, "query": q})
        covered += length
        decisions.append({"clip": o["clip"], "start": o["start"], "action": "insert", "asset": c["id"], "score": sc,
                          "why": f"'{c['title'][:70]}' matches {', '.join(hit)} (relevance {sc}); {c['license']}"})
    return {"inserts": inserts, "decisions": decisions, "share": round(covered / max(total, 0.1), 3),
            "source_errors": source_errors, "method": "lexical keyword relevance (no vision model)"}


def _phash(path: Path) -> str | None:
    try:
        from PIL import Image
        im = Image.open(path).convert("L").resize((9, 8))
        px = list(im.getdata())
        return "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
    except Exception:  # noqa: BLE001
        return None


def fetch(asset: dict) -> Path:
    url = asset["file_url"]
    ext = ".mp4" if asset["kind"] == "video" else Path(url.split("?")[0]).suffix.lower() or ".jpg"
    dest = _cache_dir() / f"{hashlib.sha1(url.encode()).hexdigest()[:20]}{ext}"
    if not dest.exists() or dest.stat().st_size == 0:
        net.download(url, dest, timeout=120, retries=2)
        man = _cache_dir() / "manifest.json"
        data = json.loads(man.read_text(encoding="utf-8")) if man.exists() else {}
        data[dest.name] = {k: asset.get(k) for k in ("id", "source", "title", "page", "file_url", "license", "credit")} | {
            "retrieved": time.strftime("%Y-%m-%d")}
        man.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return dest


def apply(tl: dict, p: dict, size: tuple[int, int] | None = None, fetcher=None) -> tuple[dict, dict]:
    """Insert the planned cutaways. Stills are turned into Ken Burns clips; near-duplicate pictures are dropped."""
    from . import generate
    fetcher = fetcher or fetch
    out = copy.deepcopy(tl)
    W, H = size or (out["width"], out["height"])
    by_id = {c["id"]: c for c in out["clips"]}
    hashes: list[str] = []
    applied, credits = [], []
    for ins in p["inserts"]:
        a = ins["asset"]
        try:
            path = fetcher(a)
        except Exception as e:  # noqa: BLE001
            p["decisions"].append({"clip": ins["clip"], "action": "skip", "why": f"download failed: {e}"})
            continue
        probe_img = path
        if a["kind"] == "video":
            probe_img = path.with_suffix(".frame.jpg")
            if not probe_img.exists():
                from .media import ff
                ff.run(["-ss", "1", "-i", path, "-frames:v", "1", probe_img], op="broll-frame")
        h = _phash(probe_img)
        if h and any(sum(x != y for x, y in zip(h, o)) <= 8 for o in hashes):
            p["decisions"].append({"clip": ins["clip"], "action": "skip", "why": f"{a['id']} is a near-duplicate of a picture already used"})
            continue
        if h:
            hashes.append(h)
        dur = ins["local_end"] - ins["local_start"]
        src = path
        if a["kind"] == "image":
            src = path.with_suffix(f".kb{W}x{H}.mp4")
            if not src.exists():
                generate.animate_still(path, src, seconds=dur + 0.6, size=(W, H), move="in" if len(applied) % 2 == 0 else "out",
                                       source_provenance={"kind": "licensed", "id": a["id"]})
        c = by_id[ins["clip"]]
        c["pip"] = {"src": str(src), "in": 0.0, "full": True, "start": ins["local_start"], "end": ins["local_end"],
                    "provenance": {"kind": "footage" if a["kind"] == "video" else "animated_still", "source": a["source"],
                                   "id": a["id"], "title": a["title"], "license": a["license"], "page": a["page"],
                                   "matched": ins["matched"]}}
        applied.append(ins)
        credits.append(a["credit"])
    out["credits"] = sorted(set((out.get("credits") or []) + credits))
    out["broll"] = {"inserted": len(applied), "share": p["share"], "method": p["method"], "source_errors": p["source_errors"]}
    return out, {"inserted": len(applied), "decisions": p["decisions"], "credits": out["credits"], "share": p["share"]}

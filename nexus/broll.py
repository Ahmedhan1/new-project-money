"""B-roll / visual storytelling (BRL-1): turn a talking-head edit into a visually varied one WITHOUT changing what is said.

    opportunities(tl)                      where a cutaway helps (concrete nouns, places, objects, numbers) and where it
                                           must not be used (the opening hook, first-person/emotional statements, short clips)
    search(terms)                          licensed candidates with provenance: NASA Image and Video Library (public-domain
                                           NASA media), Openverse (CC0/PDM/BY/BY-SA, commercial + modification allowed)
    plan(tl, ...) -> {"inserts","decisions","share"}   ranked, relevance-gated, de-duplicated, never reusing an asset
    apply(tl, plan) -> (tl, report)         full-frame cutaways (clip["pip"]["full"]) over the speaker's own audio, so the
                                           narrative is unchanged; stills get Ken Burns motion; credits for every asset

    local_candidates(path, analysis, ...)  shots of local, lawfully held files (little speech) as candidates

No paid API and no key. With the local SigLIP model (media/visual.py) candidates are ranked by what the picture shows
(image-text cosine with the spoken text) plus a lexical bonus, with penalties for repeated imagery, for looking like the
speaker's own shot and for repeating the previous cutaway's shot type/motion. Without it relevance is lexical (stemmed
keyword overlap with titles/keywords/descriptions), weak matches are rejected, and the plan says `visual: false`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path

from . import config, costguard, net
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


def motion_profile(path, fps: float = 2.0, width: int = 96) -> list[float]:
    """Mean absolute luma change between consecutive sampled frames (0-255), one value per 1/fps seconds."""
    import subprocess
    import numpy as np
    from .media import ff
    info = ff.probe(path)
    v = info.get("video") or {}
    if not v:
        return []
    h = max(2, int(round(v["height"] * width / v["width"] / 2)) * 2)
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-an", "-vf",
                        f"fps={fps},scale={width}:{h},format=gray", "-f", "rawvideo", "-"], capture_output=True, timeout=600)
    n = len(p.stdout) // (width * h)
    fr = np.frombuffer(p.stdout[: n * width * h], dtype=np.uint8).reshape(n, h, width).astype(np.float32)
    return [float(np.abs(fr[i] - fr[i - 1]).mean()) for i in range(1, n)]


def moving_in_point(path, dur: float, fps: float = 2.0, still: float = 0.6) -> float:
    """Source in-point of the `dur`-second window with the most picture motion. Archive footage often opens on a static
    shot (a rocket on the pad before launch was flagged as a 2.3 s frozen picture), so 0.0 is not a safe default."""
    try:
        m = motion_profile(path, fps)
    except Exception:  # noqa: BLE001 - unreadable file: keep the start
        return 0.0
    k = max(1, int(round(dur * fps)))
    if len(m) <= k:
        return 0.0
    best, best_i = -1.0, 0
    for i in range(0, len(m) - k + 1):
        w = m[i:i + k]
        score = min(w) * 2 + sum(w) / k   # every part of the window must move, not just one burst
        if score > best:
            best, best_i = score, i
    return round(best_i / fps, 2) if best > still else 0.0


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
        links = [x.get("href") for x in it.get("links") or [] if x.get("href")]
        preview = next((u for u in links if "~medium" in u or "~small" in u), links[0] if links else None)
        out.append({"id": f"nasa:{nid}", "source": "NASA Image and Video Library", "kind": kind, "title": d.get("title") or nid,
                    "description": desc[:600], "keywords": [k.lower() for k in d.get("keywords") or []],
                    "file_url": f"{base}~medium.jpg" if kind == "image" else f"{base}~mobile.mp4",
                    **({"hd_url": f"{base}~medium.mp4"} if kind == "video" else {}),
                    "preview_url": preview or (f"{base}~medium.jpg" if kind == "image" else None),
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
             "file_url": r["file_url"], "preview_url": r["file_url"], "page": r["url"], "license": f"CC {r['license'].upper()} {r.get('license_version', '')}".strip(),
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


# ------------------------------------------------------------------ visual relevance (VIS-1 / K-18)
VIS_MIN = 0.045          # SigLIP cosine(sentence, picture) below which a candidate is not shown (calibrated: docs/results/a2_broll)
VIS_LEX_RELIEF = 0.015   # a strong lexical match may pass this much below VIS_MIN
LEX_WEIGHT = 0.012       # lexical relevance points -> cosine units (a 2-point title match is worth +0.024)
DUP_REJECT = 0.92        # cosine to a picture already used in this edit: same imagery, rejected
DUP_SOFT = 0.80          # above this a candidate is penalised in proportion (repeated look)
SELF_REJECT = 0.88       # cosine to the speaker's own shot: the cutaway would look like the shot it covers
VARIETY_PEN = 0.01       # same shot type and motion level as the previous cutaway


def _preview_path(c: dict) -> Path | None:
    url = c.get("preview_url") or (c.get("file_url") if c.get("kind") == "image" else None)
    if not url:
        return None
    dest = _cache_dir() / f"prev_{hashlib.sha1(url.encode()).hexdigest()[:20]}{Path(url.split('?')[0]).suffix.lower() or '.jpg'}"
    if not dest.exists() or dest.stat().st_size == 0:
        if c.get("source", "").startswith("NASA"):
            costguard.authorize("nasa-media", "download")
        net.download(url, dest, timeout=60, retries=1)
    return dest


def candidate_embedding(c: dict):
    """Embedding of a candidate's picture: local shots carry one; remote ones use their preview image (cached)."""
    if c.get("embedding") is not None:
        return c["embedding"]
    from .media import visual
    if not visual.available():
        return None
    if c.get("local_path"):
        return visual.asset_embedding(c["local_path"])
    p = _preview_path(c)
    return visual.asset_embedding(p) if p else None


def local_candidates(path: str, analysis: dict, licence: str, credit: str, *, max_speech: float = 0.15, min_len: float = 2.0,
                     source: str = "local library") -> list[dict]:
    """Cutaway candidates from a local, lawfully held file: its measured shots with little speech, each carrying the
    shot embedding, shot type and motion level from media/visual.py (analysis["visual"])."""
    from .media import visual
    vis = (analysis or {}).get("visual") or {}
    embs = visual.shot_embeddings(vis) if vis.get("available") else {}
    rows = {r["id"]: r for r in vis.get("shots") or []}
    out = []
    for sh in analysis.get("shots") or []:
        r = rows.get(sh.get("id"))
        if sh.get("speech_share", 0) > max_speech or sh["end"] - sh["start"] < min_len or (sh.get("quality") or 1.0) < 0.5:
            continue
        if r and r.get("dup_of") is not None:
            continue  # a visual repeat of an earlier shot of the same file
        out.append({"id": f"local:{Path(path).stem}#{sh['id']}", "source": source, "kind": "local_video", "src": str(path),
                    "in": float(sh["start"]), "out": float(sh["end"]), "title": (r or {}).get("scene", [{}])[0].get("label", "shot"),
                    "description": "", "keywords": [], "license": licence, "credit": credit, "rights_note": None,
                    "page": str(path), "embedding": embs.get(sh["id"]), "shot_type": (r or {}).get("shot_type"),
                    "motion_level": (r or {}).get("motion_level")})
    return out


# ------------------------------------------------------------------ planning
def plan(tl: dict, *, max_share: float = 0.4, per_cut: float = 4.5, min_cut: float = 2.0, min_score: float = 2.0,
         sources: tuple = ("nasa", "openverse"), search=None, context: str = "", local: list[dict] | None = None,
         visual_rank: bool | None = None, embed=None, embed_text=None, speaker_embedding=None,
         vis_min: float = VIS_MIN) -> dict:
    """Choose cutaways for a built timeline.

    Lexical path (no visual model): stemmed keyword overlap with titles/keywords, strict gates (as before).
    Visual path (SigLIP available, or `embed`/`embed_text` injected): every licence-clean, on-topic candidate is ranked by
    cosine(spoken text, picture) + a small lexical bonus, minus penalties for imagery already used in the edit, for
    looking like the speaker's own shot, and for repeating the previous cutaway's shot type + motion. `local` adds
    candidates from local files (local_candidates). `search(source, query)` can be injected (tests)."""
    search = search or (lambda src, q: SOURCES[src](q))
    from .timeline.render import total_duration
    if visual_rank is None:
        from .media import visual
        visual_rank = embed is not None or visual.available()
    if visual_rank:
        if embed_text is None:
            from .media import visual

            def embed_text(t):
                return visual.embed_texts([t])[0]
        embed = embed or candidate_embedding
    total = total_duration(tl)
    ops = opportunities(tl)
    # the programme's overall subject: candidates must share it, so "humidity" in a cave story does not pull a flu study
    ctx = set(und.content(" ".join(terms(" ".join(s["text"] for s in tl.get("subtitles", [])), k=12)) + " " + (context or "")))
    decisions = [{"clip": o["clip"], "start": o["start"], "action": "skip", "why": o["why"]} for o in ops if not o["use"]]
    used, inserts, covered = set(), [], 0.0
    used_embs: list = []
    prev_style = None
    source_errors: dict[str, str] = {}
    emb_cache: dict = {}

    def emb_of(c):
        if c["id"] not in emb_cache:
            try:
                emb_cache[c["id"]] = embed(c)
            except Exception as e:  # noqa: BLE001 - preview not reachable: candidate cannot be judged visually
                source_errors.setdefault("preview", f"{type(e).__name__}: {str(e)[:120]}")
                emb_cache[c["id"]] = None
        return emb_cache[c["id"]]

    order = sorted((o for o in ops if o["use"]), key=lambda o: -len(o["terms"])) if not visual_rank else \
        [o for o in ops if o["use"]]
    for o in order:
        if covered >= max_share * total:
            decisions.append({"clip": o["clip"], "start": o["start"], "action": "skip", "why": f"B-roll share reached ({max_share:.0%})"})
            continue
        q_terms = o["terms"][:4]
        best, rejected = None, []
        pool: list[tuple[dict, str, str]] = [(c, "local", "") for c in (local or [])]
        for src in sources:
            queries = (" ".join(q_terms[:2]), " ".join(q_terms[:2]) + (f" {context}" if context else ""), q_terms[0])
            for q in dict.fromkeys(x.strip() for x in queries if x.strip()):
                try:
                    cands = search(src, q)
                except Exception as e:  # noqa: BLE001 - source down/rate limited: record and continue
                    source_errors[src] = f"{type(e).__name__}: {str(e)[:120]}"
                    cands = []
                pool += [(c, src, q) for c in cands]
        t_emb = embed_text(o["text"]) if visual_rank and o["text"] else None
        seen_ids: set = set()
        for c, src, q in pool:
            if c["id"] in used or c["id"] in seen_ids:
                continue
            seen_ids.add(c["id"])
            if c.get("rights_note") or not c.get("license"):
                rejected.append(f"{c['id']}: {c.get('rights_note') or 'no licence recorded'}")
                continue
            sc, hit = relevance(c, q_terms)
            weak = SOURCE_GENERIC.get(src, set())
            hit = [h for h in hit if " " in h or not (set(und.content(h)) <= {w for t in weak for w in und.content(t)})]
            sc = round(sc if hit else 0.0, 2)
            strong = (len(set(hit)) >= 2 or any(" " in h for h in hit)) and bool(head_hits(c, hit))
            if src != "local":
                blob = set(und.content(c["title"] + " " + " ".join(c.get("keywords") or []) + " " + (c.get("description") or "")))
                on_topic = not ctx or bool(ctx & blob)
                if src in GENERAL_SOURCES and ctx:  # a general index: the title must share the programme's subject too
                    own = set(und.content(c["title"] + " " + " ".join(c.get("keywords") or [])))
                    on_topic = bool((ctx - set(und.content(" ".join(q_terms)))) & own)
                if not on_topic:
                    rejected.append(f"{c['id']}: off the programme's subject ({', '.join(sorted(ctx)[:5])})")
                    continue
            if not visual_rank:
                if src == "local":
                    continue  # local shots have no words to match without the visual model
                if sc < min_score or not strong:
                    rejected.append(f"{c['id']}: relevance {sc} ({', '.join(hit) or 'no shared terms'})")
                    continue
                if best is None or sc > best["score"] or (sc == best["score"] and c["kind"] == "video" and best["asset"]["kind"] != "video"):
                    best = {"score": sc, "asset": c, "matched": hit, "query": q, "reasons": [f"lexical relevance {sc}: {', '.join(hit)}"]}
                continue
            e = emb_of(c) if t_emb is not None else None
            if e is None:
                rejected.append(f"{c['id']}: picture could not be embedded")
                continue
            import numpy as np
            vis = float(np.dot(t_emb, e))
            reasons = [f"visual match {vis:.3f} (SigLIP cosine of the spoken text and the picture)"]
            bar = vis_min - (VIS_LEX_RELIEF if strong else 0.0)
            if vis < bar:
                rejected.append(f"{c['id']}: visual match {vis:.3f} < {bar:.3f}")
                continue
            score = vis + LEX_WEIGHT * min(sc, 4.0)
            if sc:
                reasons.append(f"lexical bonus {LEX_WEIGHT * min(sc, 4.0):+.3f} ({', '.join(hit)})")
            if used_embs:
                rep = max(float(np.dot(e, u)) for u in used_embs)
                if rep >= DUP_REJECT:
                    rejected.append(f"{c['id']}: repeats imagery already used (cosine {rep:.3f})")
                    continue
                if rep > DUP_SOFT:
                    score -= (rep - DUP_SOFT) * 0.2
                    reasons.append(f"similar to an earlier cutaway (cosine {rep:.3f}): {-(rep - DUP_SOFT) * 0.2:+.3f}")
            if speaker_embedding is not None:
                se = float(np.dot(e, speaker_embedding))
                if se >= SELF_REJECT:
                    rejected.append(f"{c['id']}: looks like the speaker's own shot (cosine {se:.3f})")
                    continue
            style = (c.get("shot_type"), c.get("motion_level"))
            if prev_style and style == prev_style and style != (None, None):
                score -= VARIETY_PEN
                reasons.append(f"same shot type/motion as the previous cutaway ({style[0]}, {style[1]}): {-VARIETY_PEN:+.3f}")
            if best is None or score > best["score"]:
                best = {"score": round(score, 4), "asset": c, "matched": hit, "query": q, "reasons": reasons, "visual": round(vis, 4),
                        "lexical": sc, "embedding": e}
        if not best:
            decisions.append({"clip": o["clip"], "start": o["start"], "action": "skip",
                              "why": f"no relevant licensed candidate for '{' '.join(q_terms) or o['text'][:40]}'", "rejected": rejected[:6]})
            continue
        c = best["asset"]
        a = min(1.0, o["duration"] * 0.2)
        length = max(min_cut, min(per_cut, o["duration"] - a - 0.6))
        if c["kind"] == "local_video":
            length = min(length, c["out"] - c["in"])
        if length < min_cut:
            decisions.append({"clip": o["clip"], "start": o["start"], "action": "skip", "why": "not enough room after the cut"})
            continue
        used.add(c["id"])
        if best.get("embedding") is not None:
            used_embs.append(best["embedding"])
        prev_style = (c.get("shot_type"), c.get("motion_level"))
        asset = {k: v for k, v in c.items() if k != "embedding"}
        inserts.append({"clip": o["clip"], "local_start": round(a, 3), "local_end": round(a + length, 3), "asset": asset,
                        "score": best["score"], "matched": best["matched"], "query": best["query"], "reasons": best["reasons"],
                        "visual": best.get("visual"), "lexical": best.get("lexical", best["score"]), "text": o["text"][:200],
                        "provenance": {"source": c.get("source"), "id": c["id"], "license": c.get("license"),
                                       "credit": c.get("credit"), "page": c.get("page")}})
        covered += length
        decisions.append({"clip": o["clip"], "start": o["start"], "action": "insert", "asset": c["id"], "score": best["score"],
                          "why": f"'{c['title'][:70]}': {'; '.join(best['reasons'])}; {c['license']}"})
    method = ("visual: SigLIP image-text cosine + lexical bonus, duplicate/self/variety penalties (licence + subject gates kept)"
              if visual_rank else "lexical keyword relevance (no vision model) - flagged: pictures were not looked at")
    return {"inserts": inserts, "decisions": decisions, "share": round(covered / max(total, 0.1), 3),
            "source_errors": source_errors, "method": method, "visual": bool(visual_rank)}


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


def hd_window(asset: dict, start: float, dur: float) -> Path | None:
    """The used window of a video asset from its 1280x720 rendition (range requests: only these seconds are read).
    The 320x180 'mobile' file picks the in-point; upscaling it to 1080p was visibly soft. None when unavailable."""
    url = asset.get("hd_url")
    if not url:
        return None
    dest = _cache_dir() / f"{hashlib.sha1(f'{url}|{start:.2f}|{dur:.2f}'.encode()).hexdigest()[:20]}.hd.mp4"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        from .media import ff
        costguard.authorize("nasa-media", "download")
        ff.run(["-ss", f"{start:.2f}", "-i", url, "-t", f"{dur:.2f}", "-an", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "18", "-pix_fmt", "yuv420p", dest], op="broll-hd-window")
        return dest if dest.exists() and dest.stat().st_size > 0 else None
    except Exception:  # noqa: BLE001 - offline / host refused: the mobile rendition still works
        dest.unlink(missing_ok=True)
        return None


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
        if a["kind"] == "local_video":  # a shot of a local file: no download, in-point from the shot (middle of it)
            dur = ins["local_end"] - ins["local_start"]
            c = by_id[ins["clip"]]
            src_in = round(a["in"] + max(0.0, (a["out"] - a["in"] - dur) / 2), 3)
            c["pip"] = {"src": a["src"], "in": src_in, "full": True, "start": ins["local_start"], "end": ins["local_end"],
                        "provenance": {"kind": "footage", "source": a["source"], "id": a["id"], "title": a["title"],
                                       "license": a["license"], "page": a["page"], "matched": ins["matched"],
                                       "visual": ins.get("visual"), "reasons": ins.get("reasons")}}
            applied.append(ins)
            credits.append(a["credit"])
            continue
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
        src_in, rendition = 0.0, None
        if a["kind"] == "video":
            src_in = moving_in_point(path, dur)
            hd = hd_window(a, src_in, dur + 0.5)
            if hd:
                src, src_in, rendition = hd, 0.0, "1280x720 window"
        c["pip"] = {"src": str(src), "in": src_in, "full": True, "start": ins["local_start"], "end": ins["local_end"],
                    "provenance": {"kind": "footage" if a["kind"] == "video" else "animated_still", "source": a["source"],
                                   "id": a["id"], "title": a["title"], "license": a["license"], "page": a["page"],
                                   "matched": ins["matched"], "visual": ins.get("visual"), "reasons": ins.get("reasons"),
                                   **({"rendition": rendition} if rendition else {})}}
        applied.append(ins)
        credits.append(a["credit"])
    out["credits"] = sorted(set((out.get("credits") or []) + credits))
    out["broll"] = {"inserted": len(applied), "share": p["share"], "method": p["method"], "source_errors": p["source_errors"]}
    return out, {"inserted": len(applied), "decisions": p["decisions"], "credits": out["credits"], "share": p["share"]}

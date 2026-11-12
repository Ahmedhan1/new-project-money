"""Repurpose studio: rework footage the user owns, has licensed, or that is public domain, and commentary/recap videos whose
substance is new (own narration, analysis, graphics).

What this module will NOT do (and refuses with `RepurposeRefused`): anything whose purpose is to get someone else's video
past a platform's copyright or reused-content detection -- mirroring/flipping, pitch or speed tricks, cropping or
re-framing to evade matching, fingerprint games -- and it contains no platform downloader. The durable route to
republishing is rights + real transformation; `rights_review` gates exports and `transformation_report` measures it.

Timeline extras written here (unknown keys are ignored by the renderer):
  tl["repurpose"] = {"layout", "sources": {path: {media_id, name, duration}}, "used": [{media_id, src, in, out, clip, audio}],
                     "narration": [{start, end, text, clip, overlap?}], ...}
  image clips that are frames of a source carry "derived_from": <media path> and "provenance".
  narration audio items carry "role": "narration", "duration" and, when one line runs over several shots, "spans_cuts": True.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from pathlib import Path

from . import config, edit, models, project as prj
from .media import analyze, ff
from .qa import _c
from .speech import tts
from .timeline import ops, subs
from .timeline.render import clip_duration, clip_starts, total_duration

RIGHTS = {"owned", "licensed", "public-domain", "commentary"}
BASIS_LABEL = {"owned": "own footage", "licensed": "used under licence", "public-domain": "public domain",
               "commentary": "excerpts used for commentary / criticism"}

# Commentary thresholds. These are CONSERVATIVE EDITORIAL THRESHOLDS CHOSEN BY NEXUS, NOT A LEGAL TEST. Fair use / fair
# dealing / quotation rights differ from country to country and are decided case by case; passing them does not make
# a use lawful, it only means the video is built mainly from new material.
COMMENTARY_MIN_NARRATION_SHARE = 0.50   # new narration/commentary over >= 50 % of programme time
COMMENTARY_MAX_UNMODIFIED_S = 30.0      # longest uninterrupted unmodified source stretch <= 30 s
COMMENTARY_MAX_SOURCE_SHARE = 0.50      # at most 50 % of any source's duration used
RECAP_MAX_OVERLAP = 0.35                # recap narration: max share of its 4-word sequences found verbatim in the transcript
DISCLAIMER = ("These are conservative editorial thresholds chosen by NEXUS, not a legal test. Fair use, fair dealing and "
              "quotation rights vary by country and are decided case by case; the rights declaration is the user's responsibility.")
REFUSAL = ("NEXUS will not do this: it only reworks footage you own, have licensed or that is public domain, or makes "
           "commentary whose substance is new. It does not mirror, pitch-shift, speed-shift, crop or otherwise alter someone "
           "else's video to get it past copyright or reused-content detection, and it does not download from platforms. "
           "Get a licence or permission, use public-domain footage, or make a commentary/recap with your own narration.")

_EVASION_TEXT = re.compile(
    r"\b(mirror(ed|ing)?|h?flip(ped|ping)?|pitch[- ]?shift\w*|chang\w* the pitch|speed (it )?up to|slow (it )?down to avoid|"
    r"content[- ]?id|reused[- ]content|fingerprint\w*|bypass\w*|evad\w*|evasion|avoid\w* (copyright|detection|claims?|strikes?|matching)|"
    r"(get|slip|sneak)\w* (it )?(past|through|around)|re-?upload\w* (someone|somebody|another|their|other)|"
    r"yt-?dlp|youtube-dl|download\w* (it |the video |videos )?from (youtube|tiktok|instagram|facebook|twitter|x\.com)|"
    r"crop\w* (it )?to (avoid|evade|beat|fool)|not (get )?detected|undetect\w*)\b", re.I)
_EVASION_OPTIONS = {"mirror", "flip", "hflip", "vflip", "pitch", "pitch_shift", "speed_trick", "crop_to_evade", "fingerprint", "evade",
                    "evade_detection", "bypass_detection", "avoid_content_id", "download_url", "url", "source_url"}
_EVASION_EFFECTS = {"mirror", "hflip", "vflip", "flip", "pitch", "pitch_shift"}


class RepurposeRefused(RuntimeError):
    """Raised for requests outside the boundary, and when an export's rights review fails."""


def check_request(text: str = "", options: dict | None = None) -> None:
    """Refuse requests whose purpose is detection evasion or platform downloading. Raises RepurposeRefused."""
    bad = sorted(k for k, v in (options or {}).items() if k in _EVASION_OPTIONS and v)
    m = _EVASION_TEXT.search(text or "")
    if bad or m:
        why = f"option(s) {', '.join(bad)}" if bad else f"request mentions \"{m.group(0)}\""
        raise RepurposeRefused(f"{REFUSAL} ({why})")


# ---------------------------------------------------------------- rights
def _media(project, ref: str) -> dict:
    ref = str(ref)
    if re.match(r"^[a-z][a-z0-9+.-]*://", ref):
        raise RepurposeRefused(f"{REFUSAL} (a URL is not a project media item: {ref[:60]})")
    for m in project.data.get("media", []):
        if ref in (m["id"], m["path"]):
            return m
    raise KeyError(f"{ref} is not media of project {project.id}; import it first")


def declare_rights(project, media_id: str, basis: str, evidence: str = "", holder: str = "") -> dict:
    """Record the user's rights declaration on a project media entry. Returns the declaration."""
    if basis not in RIGHTS:
        raise ValueError(f"basis must be one of {sorted(RIGHTS)}")
    m = _media(project, media_id)
    prev = m.get("rights")
    decl = {"basis": basis, "evidence": evidence.strip(), "holder": holder.strip() or ("the user" if basis == "owned" else ""),
            "declared_by": "user", "declared_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts": time.time()}
    if prev and prev != "user-supplied":
        decl["previous"] = prev if isinstance(prev, str) else {k: v for k, v in prev.items() if k != "previous"}
    m["rights"] = decl
    project.save()
    return decl


def _decl(m: dict | None) -> dict | None:
    r = (m or {}).get("rights")
    return r if isinstance(r, dict) and r.get("basis") in RIGHTS else None


def _source_paths(tl: dict) -> list[str]:
    seen: list[str] = []
    for p in (tl.get("repurpose") or {}).get("sources", {}):
        seen.append(p)
    for c in tl.get("clips", []):
        for p in (c.get("src") if c["type"] == "video" else None, c.get("derived_from"), (c.get("pip") or {}).get("src")):
            if p and p not in seen:
                seen.append(p)
    return seen


def rights_review(project, tl: dict, analyses: dict | None = None) -> dict:
    """Stage "1-rights": every source clip needs a declaration; commentary also needs the transformation thresholds."""
    checks = []
    by_path = {m["path"]: m for m in project.data.get("media", [])}
    generated = project.data.get("generated", {})
    bases = set()
    for p in _source_paths(tl):
        name = Path(p).name
        if re.match(r"^[a-z][a-z0-9+.-]*://", p):
            checks.append(_c(f"rights {name}", False, "remote URL as a source: no platform downloads", fix="declare-rights"))
            continue
        if p in generated and p not in by_path:
            continue  # produced by NEXUS itself (narration, renders, cards); provenance is checked by the asset stage
        m = by_path.get(p)
        d = _decl(m)
        if m is None:
            checks.append(_c(f"rights {name}", False, "not a project media item, so no rights declaration exists", fix="declare-rights"))
        elif d is None:
            checks.append(_c(f"rights {name}", False, f"{m['id']}: no rights declaration (declare owned / licensed / public-domain / commentary)",
                             fix="declare-rights"))
        else:
            bases.add(d["basis"])
            checks.append(_c(f"rights {name}", True, f"{d['basis']} - holder: {d.get('holder') or 'n/a'} - declared {d['declared_at']}"))
            if d["basis"] == "licensed" and not d.get("evidence"):
                checks.append(_c(f"licence evidence {name}", False, "licensed but no licence reference/evidence recorded", "major", fix="declare-rights"))
            if d["basis"] == "public-domain" and not d.get("evidence"):
                checks.append(_c(f"public-domain evidence {name}", False, "no source/evidence for the public-domain status", "minor"))
    evasive = [c["id"] for c in tl.get("clips", []) if any(c.get(k) for k in ("mirror", "hflip", "vflip", "pitch", "pitch_shift"))
               or any((e or {}).get("type") in _EVASION_EFFECTS for e in c.get("effects", []))]
    checks.append(_c("no detection-evasion transforms", not evasive, "none" if not evasive else f"mirror/flip/pitch on clips {evasive}"))
    rep = transformation_report(tl, analyses)
    checks.append(_c("transformation (information)", True, rep["text"]))
    if "commentary" in bases:
        t = rep["commentary"]
        checks.append(_c("commentary: new narration share", t["narration_ok"],
                         f"{rep['commentary_share']:.0%} of programme time (needs >= {COMMENTARY_MIN_NARRATION_SHARE:.0%})", fix="add-commentary"))
        checks.append(_c("commentary: longest unmodified source stretch", t["stretch_ok"],
                         f"{rep['longest_unmodified_stretch_s']:.1f}s (max {COMMENTARY_MAX_UNMODIFIED_S:.0f}s)", fix="shorten-excerpts"))
        checks.append(_c("commentary: share of the source used", t["source_share_ok"],
                         f"{rep['max_source_share']:.0%} of a source (max {COMMENTARY_MAX_SOURCE_SHARE:.0%})", fix="shorten-excerpts"))
        checks.append(_c("commentary thresholds are editorial", True, DISCLAIMER))
    return {"stage": "1-rights", "checks": checks, "passed": all(c["ok"] or c["severity"] == "minor" for c in checks), "report": rep}


def assert_exportable(project, tl: dict, analyses: dict | None = None) -> dict:
    """Return the rights review, or raise RepurposeRefused if it fails (exports must not run)."""
    rv = rights_review(project, tl, analyses)
    if not rv["passed"]:
        bad = [f"{c['check']}: {c['detail']}" for c in rv["checks"] if not c["ok"] and c["severity"] != "minor"]
        raise RepurposeRefused("export refused by the rights review: " + "; ".join(bad))
    return rv


def render_repurposed(project, tl: dict, name: str, analyses: dict | None = None) -> tuple[dict, dict]:
    """Rights gate -> render with the QA loop -> continuity review -> approval report. Refuses if the gate fails."""
    from . import qa, qa_continuity
    rv = assert_exportable(project, tl, analyses)
    stats, report = qa.render_with_qa(project, tl, name, analyses, extra_stages=[rv])
    cont = qa_continuity.continuity_review(stats["output"], tl)
    report = qa.approve(project.dir, name, report["stages"] + [cont],
                        {"corrections": report.get("corrections", []), "render": stats, "credits": credits_block(project, tl)})
    return stats, report


def credits_block(project, tl: dict) -> list[str]:
    """Attribution lines for every source in the timeline (for descriptions and end cards)."""
    by_path = {m["path"]: m for m in project.data.get("media", [])}
    lines = []
    for p in _source_paths(tl):
        m = by_path.get(p)
        if m is None:
            continue
        d = _decl(m)
        if d is None:
            lines.append(f"{m['name']} - RIGHTS NOT DECLARED")
            continue
        parts = [m["name"], f"(c) {d['holder']}" if d.get("holder") else None, BASIS_LABEL[d["basis"]],
                 d.get("evidence") or None]
        lines.append(" - ".join(x for x in parts if x))
    return lines


# ---------------------------------------------------------------- measurement
def _union(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted((a, b) for a, b in iv if b > a):
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _len(iv: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in _union(iv))


def _subtract(iv: list[tuple[float, float]], cut: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    cut = _union(cut)
    for a, b in _union(iv):
        cur = a
        for x, y in cut:
            if y <= cur or x >= b:
                continue
            if x > cur:
                out.append((cur, x))
            cur = max(cur, y)
        if cur < b:
            out.append((cur, b))
    return out


def _intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return _subtract(a, _subtract(a, b))


def _audio_dur(a: dict) -> float:
    if a.get("duration") is not None:
        return float(a["duration"])
    p = Path(a.get("src", ""))
    return ff.duration(p) if p.exists() else 0.0


def transformation_report(tl: dict, analyses: dict | None = None) -> dict:
    """Measured shares over programme time, from the timeline alone (no re-analysis). Seconds are programme seconds."""
    rp = tl.get("repurpose") or {}
    sources = list(rp.get("sources", {})) or sorted({c["src"] for c in tl["clips"] if c["type"] == "video"})
    srcset = set(sources)
    prog = total_duration(tl)
    starts = clip_starts(tl)
    picture, stills, pip, orig_audio, graphics = [], [], [], [], []
    used: dict[str, list[tuple[float, float]]] = {s: [] for s in sources}
    n_clips = 0
    for c, st in zip(tl["clips"], starts):
        d = clip_duration(c)
        iv = (st, st + d)
        if c["type"] == "video" and c["src"] in srcset:
            n_clips += 1
            picture.append(iv)
            used[c["src"]].append((c["in"], c["out"]))
            if not c.get("mute") and float(c.get("volume", 1.0)) > 0:
                orig_audio.append(iv)
        elif c.get("derived_from") in srcset:
            stills.append(iv)
            if c.get("composited"):
                graphics.append(iv)
        elif c["type"] in ("color", "image"):
            graphics.append(iv)  # cards and generated stills are new picture
        p = c.get("pip") or {}
        if p.get("src"):
            graphics.append(iv)
            if p["src"] in srcset:
                pip.append(iv)
                pin = float(p.get("in", 0))
                used[p["src"]].append((pin, pin + d))
        for t in c.get("text", []):
            graphics.append((st + float(t.get("start", 0)), st + min(d, float(t.get("end", d)))))
    narr = [(float(a.get("start", 0)), float(a.get("start", 0)) + _audio_dur(a)) for a in tl.get("audio", [])
            if a.get("role", "narration") == "narration"]
    narr = _intersect(narr, [(0.0, prog)])
    captions = [(q["start"], q["end"]) for q in tl.get("subtitles", [])]
    unmodified = _subtract(_subtract(picture, narr), graphics)
    # touching pieces count as one stretch, even across a cut between two excerpts (conservative)
    longest_unmod = max((b - a for a, b in _union(unmodified)), default=0.0)
    longest_src = max((b - a for a, b in _union(picture)), default=0.0)
    durs = {}
    for s in sources:
        d = (rp.get("sources", {}).get(s) or {}).get("duration") or ((analyses or {}).get(s) or {}).get("probe", {}).get("duration")
        if not d and Path(s).exists():
            d = ff.duration(s)
        durs[s] = float(d or 0.0)
    shares = {s: round(_len(used[s]) / durs[s], 4) if durs[s] else None for s in sources}
    r2 = lambda x: round(x, 3)
    third = _len(picture + stills + pip)
    rep = {
        "programme_s": r2(prog),
        "third_party_picture_s": r2(third), "source_moving_picture_s": r2(_len(picture)), "source_stills_s": r2(_len(stills)),
        "source_pip_s": r2(_len(pip)),
        "original_audio_kept_s": r2(_len(orig_audio)), "original_audio_replaced_s": r2(_len(picture) - _len(_intersect(picture, orig_audio))),
        "new_narration_s": r2(_len(narr)), "new_graphics_s": r2(_len(graphics)), "captions_s": r2(_len(captions)),
        "new_material_s": r2(_len(narr + graphics)),
        "source_clips": n_clips, "longest_source_stretch_s": r2(longest_src), "longest_unmodified_stretch_s": r2(longest_unmod),
        "source_used_s": {s: r2(_len(used[s])) for s in sources}, "source_duration_s": {s: r2(durs[s]) for s in sources},
        "source_share": shares, "max_source_share": max((v for v in shares.values() if v is not None), default=0.0),
        "commentary_share": round(_len(narr) / prog, 4) if prog else 0.0,
        "third_party_share": round(third / prog, 4) if prog else 0.0,
        "thresholds": {"min_narration_share": COMMENTARY_MIN_NARRATION_SHARE, "max_unmodified_stretch_s": COMMENTARY_MAX_UNMODIFIED_S,
                       "max_source_share": COMMENTARY_MAX_SOURCE_SHARE},
        "disclaimer": DISCLAIMER,
    }
    t = {"narration_ok": rep["commentary_share"] >= COMMENTARY_MIN_NARRATION_SHARE,
         "stretch_ok": longest_unmod <= COMMENTARY_MAX_UNMODIFIED_S + 1e-6,
         "source_share_ok": rep["max_source_share"] <= COMMENTARY_MAX_SOURCE_SHARE + 1e-6}
    t["passes"] = all(t.values())
    rep["commentary"] = t
    if rp.get("narration_overlap") is not None:
        rep["narration_overlap"] = rp["narration_overlap"]
    rep["text"] = (f"{prog:.1f}s programme: third-party picture {third:.1f}s ({rep['third_party_share']:.0%}), original audio kept "
                   f"{rep['original_audio_kept_s']:.1f}s / replaced {rep['original_audio_replaced_s']:.1f}s, new narration "
                   f"{rep['new_narration_s']:.1f}s ({rep['commentary_share']:.0%}), new graphics/text {rep['new_graphics_s']:.1f}s; "
                   f"{n_clips} source clip(s), longest unmodified stretch {longest_unmod:.1f}s, max source share "
                   f"{rep['max_source_share']:.0%}. Commentary thresholds {'met' if t['passes'] else 'NOT met'}. {DISCLAIMER}")
    return rep


def narration_overlap(narration: str, transcript: str, n: int = 4) -> float:
    """Share of the narration's n-word sequences that appear verbatim in the transcript (0 = all new wording)."""
    w = lambda s: re.findall(r"\w+", s.lower())
    a, b = w(narration), w(transcript)
    grams = lambda x: [tuple(x[i:i + n]) for i in range(len(x) - n + 1)]
    ga, gb = grams(a), set(grams(b))
    return round(sum(g in gb for g in ga) / len(ga), 3) if ga else 0.0


# ---------------------------------------------------------------- layout helpers
def _tts(project, text: str, lang: str) -> dict:
    out = project.dir / "audio" / f"rp_{hashlib.sha1(f'{lang}|{text}'.encode()).hexdigest()[:12]}.wav"
    r = tts.synthesize(text, out, lang)
    project.data.setdefault("generated", {})[str(out)] = {"license": "generated narration", "source": r.get("engine", "tts")}
    return r


def _narr_item(r: dict, start: float, clip_id: str, text: str, **kw) -> dict:
    return {"src": r["path"], "start": round(start, 3), "clip": clip_id, "role": "narration", "duration": round(float(r["duration"]), 3),
            "text": text, **kw}


def _freeze(project, m: dict, t: float, W: int, H: int) -> Path:
    """A freeze frame of the source composited as picture-in-picture over a blurred, darkened copy of itself."""
    out = project.dir / "work" / "repurpose" / f"freeze_{m['id']}_{int(t * 1000)}_{W}x{H}.png"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    fw = int(W * 0.46) // 2 * 2
    x, y = int(W * 0.50), int(H * 0.08)
    fc = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2,eq=brightness=-0.2[bg];"
          f"[b]scale={fw}:-2,pad=iw+8:ih+8:4:4:color=white[fg];[bg][fg]overlay={x}:{y}")
    ff.run(["-ss", f"{max(0.0, t):.3f}", "-i", m["path"], "-frames:v", "1", "-filter_complex", fc, out], op="repurpose-freeze")
    return out


def _source_entry(m: dict, analysis: dict | None) -> dict:
    d = (analysis or {}).get("probe", {}).get("duration") or ff.duration(m["path"])
    return {"media_id": m["id"], "name": m["name"], "duration": round(float(d), 3)}


def _segments(analysis: dict) -> list[dict]:
    segs = list(((analysis or {}).get("transcript") or {}).get("segments") or [])
    if segs and any("score" not in s for s in segs):
        for s, sc in zip(segs, analyze.score_segments(segs)):
            s.setdefault("score", sc)
    return segs


def _captions_from_sources(tl: dict, path: str, analysis: dict | None) -> None:
    try:
        edit.retime_subtitles(tl, {path: analysis} if analysis else {})
    except (KeyError, TypeError):  # transcript without word timings
        tl["subtitles"] = []


def _finish(tl: dict, narr_cues: list[dict]) -> dict:
    tl["subtitles"] = subs.fix_timing(list(tl.get("subtitles", [])) + narr_cues)
    starts = clip_starts(tl)
    for u in tl["repurpose"]["used"]:
        i = next(k for k, c in enumerate(tl["clips"]) if c["id"] == u["clip"])
        u["programme_start"] = round(starts[i], 3)
    return tl


def _match_gain(analysis: dict | None) -> float:
    """Linear gain that brings a source's dialogue to the narration loudness (measured LUFS from the analysis)."""
    lufs = ((analysis or {}).get("loudness") or {}).get("lufs")
    if lufs is None or lufs < -60:
        return 1.0
    return round(10 ** (max(-12.0, min(12.0, tts.MASTER_LUFS - lufs)) / 20), 3)


def _size(analysis: dict | None, options: dict) -> tuple[int, int]:
    v = (analysis or {}).get("probe", {}).get("video") or {}
    return int(options.get("width") or v.get("width") or 1280), int(options.get("height") or v.get("height") or 720)


# ---------------------------------------------------------------- layouts
def commentary_timeline(project, src: str, analysis: dict, comments: list[dict], lang: str = "en", **options) -> dict:
    """Source excerpt -> freeze (source audio stops) -> spoken commentary over a picture-in-picture freeze frame with a
    lower third and captions -> next excerpt. Excerpts are kept short (by default <= 0.6 x the following comment, max
    `excerpt_s`) so that new commentary dominates. options: title, excerpt_s (12), label ("Commentary"), width, height."""
    check_request(" ".join(str(c.get("text", "")) for c in comments) + " " + str(options.get("request", "")), options)
    m = _media(project, src)
    path = m["path"]
    W, H = _size(analysis, options)
    dur = (analysis or {}).get("probe", {}).get("duration") or ff.duration(path)
    segs = _segments(analysis)
    max_ex = float(options.get("excerpt_s", 12.0))
    label = options.get("label", "Commentary")
    tl = prj.new_timeline(W, H, lang)
    tl["audio_fx"] = "none"
    tl["repurpose"] = {"layout": "commentary", "sources": {path: _source_entry(m, analysis)}, "used": [], "narration": [], "comments": len(comments)}
    narr_items, cues = [], []

    def card(text: str, spoken: str, style: str = "title") -> None:
        r = _tts(project, spoken, lang)
        c = ops.color_clip(round(r["duration"] + 0.7, 3), "0x0b1020", text=[{"text": text, "style": style}], fade_in=0.3)
        tl["clips"].append(c)
        narr_items.append((len(tl["clips"]) - 1, 0.3, r, spoken))

    title = options.get("title") or f"Commentary: {Path(m['name']).stem.replace('_', ' ')}"
    card(title, options.get("intro") or f"{title}. Here is what is worth noticing.")
    prev = 0.0
    for k, cm in enumerate(sorted(comments, key=lambda c: float(c["at"]))):
        at = min(float(cm["at"]), dur)
        seg = next((s for s in segs if s["start"] <= at <= s["end"] + 0.3), None)
        end = min(dur, seg["end"] + 0.1) if seg and seg["end"] - at < 2.5 else at
        r = _tts(project, cm["text"], lang)
        ex = min(max_ex, max(2.0, 0.6 * float(r["duration"])))
        start = max(prev, end - ex)
        nxt = next((s["start"] for s in segs if start - 0.05 <= s["start"] < end - 1.0), None)
        start = max(prev, nxt - 0.1) if nxt is not None else start
        if end - start >= 1.0:
            c = ops.video_clip(path, start, end, label=f"excerpt {k + 1}")
            if (g := _match_gain(analysis)) != 1.0:
                c["volume"] = g  # source dialogue at the same loudness as the commentary voice
            tl["clips"].append(c)
            tl["repurpose"]["used"].append({"media_id": m["id"], "src": path, "in": c["in"], "out": c["out"], "clip": c["id"], "audio": "kept"})
        png = _freeze(project, m, max(0.0, end - 1.0 / tl["fps"]), W, H)
        f = ops.image_clip(str(png), round(r["duration"] + 0.6, 3), kenburns="none", derived_from=path, composited=True, at=round(end, 3),
                           provenance={"license": (_decl(m) or {}).get("basis", "undeclared"), "source": f"frame of {m['name']} at {end:.2f}s"},
                           text=[{"text": label, "style": "lower_third"}])
        tl["clips"].append(f)
        narr_items.append((len(tl["clips"]) - 1, 0.2, r, cm["text"]))
        prev = end
    card(options.get("outro_title") or "Sources", options.get("outro") or "Sources and credits are listed in the description.", "subtitle")
    tl["clips"][-1]["text"].append({"text": " | ".join(credits_block(project, tl))[:300] or m["name"], "style": "lower_third"})
    tl["clips"][-1]["fade_out"] = 0.5
    _captions_from_sources(tl, path, analysis)
    starts = clip_starts(tl)
    for i, off, r, text in narr_items:
        s = starts[i] + off
        tl["audio"].append(_narr_item(r, s, tl["clips"][i]["id"], text))
        tl["repurpose"]["narration"].append({"start": round(s, 3), "end": round(s + r["duration"], 3), "text": text, "clip": tl["clips"][i]["id"]})
        cues += subs.cues_from_text(text, s, s + float(r["duration"]))
    return _finish(tl, cues)


def _keywords(text: str, k: int = 3, avoid: Counter | None = None) -> list[str]:
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z'-]+", text.lower()) if w not in analyze.STOP and len(w) > 3]
    cnt = Counter(words)
    rank = sorted(cnt, key=lambda w: (-(cnt[w] / (1 + (avoid or Counter())[w] * 0.5)), words.index(w)))
    return rank[:k]


_FALLBACK_LINES = ["First, the focus is on {a} and {b}.", "Next comes {a}, and how it connects to {b}.",
                   "Then the talk turns to {a}, with {b} close behind.", "Finally, it all comes down to {a} and {b}."]


def _fallback_section(text: str, n: int, total: int, avoid: Counter) -> tuple[str, str]:
    kw = _keywords(text, 3, avoid) or ["the main idea", "what follows"]
    a, b = kw[0], kw[1] if len(kw) > 1 else "the details"
    tmpl = _FALLBACK_LINES[-1] if n == total - 1 and total > 1 else _FALLBACK_LINES[min(n, len(_FALLBACK_LINES) - 2)]
    line = tmpl.format(a=a, b=b)
    num = re.search(r"\b(\d[\d.,]*)\s*(percent|%|times|years|hours|megawatts|kilowatts|gigawatts|kwh|mw|gw)\b", text, re.I)
    if num:
        line += f" Key figure: {num.group(1)} {num.group(2).lower().replace('%', 'percent')}."
    return " ".join(w.capitalize() for w in kw[:2]), line


def _llm_ready() -> bool:
    try:
        m = models.catalog()["models"]["llm-qwen3-4b"]
        d = config.MODELS / m["dir"]
        return all((d / f).exists() for f in m["files"])
    except (KeyError, OSError):
        return False


def _llm_section(text: str, n: int, total: int) -> tuple[str, str] | None:
    from . import llm
    try:
        raw = llm.chat(f"Part {n + 1} of {total} of a talk, transcript:\n\"\"\"{text[:2500]}\"\"\"\n\n"
                       "Write NEW narration for a recap video of this part: 1 or 2 short sentences, at most 40 words, that "
                       "summarise it in your own words. Do not quote and do not reuse phrases from the transcript. "
                       "Then a heading of at most 4 words.\nAnswer in exactly this format and nothing else:\n"
                       "HEADING: <heading>\nNARRATION: <narration>",
                       "You write concise, original recap narration. You follow the output format exactly.", max_tokens=160, temperature=0.4)
    except Exception:  # model missing / load failure: the caller falls back
        return None
    h = re.search(r"^HEADING:\s*(.+)$", raw, re.M)
    t = re.search(r"^NARRATION:\s*(.+)$", raw, re.M)
    if not (h and t):
        return None
    line = t.group(1).strip(" \"'*")
    return h.group(1).strip(" \"'*#")[:40], line[:300]


def recap_timeline(project, src: str, analysis: dict, target_s: float = 90, lang: str = "en", **options) -> dict:
    """Summary video: the highest-scoring transcript segments (chronological, muted) under NEW narration that summarises each
    section, with section title cards, lower thirds and captions. options: use_llm (True), max_source_share (0.45),
    sections (auto), music (True), title, width, height."""
    check_request(str(options.get("request", "")), options)
    m = _media(project, src)
    path = m["path"]
    W, H = _size(analysis, options)
    dur = float((analysis or {}).get("probe", {}).get("duration") or ff.duration(path))
    segs = [s for s in _segments(analysis) if s["end"] - s["start"] >= 0.8]
    if not segs:
        raise ValueError("recap needs a transcript with speech segments (analyse the source with transcribe=True)")
    budget = min(float(target_s) * 0.75, dur * float(options.get("max_source_share", 0.45)))
    chosen, total = [], 0.0
    for s in sorted(segs, key=lambda s: -s.get("score", 0.0)):
        d = s["end"] - s["start"] + 0.3
        if chosen and total + d > budget:
            continue
        chosen.append(s)
        total += d
    chosen.sort(key=lambda s: s["start"])
    k = int(options.get("sections") or max(1, min(4, round(len(chosen) / 3))))
    per = -(-len(chosen) // k)
    groups = [chosen[i:i + per] for i in range(0, len(chosen), per)]
    full = " ".join(s["text"] for s in segs)
    avoid = Counter(w for g in groups for w in set(_keywords(" ".join(s["text"] for s in g), 6)))
    use_llm = options.get("use_llm", True) and _llm_ready()
    plan, planner = [], []
    for n, g in enumerate(groups):
        text = " ".join(s["text"].strip() for s in g)
        got = _llm_section(text, n, len(groups)) if use_llm else None
        if got and narration_overlap(got[1], full) > RECAP_MAX_OVERLAP:
            got = _llm_section(text, n, len(groups))  # one retry; too much copied wording
        if got and narration_overlap(got[1], full) <= RECAP_MAX_OVERLAP and len(got[1].split()) >= 5:
            planner.append("llm")
        else:
            got = _fallback_section(text, n, len(groups), avoid)
            planner.append("fallback")
        plan.append({"heading": got[0], "narration": got[1], "segs": g, "overlap": narration_overlap(got[1], full)})
    tl = prj.new_timeline(W, H, lang)
    tl["audio_fx"] = "none"
    tl["repurpose"] = {"layout": "recap", "sources": {path: _source_entry(m, analysis)}, "used": [], "narration": [],
                       "planner": "local LLM" if set(planner) == {"llm"} else "deterministic fallback" if set(planner) == {"fallback"} else "mixed",
                       "sections": [{"heading": p["heading"], "planner": pl} for p, pl in zip(plan, planner)]}
    title = options.get("title") or f"Recap: {Path(m['name']).stem.replace('_', ' ').title()}"
    intro = options.get("intro") or (f"The short version, in {len(plan)} parts." if len(plan) > 1 else "The short version, in one minute or less.")
    narr_items, cues = [], []
    r = _tts(project, intro, lang)
    tl["clips"].append(ops.color_clip(round(r["duration"] + 0.8, 3), "0x0b1020", text=[{"text": title, "style": "title"}], fade_in=0.4))
    narr_items.append((0, 0.3, r, intro, {}))
    for n, p in enumerate(plan):
        tl["clips"].append(ops.color_clip(1.6, "0x101828", text=[{"text": f"{n + 1}. {p['heading']}", "style": "kinetic"}]))
        r = _tts(project, p["narration"], lang)
        clips = [ops.video_clip(path, max(0.0, s["start"] - 0.15), min(dur, s["end"] + 0.15), mute=True) for s in p["segs"]]
        need = float(r["duration"]) + 0.8 - sum(clip_duration(c) for c in clips)
        if need > 0:  # stretch the section's footage (later, then earlier) so its narration fits inside it
            clips[-1]["out"] = round(min(dur, clips[-1]["out"] + need), 3)
            short = float(r["duration"]) + 0.8 - sum(clip_duration(c) for c in clips)
            if short > 0:
                clips[0]["in"] = round(max(0.0, clips[0]["in"] - short), 3)
        clips[0]["text"] = [{"text": p["heading"], "style": "lower_third", "start": 0.2, "end": min(3.0, clip_duration(clips[0]))}]
        first = len(tl["clips"])
        for c in clips:
            tl["clips"].append(c)
            tl["repurpose"]["used"].append({"media_id": m["id"], "src": path, "in": c["in"], "out": c["out"], "clip": c["id"], "audio": "muted"})
        narr_items.append((first, 0.2, r, p["narration"], {"spans_cuts": True, "overlap": p["overlap"]}))
    end = ops.color_clip(3.0, "0x0b1020", text=[{"text": "Source", "style": "subtitle"},
                                                {"text": " | ".join(credits_block(project, tl))[:300] or m["name"], "style": "lower_third"}], fade_out=0.6)
    tl["clips"].append(end)
    starts = clip_starts(tl)
    for i, off, r, text, extra in narr_items:
        s = starts[i] + off
        tl["audio"].append(_narr_item(r, s, tl["clips"][i]["id"], text, **({"spans_cuts": True} if extra.get("spans_cuts") else {})))
        tl["repurpose"]["narration"].append({"start": round(s, 3), "end": round(s + r["duration"], 3), "text": text,
                                             "clip": tl["clips"][i]["id"], **({"overlap": extra["overlap"]} if "overlap" in extra else {})})
        cues += subs.cues_from_text(text, s, s + float(r["duration"]))
    ov = [p["overlap"] for p in plan]
    tl["repurpose"]["narration_overlap"] = {"max": max(ov), "mean": round(sum(ov) / len(ov), 3), "limit": RECAP_MAX_OVERLAP,
                                            "ok": max(ov) <= RECAP_MAX_OVERLAP}
    if options.get("music", True):
        from .audio import music
        tl["music"] = {"src": str(music.music_bed(total_duration(tl), options.get("mood", "documentary"), int(options.get("seed", 5)))),
                       "volume": 0.3, "duck": True}
    return _finish(tl, cues)


def compilation_timeline(project, items: list[dict], title: str, lang: str = "en", **options) -> dict:
    """Themed compilation across declared sources: narrated intro, numbered title cards (optional spoken `note` per item),
    the item with its label as a lower third, and a narrated outro with credits.
    items: [{"media": media id or path, "in": s, "out": s, "label": str, "note": optional spoken line}]."""
    check_request(" ".join(f"{it.get('label', '')} {it.get('note', '')}" for it in items) + " " + title + " " + str(options.get("request", "")), options)
    if not items:
        raise ValueError("a compilation needs at least one item")
    W, H = int(options.get("width", 1280)), int(options.get("height", 720))
    tl = prj.new_timeline(W, H, lang)
    tl["audio_fx"] = "none"
    tl["repurpose"] = {"layout": "compilation", "sources": {}, "used": [], "narration": [], "title": title}
    narr_items, cues = [], []
    intro = options.get("intro") or f"{title}. {len(items)} moments, one theme."
    r = _tts(project, intro, lang)
    tl["clips"].append(ops.color_clip(round(r["duration"] + 0.8, 3), "0x0b1020", text=[{"text": title, "style": "title"}], fade_in=0.4))
    narr_items.append((0, 0.3, r, intro))
    for n, it in enumerate(items):
        m = _media(project, it["media"])
        if m["path"] not in tl["repurpose"]["sources"]:
            tl["repurpose"]["sources"][m["path"]] = _source_entry(m, (options.get("analyses") or {}).get(m["path"]))
        label = it.get("label") or Path(m["name"]).stem.replace("_", " ")
        cd = 1.8
        if it.get("note"):
            rn = _tts(project, it["note"], lang)
            cd = max(cd, round(float(rn["duration"]) + 0.7, 3))
        tl["clips"].append(ops.color_clip(cd, "0x101828", text=[{"text": f"#{n + 1}  {label}", "style": "kinetic"}]))
        if it.get("note"):
            narr_items.append((len(tl["clips"]) - 1, 0.25, rn, it["note"]))
        c = ops.video_clip(m["path"], float(it["in"]), float(it["out"]))
        if (g := _match_gain((options.get("analyses") or {}).get(m["path"]))) != 1.0:
            c["volume"] = g
        c["text"] = [{"text": label, "style": "lower_third", "start": 0.3, "end": min(3.0, clip_duration(c))}]
        tl["clips"].append(c)
        tl["repurpose"]["used"].append({"media_id": m["id"], "src": m["path"], "in": c["in"], "out": c["out"], "clip": c["id"], "audio": "kept"})
    outro = options.get("outro") or f"That was {title}. Every source is credited on screen and in the description."
    r = _tts(project, outro, lang)
    tl["clips"].append(ops.color_clip(round(max(3.0, r["duration"] + 0.8), 3), "0x0b1020",
                                      text=[{"text": "Credits", "style": "subtitle"},
                                            {"text": " | ".join(credits_block(project, tl))[:400], "style": "lower_third"}], fade_out=0.6))
    narr_items.append((len(tl["clips"]) - 1, 0.3, r, outro))
    ans = {p: a for p, a in (options.get("analyses") or {}).items() if p in tl["repurpose"]["sources"] and a}
    if ans:
        try:
            edit.retime_subtitles(tl, ans)
        except (KeyError, TypeError):
            tl["subtitles"] = []
    starts = clip_starts(tl)
    for i, off, r, text in narr_items:
        s = starts[i] + off
        tl["audio"].append(_narr_item(r, s, tl["clips"][i]["id"], text))
        tl["repurpose"]["narration"].append({"start": round(s, 3), "end": round(s + r["duration"], 3), "text": text, "clip": tl["clips"][i]["id"]})
        cues += subs.cues_from_text(text, s, s + float(r["duration"]))
    return _finish(tl, cues)


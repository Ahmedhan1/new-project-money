"""Platform knowledge base (registry/platforms.yaml) and per-platform tuning of a timeline.

Ranking systems are not public. `signals` in the YAML are only what the platforms have said themselves (with source + date)
or are labelled editorial rules of thumb. tune() turns a timeline into a platform cut and explains every change with the
signal or spec it serves; checklist() measures a timeline against the platform spec (same stage shape as qa); metadata()
writes title/description/hashtags within the platform's limits. Nothing here uploads anything.
"""
from __future__ import annotations

import copy
import functools
import re

import yaml

from . import config
from .timeline.render import clip_duration, clip_starts, total_duration

CONFIDENCE = ("official-statement", "official-guideline", "editorial-rule-of-thumb")
SHORT_KINDS = ("short",)
MAX_TRIM_SHARE = 0.4      # tune() never removes more than this share of a programme to hit a platform limit
CAPTION_BAND = 0.07       # rough height of one caption line as a fraction of frame height
STOP = set("this that with from your what have about they will just their there here been were into than then them these those "
           "when which would could should also very more most some such only over because while where".split())


@functools.lru_cache(maxsize=1)
def _load() -> dict:
    return yaml.safe_load((config.REGISTRY_DIR / "platforms.yaml").read_text(encoding="utf-8"))


def all_platforms() -> dict[str, dict]:
    return copy.deepcopy(_load()["platforms"])


def names() -> list[str]:
    return list(_load()["platforms"])


def spec(platform: str) -> dict:
    """Platform (or generic format, e.g. 'square') spec incl. signals. Aliases: reels -> instagram-reels, twitter -> x."""
    d = _load()
    p = {"reels": "instagram-reels", "twitter": "x", "youtube-shorts": "shorts", "snapchat": "snapchat-spotlight"}.get(platform, platform)
    s = d["platforms"].get(p) or d.get("formats", {}).get(p)
    if s is None:
        raise KeyError(f"unknown platform {platform!r}; known: {', '.join(names())}")
    s = copy.deepcopy(s)
    s["id"] = p
    s["size"] = tuple(s["size"])
    return s


def signals(platform: str) -> list[dict]:
    return spec(platform).get("signals", [])


def validate() -> list[str]:
    """Problems in platforms.yaml (empty list = valid)."""
    errs = []
    for pid, s in _load()["platforms"].items():
        for k in ("size", "fit", "max_s", "title_max", "desc_max", "hashtags", "safe_zone", "loudness_lufs", "captions", "kind"):
            if k not in s:
                errs.append(f"{pid}: missing spec {k}")
        if len(s.get("signals") or []) < 3:
            errs.append(f"{pid}: fewer than 3 signals")
        for sg in s.get("signals") or []:
            for k in ("id", "claim", "source", "read_on", "confidence", "implication"):
                if not sg.get(k):
                    errs.append(f"{pid}/{sg.get('id')}: missing {k}")
            if sg.get("confidence") not in CONFIDENCE:
                errs.append(f"{pid}/{sg.get('id')}: bad confidence {sg.get('confidence')}")
            src = sg.get("source")
            if isinstance(src, dict):
                if not str(src.get("url", "")).startswith("https://") or not src.get("title"):
                    errs.append(f"{pid}/{sg.get('id')}: source needs title + https url")
            elif not str(src).startswith("unverified"):
                errs.append(f"{pid}/{sg.get('id')}: a source without a URL must say 'unverified'")
            elif sg.get("confidence") != "editorial-rule-of-thumb":
                errs.append(f"{pid}/{sg.get('id')}: unverified source must be editorial-rule-of-thumb")
    return errs


def source_stats() -> dict:
    sourced, unverified = [], []
    for pid, s in _load()["platforms"].items():
        for sg in s["signals"]:
            (sourced if isinstance(sg["source"], dict) else unverified).append(f"{pid}/{sg['id']}")
    return {"sourced": sourced, "unverified": unverified}


def _sig(sp: dict, *prefer: str) -> str:
    """Id of the first matching signal (by id substring) to cite in a note; 'spec' if none."""
    for want in prefer:
        for sg in sp.get("signals", []):
            if want in sg["id"]:
                return sg["id"]
    return "spec"


def _note(notes: list, change: str, signal: str, sp: dict) -> None:
    conf = next((s["confidence"] for s in sp.get("signals", []) if s["id"] == signal), "spec" if signal == "spec" else "editorial-rule-of-thumb")
    notes.append({"change": change, "signal": signal, "confidence": conf})


# ---------------------------------------------------------------- helpers
def _is_card(c: dict) -> bool:
    return c["type"] == "color" and (c.get("role") in ("title_card", "chapter_card") or bool(c.get("text")))


def _clip_score(c: dict, analysis_by_src: dict | None) -> float:
    if c.get("score") is not None:
        return float(c["score"])
    an = (analysis_by_src or {}).get(c.get("src", ""))
    segs = ((an or {}).get("transcript") or {}).get("segments") or []
    sc = [segs[i].get("score", 0.0) for i in c.get("segs", []) if i < len(segs)]
    return max(sc) if sc else 0.0


def _delete(tl: dict, i: int) -> None:
    """Ripple-delete clip i in place (subtitles, narration and markers after it move up)."""
    from .timeline import ops
    new = ops.ripple_delete(tl, tl["clips"][i]["id"])
    tl.clear()
    tl.update(new)


def renderer_margin_v() -> bool:
    """True once the caption renderer (nexus.fx.captions, FX-1) honours tl['captions']['margin_v']; until then it is ignored."""
    try:
        from .fx import captions as fxcap
        return "margin_v" in getattr(fxcap, "DEFAULTS", {})
    except Exception:
        return False


def _caption_box(cap: dict | None, W: int, H: int) -> tuple[float, float] | None:
    """(top, bottom) of the caption band as fractions of the frame height, as the renderer places it."""
    if not cap:
        return None
    vertical = H > W
    pos = cap.get("position", "bottom")
    mv = cap.get("margin_v") if renderer_margin_v() else None
    if pos == "bottom":
        m = mv if mv is not None else (0.16 if vertical else 0.07)
        return (1 - m - CAPTION_BAND * 1.6, 1 - m)
    if pos == "top":
        m = mv if mv is not None else (0.12 if vertical else 0.06)
        return (m, m + CAPTION_BAND * 1.6)
    return (0.5 - CAPTION_BAND, 0.5 + CAPTION_BAND)


def _in_safe(box, sz: dict) -> bool:
    return box is None or (box[0] >= sz.get("top", 0) - 1e-6 and box[1] <= 1 - sz.get("bottom", 0) + 1e-6)


# ---------------------------------------------------------------- tune
def tune(tl: dict, platform: str, analysis_by_src: dict | None = None) -> tuple[dict, list[dict]]:
    """Adapt a timeline to one platform. Returns (new timeline, notes); every note names the signal or spec it serves."""
    sp = spec(platform)
    tl = copy.deepcopy(tl)
    notes: list[dict] = []
    W, H = sp["size"]
    short = sp["kind"] in SHORT_KINDS
    # 1. frame + fit
    if (tl["width"], tl["height"]) != (W, H):
        _note(notes, f"frame {tl['width']}x{tl['height']} -> {W}x{H}", "spec", sp)
    tl["width"], tl["height"] = W, H
    fits = set()
    for c in tl["clips"]:
        if c["type"] == "image":
            c["fit"] = "cover"
            continue
        if c["type"] != "video" or c.get("fit_override"):
            continue
        an = (analysis_by_src or {}).get(c.get("src", "")) or {}
        v = (an.get("probe") or {}).get("video") or {}
        src_vertical = bool(v) and v.get("height", 0) > v.get("width", 1)
        same_orientation = bool(v) and (src_vertical == (H > W))
        face = c.get("focus_source") == "face" or ((an.get("quality") or {}).get("face_focus_x") is not None)
        if sp["fit"] == "cover" or same_orientation:
            fit = "cover"
        elif face and H > W:
            fit = "cover"      # a detected subject: crop around it instead of blur bars
        else:
            fit = sp["fit"] if sp["fit"] in ("blur", "contain") else "blur"
        fx = (an.get("quality") or {}).get("face_focus_x")
        if fit == "cover" and fx is not None and not c.get("focus"):
            c["focus"] = [fx, 0.5]
        c["fit"] = fit
        fits.add(fit)
    if fits:
        _note(notes, f"fit: {', '.join(sorted(fits))} (cover = crop around the clip focus; blur = full picture on a blurred fill)",
              _sig(sp, "format", "lowres", "watermark") if sp["fit"] == "cover" else "spec", sp)
    if H > W:  # a 2.39 letterbox wastes most of a vertical frame
        lb = 0
        for c in tl["clips"]:
            if c.get("effects"):
                n0 = len(c["effects"])
                c["effects"] = [e for e in c["effects"] if e.get("type") != "letterbox"]
                lb += n0 - len(c["effects"])
        if lb:
            _note(notes, f"removed letterbox from {lb} clip(s) for the vertical frame", _sig(sp, "format", "lowres"), sp)
    # 2. title / chapter cards
    mode, cap_s = sp.get("title_card", "keep"), float(sp.get("title_card_max_s", 2.5))
    lead_title = None
    i = 0
    while i < len(tl["clips"]):
        c = tl["clips"][i]
        if _is_card(c):
            if mode == "remove" or (mode == "short" and i > 0 and c.get("role") == "chapter_card"):
                if i == 0 and c.get("text"):
                    lead_title = c["text"][0].get("text")
                _delete(tl, i)
                _note(notes, f"removed {c.get('role', 'title card')} ({clip_duration(c):.1f}s)", _sig(sp, "hook", "viewed", "completion"), sp)
                continue
            if clip_duration(c) > cap_s > 0:
                from .timeline import ops
                new = ops.trim(tl, c["id"], t_out=cap_s)
                tl.clear()
                tl.update(new)
                _note(notes, f"shortened {c.get('role', 'title card')} to {cap_s:.1f}s", _sig(sp, "hook", "satisfaction", "ranking"), sp)
        i += 1
    if not tl["clips"]:
        return tl, notes + [{"change": "nothing left to tune", "signal": "spec", "confidence": "spec"}]
    # 3. hook: a visual and a caption from the first second
    first = tl["clips"][0]
    if short:
        if first.get("fade_in"):
            first["fade_in"] = 0
            _note(notes, "removed the fade-in so the first frame is picture, not black", _sig(sp, "hook", "viewed"), sp)
        if lead_title and first["type"] == "video" and not first.get("text"):
            first["text"] = [{"text": lead_title, "style": "callout", "start": 0.0, "end": round(min(2.5, clip_duration(first)), 3)}]
            _note(notes, "title moved onto the first shot as a 2.5 s callout", _sig(sp, "hook", "viewed"), sp)
    # 4. duration policy
    _fit_duration(tl, sp, analysis_by_src, notes)
    # 5. captions inside the safe zone
    want = dict(sp.get("captions") or {})
    cur = dict(tl.get("captions") or {})
    style = want.get("style", "classic")
    if style in ("pop", "karaoke") and not any(q.get("words") for q in tl.get("subtitles", [])):
        if not _word_captions(tl, analysis_by_src, int(want.get("words_per_cue", 3))):
            style = "boxed"
            _note(notes, "no word timings: captions fall back to boxed sentence cues", "spec", sp)
    caps = {**cur, **want, "style": style}
    caps.pop("margin_v", None)
    sz = sp.get("safe_zone") or {}
    pillar = H > W and fits == {"blur"}
    if pillar and renderer_margin_v():
        # landscape picture in the middle of a vertical frame: captions go under the picture, above the platform UI
        caps["position"], caps["margin_v"] = "bottom", round(sz.get("bottom", 0) + 0.03, 3)
    elif pillar:
        _note(notes, "captions stay in the middle (over the picture): the renderer does not yet honour captions.margin_v", "spec", sp)
    box = _caption_box(caps, W, H)
    if not _in_safe(box, sz):
        if caps["position"] == "bottom":
            caps["margin_v"] = round(sz.get("bottom", 0) + 0.03, 3)
        box = _caption_box(caps, W, H)
        if not _in_safe(box, sz):
            caps["position"] = "middle"
            caps.pop("margin_v", None)
    tl["captions"] = caps
    if tl.get("subtitles"):
        b = _caption_box(caps, W, H)
        _note(notes, f"captions {caps['style']} at {caps['position']} (band {b[0]:.2f}-{b[1]:.2f} of height, UI-free zone "
                     f"{sz.get('top', 0):.2f}-{1 - sz.get('bottom', 0):.2f})", _sig(sp, "sound-off", "muted", "factors", "hook"), sp)
    elif sp.get("captions_required"):
        _note(notes, "WARNING: platform cut has no captions (no transcript)", _sig(sp, "sound-off", "muted"), sp)
    # 6. overlays: progress bar per platform; no watermark on platform cuts
    ov = [o for o in tl.get("overlays") or [] if o.get("type") not in ("progress_bar", "watermark")]
    if any(o.get("type") == "watermark" for o in tl.get("overlays") or []):
        _note(notes, "removed watermark overlay", _sig(sp, "originality", "watermark", "duplicates"), sp)
    if sp.get("progress_bar"):
        ov.append({"type": "progress_bar", "color": "#3EC5FF", "height": 8})
        _note(notes, "progress bar on", _sig(sp, "completion", "hook"), sp)
    tl["overlays"] = ov
    # 7. loudness
    if float(tl.get("target_lufs", -14.0)) != float(sp["loudness_lufs"]):
        _note(notes, f"loudness target {tl.get('target_lufs')} -> {sp['loudness_lufs']} LUFS", "spec", sp)
    tl["target_lufs"] = float(sp["loudness_lufs"])
    # 8. ending
    last = tl["clips"][-1]
    if sp.get("end") == "loop":
        if last.get("fade_out"):
            last["fade_out"] = 0
        if tl.get("music"):
            tl["music"]["fade_out"] = 0
        _note(notes, "loopable ending: no fade to black on the last shot", _sig(sp, "loop", "completion", "hook"), sp)
    else:
        last.setdefault("fade_out", 0.5)
        if sp.get("end") == "end-card":
            lo, hi = sp.get("end_card_s", [5, 20])
            _note(notes, f"end screen: add YouTube end-screen elements in Studio over the last {lo}-{hi} s (no extra card rendered)", "spec", sp)
    tl["end_style"] = sp.get("end")
    tl["platform"] = sp["id"]
    tl["platform_meta"] = {"hashtags": sp["hashtags"], "title_max": sp["title_max"], "desc_max": sp["desc_max"]}
    return tl, notes


def _word_captions(tl: dict, analysis_by_src: dict | None, per_cue: int) -> bool:
    try:
        from . import shorts
    except ImportError:
        return False
    words = shorts.programme_words(tl, analysis_by_src)
    if not words:
        return False
    tl["subtitles"] = shorts.word_cues(words, total_duration(tl), per_cue)
    return True


def _fit_duration(tl: dict, sp: dict, analysis_by_src: dict | None, notes: list) -> None:
    mx, mn = sp.get("max_s"), sp.get("min_s") or 0
    total = total_duration(tl)
    if mx and total > mx:
        need = total - mx
        if need > MAX_TRIM_SHARE * total:
            _note(notes, f"FLAG: {total:.0f}s is {need:.0f}s over the {sp['id']} limit of {mx}s; cutting that much would lose the "
                         f"meaning - make a dedicated short (director/shorts) instead", "spec", sp)
            tl["platform_flags"] = tl.get("platform_flags", []) + ["over_max_duration"]
            return
        # drop least important clips first (never the first shot = hook, never the last = conclusion)
        while total_duration(tl) > mx:
            cands = [(i, c) for i, c in enumerate(tl["clips"]) if 0 < i < len(tl["clips"]) - 1 and c["type"] in ("video", "image")]
            if not cands:
                break
            i, c = min(cands, key=lambda ic: (_clip_score(ic[1], analysis_by_src), -clip_duration(ic[1])))
            over = total_duration(tl) - mx
            d = clip_duration(c)
            _delete(tl, i)
            _note(notes, f"dropped least important shot ({d:.1f}s, score {_clip_score(c, analysis_by_src):.2f}) to fit {mx}s",
                  _sig(sp, "completion", "satisfaction", "length", "format"), sp)
            if d >= over:
                break
        if total_duration(tl) > mx:
            _trim_tail(tl, mx, analysis_by_src)
            _note(notes, f"trimmed the end to fit {mx}s", "spec", sp)
    if total_duration(tl) < mn:
        _note(notes, f"FLAG: {total_duration(tl):.1f}s is shorter than the {sp['id']} minimum of {mn}s", "spec", sp)
        tl["platform_flags"] = tl.get("platform_flags", []) + ["under_min_duration"]
    lo, hi = sp.get("ideal_s") or (None, None)
    if hi and total_duration(tl) > hi and not (mx and total_duration(tl) > mx):
        _note(notes, f"note: {total_duration(tl):.0f}s is above the editorial sweet spot {lo}-{hi}s for {sp['id']} (not a platform rule)",
              "spec", sp)


def _trim_tail(tl: dict, mx: float, analysis_by_src: dict | None) -> None:
    """Shorten the last video clip so the programme ends at <= mx, at a word end when word timings are known."""
    from .timeline import ops
    c = tl["clips"][-1]
    if c["type"] != "video":
        return
    start = clip_starts(tl)[-1]
    sp = float(c.get("speed", 1.0))
    t_out = c["in"] + (mx - start - 0.05) * sp
    an = (analysis_by_src or {}).get(c.get("src", "")) or {}
    ends = [w["end"] for s in (an.get("transcript") or {}).get("segments", []) for w in s.get("words") or []
            if c["in"] < w["end"] <= t_out]
    if ends:
        t_out = max(ends) + 0.05 if max(ends) + 0.05 <= t_out else max(ends)
    if t_out > c["in"] + 0.4:
        new = ops.trim(tl, c["id"], t_out=t_out)
        tl.clear()
        tl.update(new)
        tl["subtitles"] = [q for q in tl.get("subtitles", []) if q["start"] < mx]
        for q in tl["subtitles"]:
            q["end"] = min(q["end"], mx)


# ---------------------------------------------------------------- checklist
def _c(name: str, ok: bool, detail: str, severity: str = "major", fix: str | None = None) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, "severity": "info" if ok else severity, "fix": None if ok else fix}


def checklist(tl: dict, platform: str) -> dict:
    """Pass/fail of a timeline against the platform spec. Same shape as the qa stages."""
    sp = spec(platform)
    W, H = sp["size"]
    checks = []
    total = total_duration(tl)
    checks.append(_c("frame size", (tl["width"], tl["height"]) == (W, H), f"{tl['width']}x{tl['height']} (spec {W}x{H})", "critical", "platforms.tune"))
    if sp.get("max_s"):
        checks.append(_c("max duration", total <= sp["max_s"] + 0.05, f"{total:.1f}s (limit {sp['max_s']}s)", "critical", "make a shorter cut"))
    checks.append(_c("min duration", total >= (sp.get("min_s") or 0), f"{total:.1f}s (minimum {sp.get('min_s') or 0}s)", "critical"))
    lo, hi = sp.get("ideal_s") or (0, 0)
    if hi:
        checks.append(_c("editorial length", lo <= total <= hi, f"{total:.0f}s vs editorial {lo}-{hi}s (rule of thumb)", "minor"))
    subs = tl.get("subtitles") or []
    if sp.get("captions_required"):
        checks.append(_c("captions present", bool(subs), f"{len(subs)} cues", "major", "add captions from transcript"))
    box = _caption_box(tl.get("captions") or {"position": "bottom"}, tl["width"], tl["height"]) if subs else None
    sz = sp.get("safe_zone") or {}
    checks.append(_c("captions inside UI-safe zone", _in_safe(box, sz),
                     "no captions" if box is None else f"band {box[0]:.2f}-{box[1]:.2f}, safe {sz.get('top', 0):.2f}-{1 - sz.get('bottom', 0):.2f}",
                     "major", "platforms.tune"))
    if sp["kind"] in SHORT_KINDS and tl["clips"]:
        c0 = tl["clips"][0]
        black = _is_card(c0) or float(c0.get("fade_in", 0) or 0) > 0.2
        checks.append(_c("first-second visual", not black, "title card / fade at 0 s" if black else "picture from frame 1", "major", "platforms.tune"))
        first_cap = min((q["start"] for q in subs), default=None)
        checks.append(_c("caption in first second", first_cap is not None and first_cap <= sp.get("hook_s", 1.0),
                         "none" if first_cap is None else f"first caption at {first_cap:.2f}s (hook {sp.get('hook_s', 1.0)}s)", "major"))
        cards = [c for c in tl["clips"] if _is_card(c)]
        checks.append(_c("no title cards", not cards, f"{len(cards)} card(s)", "minor", "platforms.tune"))
    has_pb = any(o.get("type") == "progress_bar" for o in tl.get("overlays") or [])
    checks.append(_c("progress bar setting", has_pb == bool(sp.get("progress_bar")), f"{'on' if has_pb else 'off'} (platform: "
                     f"{'on' if sp.get('progress_bar') else 'off'})", "minor", "platforms.tune"))
    wm = any(o.get("type") == "watermark" for o in tl.get("overlays") or [])
    checks.append(_c("no watermark overlay", not wm, "watermark present" if wm else "none", "major", "platforms.tune"))
    checks.append(_c("loudness target", abs(float(tl.get("target_lufs", -14)) - float(sp["loudness_lufs"])) < 0.1,
                     f"{tl.get('target_lufs', -14)} LUFS (spec {sp['loudness_lufs']})", "minor", "platforms.tune"))
    if sp.get("end") == "loop" and tl["clips"]:
        fo = float(tl["clips"][-1].get("fade_out", 0) or 0)
        checks.append(_c("loopable ending", fo == 0, "no fade-out" if fo == 0 else f"fade-out {fo}s breaks the loop", "minor", "platforms.tune"))
    return {"stage": f"platform-{sp['id']}", "platform": sp["id"], "checks": checks,
            "passed": all(c["ok"] or c["severity"] == "minor" for c in checks)}


# ---------------------------------------------------------------- metadata
def _cut(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    cut = s[: max(0, n - 1)].rsplit(" ", 1)[0].rstrip(",;:-")
    return (cut or s[: n - 1]) + "…"


def hashtags(text: str, n: int) -> list[str]:
    words = re.findall(r"[^\W\d_]{4,}", text)
    seen, out = set(), []
    for w in words:
        lw = w.lower()
        if lw not in seen and lw not in STOP:
            seen.add(lw)
            out.append("#" + w[0].upper() + w[1:])
    counts = {w: sum(1 for x in words if x.lower() == w[1:].lower()) for w in out}
    return sorted(out, key=lambda w: -counts[w])[:n] if n else []


def _chapters_text(tl: dict) -> str:
    marks = [m for m in tl.get("markers", []) if str(m.get("label", "")).startswith("chapter:")]
    if len(marks) < 3 or marks[0]["t"] > 0.5 or any(b["t"] - a["t"] < 10 for a, b in zip(marks, marks[1:])):
        return ""

    def ts(t):
        return f"{int(t // 60)}:{int(t % 60):02d}"
    return "\n".join(f"{ts(0 if k == 0 else m['t'])} {m['label'][8:].strip()}" for k, m in enumerate(marks))


def metadata(project, tl: dict, platform: str, lang: str = "en", title: str | None = None, description: str | None = None) -> dict:
    """Title / description / hashtags within the platform limits, with credits. Nothing is posted."""
    sp = spec(platform)
    data = getattr(project, "data", {}) or {}
    script = data.get("script") or {}
    title = title or tl.get("title") or script.get("title") or getattr(project, "name", "") or "Untitled"
    desc = (description or tl.get("description") or script.get("topic") or "").strip()
    if not desc:
        desc = " ".join(q["text"] for q in (tl.get("subtitles") or [])[:6])
    if sp.get("chapters"):
        ch = _chapters_text(tl)
        if ch:
            desc += "\n\nChapters:\n" + ch
    credits = data.get("credits", [])
    tags = hashtags(f"{title} {desc} " + " ".join(s.get("on_screen_text", "") for s in script.get("scenes", [])), sp["hashtags"])
    tag_line = " ".join(tags)
    tail = ("\n\nCredits:\n" + "\n".join(f"- {c}" for c in credits)) if credits else ""
    room = sp["desc_max"] - len(tail) - (len(tag_line) + 2 if tag_line else 0)
    body = _cut(desc, max(0, room)) + tail + (("\n\n" + tag_line) if tag_line else "")
    if len(body) > sp["desc_max"]:  # tiny limits (Snapchat 160, X 280): credits win over the description text
        body = _cut(tail.strip() + (" " + tag_line if tag_line else ""), sp["desc_max"])
    return {"platform": sp["id"], "language": lang, "title": _cut(title, sp["title_max"]), "description": body, "hashtags": tags,
            "limits": {"title_max": sp["title_max"], "desc_max": sp["desc_max"], "hashtags": sp["hashtags"]}, "credits": credits}

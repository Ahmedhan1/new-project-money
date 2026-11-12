"""Per-platform packaging intelligence: title / description / hashtags / CTA / thumbnail text / caption placement,
checked against the capability matrix limits. Text comes from the model router (task "title_generation", privacy
"internal") when a provider is eligible; otherwise (or on invalid output) a deterministic fallback built from the
transcript is used and the result says so. Hashtags proposed by a model are kept only if the word was actually said.
"""
from __future__ import annotations

import re

from .. import contracts as C
from . import capabilities

HASHTAGS_EDITORIAL = 5
# Editorial (NEXUS) choices, not platform data.
CTA = {"youtube": "Subscribe for the next one.", "youtube_shorts": "Follow for more.", "tiktok": "Follow for part 2.",
       "instagram_reels": "Save this for later.", "instagram_feed": "Save this for later.", "facebook": "Share it with someone who needs it.",
       "snapchat_spotlight": ""}
THUMB_WORDS = 4
SCHEMA = {"type": "object", "required": ["title", "description", "hashtags", "thumbnail_text"],
          "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "hashtags": {"type": "array"},
                         "thumbnail_text": {"type": "string"}, "cta": {"type": "string"}}}


def _clip(s: str, n: int | None) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    if n and len(s) > n:
        s = s[: max(0, n - 1)].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return s


def _clip_bytes(s: str, n: int) -> str:
    while len(s.encode("utf-8")) > n:
        s = s[:-2].rstrip() + "…"
    return s


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _clip_utf16(s: str, n: int) -> str:
    while utf16_len(s) > n:
        s = s[:-2].rstrip() + "…"
    return s


def caption_placement(platform: str) -> dict:
    """Vertical band (fractions of frame height) where burned-in captions stay clear of platform UI."""
    z = capabilities.limits(platform).get("safe_zone")
    if not z:
        return {"band": [0.70, 0.90], "basis": "no safe zone documented; lower-third default (editorial)"}
    lo, hi = float(z.get("top", 0)), 1.0 - float(z.get("bottom", 0))
    return {"band": [round(lo, 3), round(hi, 3)], "anchor_y": round(hi - 0.08, 3),
            "x_margin": [float(z.get("left", 0)), float(z.get("right", 0))],
            "basis": "platforms.yaml safe_zone (" + str(capabilities.get(platform)["media"]["safe_zone"].get("kind")) + ")"}


def _said(tag: str, text: str) -> bool:
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", tag.lstrip("#"))
    low = (text or "").lower()
    return bool(words) and all(w.lower() in low for w in words)


def _router_draft(platform: str, transcript: str, title: str | None, language: str) -> tuple[dict | None, str]:
    try:
        from ..model_router import NoProviderAvailable, complete
    except Exception as e:  # router not importable
        return None, f"deterministic (router unavailable: {type(e).__name__})"
    lim = capabilities.limits(platform)
    prompt = (f"Platform: {platform}. Language: {language}. Write packaging for this video using ONLY facts from the transcript. "
              f"Title <= {lim['title_max'] or 100} chars, no clickbait claims, no promises of results. Description: 1-3 sentences. "
              f"Up to {HASHTAGS_EDITORIAL} hashtags made of words in the transcript. thumbnail_text: <= {THUMB_WORDS} words. "
              f"Return JSON with title, description, hashtags, thumbnail_text, cta.\n"
              + (f"Working title: {title}\n" if title else "") + f"Transcript:\n{transcript[:4000]}")
    try:
        r = complete("title_generation", prompt, schema=SCHEMA, privacy="internal", max_tokens=500)
    except NoProviderAvailable as e:
        return None, f"deterministic (no provider: {'; '.join(e.reasons[:2])})"
    except Exception as e:
        return None, f"deterministic (router error: {type(e).__name__})"
    return (r.data if isinstance(r.data, dict) else None), f"router:{r.provider}/{r.model}"


def _deterministic(platform: str, transcript: str, title: str | None) -> dict:
    from ... import growth
    lim = capabilities.limits(platform)
    d = growth.platform_text(transcript, title, platform, limits={"title_max": lim["title_max"] or 100,
                                                                 "hashtags_max": lim["hashtags_max"] or HASHTAGS_EDITORIAL})
    words = re.findall(r"\w+", d["title"])
    return {"title": d["title"], "description": d["description"], "hashtags": d["hashtags"],
            "thumbnail_text": " ".join(words[:THUMB_WORDS]).upper(), "cta": CTA.get(platform, "")}


def package(platform: str, transcript: str, title: str | None = None, language: str = "en", duration_s: float | None = None,
            use_router: bool = True) -> dict:
    lim = capabilities.limits(platform)
    draft, source = (_router_draft(platform, transcript, title, language) if use_router else (None, "deterministic (router off)"))
    det = _deterministic(platform, transcript, title)
    if not draft or not str(draft.get("title", "")).strip():
        draft, source = det, (source if source.startswith("deterministic") else source + " -> invalid, deterministic")
    n_tags = min(int(lim["hashtags_max"] or HASHTAGS_EDITORIAL), HASHTAGS_EDITORIAL)
    tags = ["#" + re.sub(r"\W", "", str(t).lstrip("#")) for t in draft.get("hashtags") or []]
    dropped = [t for t in tags if not _said(t, transcript)]
    tags = [t for t in tags if t not in dropped and len(t) > 1] or det["hashtags"]
    tags = list(dict.fromkeys(tags))[:n_tags]
    t = _clip(draft.get("title") or det["title"], lim["title_max"] or 100)
    desc = _clip(draft.get("description") or det["description"], lim["description_max"])
    cta = draft.get("cta") if draft.get("cta") is not None else CTA.get(platform, "")
    if platform.startswith("youtube"):
        t, desc = t.replace("<", "").replace(">", ""), desc.replace("<", "").replace(">", "")
    full_desc = "\n\n".join(x for x in (desc, cta, " ".join(tags)) if x)
    if lim["description_max_bytes"]:
        full_desc = _clip_bytes(full_desc, int(lim["description_max_bytes"]))
    caption = None
    if lim["caption_max"]:
        caption = "\n\n".join(x for x in ((t if not lim["title_max"] else ""), desc, cta, " ".join(tags)) if x)
        caption = _clip_utf16(caption, int(lim["caption_max"])) if lim["caption_utf16"] else _clip(caption, int(lim["caption_max"]))
    thumb_text = " ".join(re.findall(r"\S+", draft.get("thumbnail_text") or det["thumbnail_text"])[:THUMB_WORDS])
    checks = [
        {"id": "title_len", "value": len(t), "limit": lim["title_max"], "ok": not lim["title_max"] or len(t) <= lim["title_max"]},
        {"id": "description_bytes", "value": len(full_desc.encode()), "limit": lim["description_max_bytes"],
         "ok": not lim["description_max_bytes"] or len(full_desc.encode()) <= lim["description_max_bytes"]},
        {"id": "description_chars", "value": len(full_desc), "limit": lim["description_max"],
         "ok": not lim["description_max"] or len(full_desc) <= lim["description_max"]},
        {"id": "caption_len", "value": (utf16_len(caption) if lim["caption_utf16"] else len(caption)) if caption else 0,
         "limit": lim["caption_max"], "ok": not caption or (utf16_len(caption) if lim["caption_utf16"] else len(caption)) <= lim["caption_max"]},
        {"id": "hashtags", "value": len(tags), "limit": lim["hashtags_max"], "ok": not lim["hashtags_max"] or len(tags) <= lim["hashtags_max"]},
        {"id": "no_angle_brackets", "value": "<" in t + full_desc or ">" in t + full_desc, "limit": False,
         "ok": not platform.startswith("youtube") or not ("<" in t + full_desc or ">" in t + full_desc)},
    ]
    if duration_s is not None:
        checks.append({"id": "duration", "value": duration_s, "limit": lim["duration_max_s"],
                       "ok": not lim["duration_max_s"] or duration_s <= lim["duration_max_s"]})
    cap = capabilities.get(platform)
    doc = C.make("PlatformMetadata", platform=platform, title=t if lim["title_max"] else "", description=full_desc, tags=tags,
                 language=language, aspect=lim["aspect"] or "", duration_s=duration_s,
                 thumbnail={"text": thumb_text, "spec": lim["thumbnail"]}, checks=checks,
                 spec_version=f"platforms.yaml {cap['media_reviewed_on']} + capabilities {cap['api_verified_on']}")
    doc.update(caption=caption, cta=cta, caption_placement=caption_placement(platform), text_source=source,
               dropped_hashtags=dropped, ok=all(c["ok"] for c in checks))
    return doc

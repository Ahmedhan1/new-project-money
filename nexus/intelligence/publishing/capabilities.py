"""Platform capability matrix = media specs (nexus/registry/platforms.yaml, read via nexus.platforms) + API reality
(capabilities.yaml in this package). Machine-readable; docs/ACCOUNT3_PLATFORM_CAPABILITIES.md is the human version.

    matrix() -> {platform: {display, media: {field: {value, kind, source, read_on}}, api: {...}, adapter, ...}}
    get(platform) -> one row (same_as resolved)
    registry_diff() -> corrections to platforms.yaml, raised as integration requests (never applied here)
"""
from __future__ import annotations

import copy
import functools
from pathlib import Path

import yaml

PLATFORMS = ("youtube", "youtube_shorts", "tiktok", "instagram_reels", "instagram_feed", "facebook", "snapchat_spotlight")
MEDIA_FIELDS = ("aspect_recommended", "resolution_recommended", "resolution_max", "fps", "duration_max_s", "max_file_bytes",
                "title_max_chars", "description_max_chars", "description_max_bytes", "caption_max_chars", "caption_max_utf16",
                "tags_max_chars", "hashtags_max", "thumbnail", "caption_files", "safe_zone", "music_policy")
API_KEYS = ("upload", "quota", "oauth", "analytics")


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    return yaml.safe_load((Path(__file__).with_name("capabilities.yaml")).read_text(encoding="utf-8"))


def verified_on() -> str:
    return str(_data()["verified"])


def api(platform: str) -> dict:
    p = _data()["platforms"]
    if platform not in p:
        raise KeyError(f"unknown platform {platform!r} (known: {', '.join(PLATFORMS)})")
    row = copy.deepcopy(p[platform])
    if row.get("same_as"):
        base = copy.deepcopy(p[row["same_as"]])
        base.update({k: v for k, v in row.items() if k != "same_as"})
        base["api_same_as"] = row["same_as"]
        row = base
    return row


def get(platform: str) -> dict:
    from ... import platforms as plat
    prof = plat.get(platform)
    a = api(platform)
    return {"platform": platform, "display": prof.get("display", platform),
            "media": {f: prof["fields"][f] for f in MEDIA_FIELDS if f in prof["fields"]},
            "api": {k: a[k] for k in API_KEYS if k in a},
            "adapter": a.get("adapter"), "adapter_status": a.get("adapter_status"),
            "default_visibility": a.get("default_visibility", "private"),
            "media_reviewed_on": prof.get("reviewed_on"), "api_verified_on": verified_on()}


def matrix() -> dict:
    return {p: get(p) for p in PLATFORMS}


def limits(platform: str) -> dict:
    """Plain text limits used by packaging checks (None = no documented limit)."""
    m = {k: (v or {}).get("value") for k, v in get(platform)["media"].items()}
    return {"title_max": m.get("title_max_chars"), "description_max": m.get("description_max_chars"),
            "description_max_bytes": m.get("description_max_bytes"),
            "caption_max": m.get("caption_max_chars") or m.get("caption_max_utf16"),
            "caption_utf16": bool(m.get("caption_max_utf16")), "hashtags_max": m.get("hashtags_max"),
            "tags_max_chars": m.get("tags_max_chars"), "duration_max_s": m.get("duration_max_s"),
            "max_file_bytes": m.get("max_file_bytes"), "safe_zone": m.get("safe_zone"), "thumbnail": m.get("thumbnail"),
            "aspect": m.get("aspect_recommended")}


def registry_diff() -> list[dict]:
    """Listed corrections + automatic ones: API facts documented here but null/unverified in platforms.yaml."""
    from ... import platforms as plat
    out = [dict(c, origin="capabilities.yaml corrections") for c in _data().get("corrections", [])]
    seen = {(c["platform"], c["field"]) for c in out}
    for p in PLATFORMS:
        a, f = api(p), plat.get(p)["fields"]
        for ours, theirs in (("analytics", "analytics_api"), ("upload", "upload_api")):
            node = a.get(ours) or {}
            documented = node.get("kind") == "documented" or any(isinstance(v, dict) and v.get("kind") == "documented"
                                                                   for v in node.values())
            if documented and (f.get(theirs) or {}).get("value") is None and (p, theirs) not in seen:
                out.append({"platform": p, "field": theirs, "registry_says": None,
                            "correct": "documented in capabilities.yaml (see api." + ours + ")", "origin": "automatic diff"})
    return out


# ------------------------------------------------------------------ provider readiness (honest, evidence-backed)
READINESS_CLASSES = ("real_provider_verified", "automated_test_verified", "implemented_not_live_tested",
                     "requires_owner_authorization", "manual_export_only", "not_supported")
_EVIDENCE = {
    "youtube": (["automated_test_verified", "implemented_not_live_tested", "requires_owner_authorization"],
                "resumable upload, resume after 5xx, thumbnails, quota ledger, containsSyntheticMedia and analytics import tested "
                "against recorded responses (test_intel_publishing, test_intel_release_gate, test_intel_owner_validation); no "
                "live upload yet: needs the owner's OAuth client (PUB-1) and owner_validation youtube --i-authorize-a-private-upload"),
    "tiktok": (["implemented_not_live_tested", "requires_owner_authorization"],
               "documented request flow incl. post_info.is_aigc; refuses without an approved TikTok app and token; unaudited apps "
               "post SELF_ONLY"),
    "instagram_reels": (["implemented_not_live_tested", "requires_owner_authorization"],
                        "container + resumable upload + is_ai_generated; no private mode, so media_publish only after owner approval"),
    "facebook": (["implemented_not_live_tested", "requires_owner_authorization"],
                 "Page /videos upload; this endpoint carries no AI label, so AI content is refused (manual upload with Meta's label)"),
    "snapchat_spotlight": (["manual_export_only"], "no organic upload API; package + MANUAL_UPLOAD.md only"),
}
_EVIDENCE["youtube_shorts"] = _EVIDENCE["youtube"]
_EVIDENCE["instagram_feed"] = _EVIDENCE["instagram_reels"]


def readiness() -> dict:
    """{platform: {classes, primary, live_verified, evidence}}. `live_verified` is True only for real_provider_verified."""
    out = {}
    for p in PLATFORMS:
        classes, ev = _EVIDENCE[p]
        assert set(classes) <= set(READINESS_CLASSES)
        out[p] = {"classes": classes, "primary": classes[-1] if "requires_owner_authorization" in classes else classes[0],
                  "live_verified": "real_provider_verified" in classes, "adapter_status": api(p).get("adapter_status"), "evidence": ev}
    return out

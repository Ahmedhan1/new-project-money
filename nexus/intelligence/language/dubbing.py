"""Dubbing manifest: per line {text, voice, licence, consent, timing target, max speed}. Voice selection refuses
non-commercial or unverified voices for commercial output; voice cloning needs a recorded consent document.

Rendering belongs to Account 2. `render_demo_line` calls the existing nexus.speech.tts.synthesize for one line only
(demo evidence), inside the shared heavy-work lock.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from . import matrix

# rough speaking rates (characters per second of synthetic narration) used only to PRE-check fit before rendering;
# heuristic engineering values, not measured per voice - the render's real duration decides.
SPEECH_CPS = {"zh": 4.5, "ja": 7.0, "ko": 7.0, "ar": 12.0, "hi": 13.0}
DEFAULT_SPEECH_CPS = 14.0
MAX_SPEED = 1.25          # fastest acceptable read (same as localize voice-over path's brisk read)
MIN_PICTURE_SPEED = 0.85  # localize.VOICEOVER_MIN_SPEED


class VoiceRefused(LookupError):
    pass


# ------------------------------------------------------------------ consent
def consent_record(speaker: str, document: str | Path, languages: list[str], uses: list[str], granted_on: str,
                   expires: str | None = None, witness: str | None = None) -> dict:
    """A consent record for cloning `speaker`'s voice. `document` must be an existing signed file (its sha256 is
    stored, the file itself never leaves the machine)."""
    p = Path(document)
    if not p.is_file() or p.stat().st_size == 0:
        raise ValueError(f"consent document {p} does not exist or is empty")
    return {"consent_id": "consent_" + hashlib.sha256(f"{speaker}{granted_on}{p.name}".encode()).hexdigest()[:10],
            "speaker": speaker, "document": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "languages": languages, "uses": uses, "granted_on": granted_on, "expires": expires, "revoked": False,
            "witness": witness, "recorded": time.strftime("%Y-%m-%d")}


def consent_problems(consent: dict | None, lang: str, use: str = "dubbing", today: str | None = None) -> list[str]:
    if not consent:
        return ["no consent record"]
    today = today or time.strftime("%Y-%m-%d")
    errs = []
    if consent.get("revoked"):
        errs.append("consent revoked")
    if consent.get("expires") and consent["expires"] < today:
        errs.append(f"consent expired on {consent['expires']}")
    if lang not in consent.get("languages", []) and "*" not in consent.get("languages", []):
        errs.append(f"consent does not cover language {lang}")
    if use not in consent.get("uses", []):
        errs.append(f"consent does not cover use '{use}'")
    p = Path(consent.get("document", ""))
    if not p.is_file():
        errs.append("consent document missing")
    elif hashlib.sha256(p.read_bytes()).hexdigest() != consent.get("sha256"):
        errs.append("consent document changed since it was recorded (hash mismatch)")
    return errs


# ------------------------------------------------------------------ voice selection
def select_voice(lang: str, commercial: bool = True, installed_only: bool = False, clone: dict | None = None) -> dict:
    """Pick a voice for `lang`. Commercial output: only commercial_use == "yes". `clone` = {"engine", "consent"}
    selects a cloning engine and requires consent covering this language. Raises VoiceRefused with the reasons."""
    if clone:
        eng = matrix.engine(clone["engine"])
        why = []
        if commercial and eng.get("commercial_use") != "yes":
            why.append(f"{clone['engine']} licence {eng.get('licence')} is not cleared for commercial use")
        langs = eng.get("languages")
        if langs and lang.split("-")[0] not in [x.split("-")[0] for x in langs]:
            why.append(f"{clone['engine']} does not support {lang}")
        why += consent_problems(clone.get("consent"), lang)
        if why:
            raise VoiceRefused(f"voice cloning refused for {lang}: " + "; ".join(why))
        c = clone["consent"]
        return {"id": f"clone:{c['speaker']}", "engine": clone["engine"], "licence": eng["licence"],
                "commercial_use": eng["commercial_use"], "consent_id": c["consent_id"], "installed": eng.get("installed", False),
                "restrictions": eng.get("restrictions")}
    voices = matrix.language(lang).get("voices") or []
    if not commercial:
        ok = [v for v in voices if v.get("commercial_use") in ("yes", "unverified", "no")]
    else:
        ok = [v for v in voices if v.get("commercial_use") == "yes"]
    if installed_only:
        ok = [v for v in ok if v.get("installed")]
    if not ok:
        states = ", ".join(f"{v['id']} ({v['commercial_use']}{'' if v.get('installed') else ', not installed'})" for v in voices) or "none"
        raise VoiceRefused(f"no {'commercially cleared ' if commercial else ''}{'installed ' if installed_only else ''}voice for {lang} "
                           f"[{states}] - deliver subtitles, or supply a voice you hold the rights to")
    ok.sort(key=lambda v: (not v.get("installed"), {"yes": 0, "unverified": 1, "no": 2}[v["commercial_use"]]))
    v = dict(ok[0])
    if v["commercial_use"] != "yes":
        v["licence_warning"] = f"{v['id']}: commercial use {v['commercial_use']} - not for monetised or client work"
    return v


# ------------------------------------------------------------------ manifest
def estimate_seconds(text: str, lang: str) -> float:
    rate = SPEECH_CPS.get(lang.split("-")[0], DEFAULT_SPEECH_CPS)
    return round(len(text.replace("\n", " ")) / rate, 2)


def build_manifest(lines: list[dict], lang: str, commercial: bool = True, installed_only: bool = False,
                   clone: dict | None = None, project_id: str = "") -> dict:
    """lines = [{text, start, end}] (target language). Returns a dubbing manifest; `voice` is None (and `blocked`
    says why) when no acceptable voice exists - the manifest is still produced so the lines can be reviewed."""
    try:
        voice, blocked = select_voice(lang, commercial, installed_only, clone), None
    except VoiceRefused as e:
        voice, blocked = None, str(e)
    out = []
    for i, ln in enumerate(lines):
        slot = round(float(ln["end"]) - float(ln["start"]), 3)
        est = estimate_seconds(ln["text"], lang)
        need = round(est / slot, 3) if slot > 0 else None
        action = "ok"
        if need is not None and need > 1.0:
            action = "brisker_read" if need <= MAX_SPEED else "shorten_line"
        out.append({"idx": i, "text": ln["text"], "voice": voice["id"] if voice else None,
                    "engine": voice.get("engine") if voice else None, "licence": voice.get("licence") if voice else None,
                    "commercial_use": voice.get("commercial_use") if voice else None,
                    "attribution": voice.get("attribution") if voice else None,
                    "consent": voice.get("consent_id") if voice else None,
                    "timing_target": {"start": ln["start"], "end": ln["end"], "slot_s": slot},
                    "estimated_s": est, "speed_needed": need, "max_speed": MAX_SPEED, "min_picture_speed": MIN_PICTURE_SPEED,
                    "action": action})
    return {"schema": "nexus.DubbingManifest/1", "project_id": project_id, "lang": lang, "commercial": commercial,
            "voice": voice, "blocked": blocked, "label": "AI voice (synthetic)", "lines": out,
            "needs_shortening": [x["idx"] for x in out if x["action"] == "shorten_line"],
            "estimate_note": "durations are character-rate estimates; the rendered audio decides"}


def render_demo_line(manifest: dict, idx: int, out_wav: str | Path) -> dict:
    """Demo only: synthesise one manifest line with nexus.speech.tts (Piper). Refuses lines without a cleared voice
    when the manifest is commercial. Caller holds jobs.heavy_lock()."""
    ln = manifest["lines"][idx]
    if not ln["voice"] or (manifest["commercial"] and ln["commercial_use"] != "yes"):
        raise VoiceRefused(manifest.get("blocked") or f"line {idx}: voice not cleared")
    if ln["engine"] != "piper":
        raise VoiceRefused(f"engine {ln['engine']} is not integrated here (Account 2 renders)")
    from ...speech import tts
    info = tts.synthesize(ln["text"], Path(out_wav), manifest["lang"].split("-")[0], voice=ln["voice"])
    if info.get("voice") != ln["voice"]:
        raise VoiceRefused(f"tts fell back to {info.get('voice')} instead of {ln['voice']}")
    return {k: info.get(k) for k in ("voice", "engine", "license", "commercial", "duration", "lufs", "cached")}

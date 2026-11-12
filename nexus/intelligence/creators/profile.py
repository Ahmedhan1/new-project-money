"""analyze_reference(): local file (or public metadata) -> CreatorReferenceProfile.

Measured fields come from measure.py. Interpreted fields come from explicit rules on those measurements (each with
`based_on` field paths and a confidence) plus an OPTIONAL router call (task "reference_interpretation", privacy
"private" = local models only). The rules use NEXUS-internal thresholds (`RULES`), not industry norms.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from pathlib import Path

from .. import contracts as C
from . import measure as M
from . import metadata as meta_mod

RIGHTS_OK = {"owned", "licensed", "public-domain", "cc0", "cc-by", "cc-by-sa", "commentary"}
# Internal decision thresholds for the rule layer (tunable; not claims about what works on any platform).
RULES = {"fast_cuts_per_min": 20.0, "slow_cuts_per_min": 6.0, "late_speech_s": 5.0, "early_speech_s": 1.0,
         "long_shot_s": 20.0, "high_silence_share": 0.2, "speech_led_share": 0.5, "spike_factor": 1.5}


class RightsError(ValueError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _i(value, conf: float, based_on: list[str], rule: str) -> dict:
    """One interpreted value with its provenance."""
    return {"value": value, "kind": "interpreted", "confidence": round(conf, 2), "based_on": based_on, "rule": rule}


# ----------------------------------------------------------------------------------------------- rule layer
def hook_type(m: dict) -> dict:
    o5, o15 = m["opening"]["first_5s"], m["opening"]["first_15s"]
    sp = m["speech"]
    if sp["available"] and o15["questions"] + o15["question_words"] > 0 and (o5["speech_onset_s"] is not None):
        return _i("question", 0.5, ["opening.first_15s.questions", "opening.first_15s.question_words"],
                  "question marker in the first 15 s of speech")
    if sp["available"] and o15["stakes_words"] > 0:
        return _i("stakes_promise", 0.45, ["opening.first_15s.stakes_words"], "stakes vocabulary in the first 15 s")
    if sp["available"] and o15["numbers"] > 0 and o5["speech_onset_s"] is not None:
        return _i("specific_number", 0.45, ["opening.first_15s.numbers"], "number spoken in the first 15 s")
    if o5["cuts"] >= 2 and o5["speech_onset_s"] is None:
        return _i("visual_montage", 0.6, ["opening.first_5s.cuts", "opening.first_5s.speech_onset_s"],
                  ">= 2 cuts and no speech in the first 5 s")
    if o5["speech_onset_s"] is not None:
        return _i("direct_statement", 0.4, ["opening.first_5s.speech_onset_s"], "speech starts in the first 5 s without markers")
    return _i("slow_atmospheric", 0.55, ["opening.first_5s.cuts", "opening.first_5s.speech_onset_s"],
              "< 2 cuts and no speech in the first 5 s")


def pacing_shape(m: dict) -> dict:
    curve = [c["cuts_per_min"] for c in m["pacing_curve"]]
    if not any(curve):
        return _i("single_shot_or_undetected_cuts", 0.5, ["pacing_curve"], "no cuts detected")
    first, mid, last = statistics.fmean(curve[:3]), statistics.fmean(curve[3:7]), statistics.fmean(curve[7:])
    if last > 1.3 * first and last >= mid:
        v = "escalating"
    elif first > 1.3 * last and first >= mid:
        v = "front_loaded"
    elif mid > 1.3 * max(first, last):
        v = "peak_in_middle"
    elif mid < 0.7 * min(first, last):
        v = "dip_in_middle"
    else:
        v = "even"
    return _i(v, 0.5, ["pacing_curve"], "mean cut rate of slices 0-30 % vs 30-70 % vs 70-100 % (30 % margin)")


def edit_tempo(m: dict) -> dict:
    c = m["cuts_per_min"]
    v = "fast" if c >= RULES["fast_cuts_per_min"] else "slow" if c <= RULES["slow_cuts_per_min"] else "medium"
    return _i(v, 0.7, ["cuts_per_min"], f"NEXUS bands: >= {RULES['fast_cuts_per_min']} fast, <= {RULES['slow_cuts_per_min']} slow")


def audio_mode(m: dict) -> dict:
    sp, au = m["speech"], m["audio"]
    if not au["has_audio"]:
        return _i("silent", 0.95, ["audio.has_audio"], "no audio stream")
    if not sp["available"]:
        return _i("unknown_no_transcript", 0.3, ["speech.available"], "no transcript")
    share = sp["speech_share"] or 0.0
    other = au["non_speech_audio_share_upper_bound"] or 0.0
    if share >= RULES["speech_led_share"]:
        return _i("speech_led", 0.65, ["speech.speech_share"], f"speech share >= {RULES['speech_led_share']}")
    if other > share:
        return _i("music_or_ambience_led", 0.4, ["audio.non_speech_audio_share_upper_bound", "speech.speech_share"],
                  "non-speech non-silent audio exceeds speech (no music classifier: low confidence)")
    return _i("mixed", 0.4, ["speech.speech_share", "audio.silence_share"], "neither speech nor other audio dominates")


def spikes(m: dict) -> list[dict]:
    curve = m["pacing_curve"]
    mean = statistics.fmean(c["cuts_per_min"] for c in curve) if curve else 0.0
    return [c for c in curve[3:] if mean and c["cuts_per_min"] >= RULES["spike_factor"] * mean]


def interpret(m: dict) -> dict:
    """Rule-based interpretation of a measured block. Every item names the fields it rests on."""
    o3, o5, o15 = (m["opening"][k] for k in ("first_3s", "first_5s", "first_15s"))
    sp, au = m["speech"], m["audio"]
    eng, strengths, weaknesses, techniques = [], [], [], []

    def add(lst, text, based_on, conf=0.5):
        lst.append({"text": text, "kind": "interpreted", "based_on": based_on, "confidence": conf})

    if o15["questions"] + o15["question_words"]:
        add(eng, "Poses a question in the opening (curiosity gap)", ["opening.first_15s.questions", "opening.first_15s.question_words"])
    if o15["stakes_words"]:
        add(eng, "Uses stakes vocabulary in the opening", ["opening.first_15s.stakes_words"], 0.4)
    if o15["numbers"]:
        add(eng, "States specific numbers early", ["opening.first_15s.numbers"], 0.45)
    sk = spikes(m)
    if sk:
        add(eng, f"Cut-rate spikes later in the video ({', '.join(c['slice'] for c in sk)}) - possible re-engagement beats",
            ["pacing_curve"], 0.4)
    if (m.get("loudness_jump_db") or 0) >= 3:
        add(eng, "Opening is louder than the rest (energy jump)", ["loudness_jump_db"], 0.5)

    if o3["cuts"] >= 1:
        add(strengths, "Visual change within the first 3 s", ["opening.first_3s.cuts"], 0.6)
    if sp["available"] and o5["speech_onset_s"] is not None and o5["speech_onset_s"] <= RULES["early_speech_s"]:
        add(strengths, "Speech starts within the first second", ["opening.first_5s.speech_onset_s"], 0.6)
    if sp["available"] and (sp["speech_share"] or 0) >= RULES["speech_led_share"] and \
            (sp["time_to_first_speech_s"] is None or sp["time_to_first_speech_s"] > RULES["late_speech_s"]):
        add(weaknesses, f"Speech-led video whose first words come after {RULES['late_speech_s']} s",
            ["speech.speech_share", "speech.time_to_first_speech_s"], 0.5)
    if (m["longest_shot_s"] or 0) > RULES["long_shot_s"] and m["cuts_per_min"] > RULES["slow_cuts_per_min"]:
        add(weaknesses, f"At least one shot longer than {RULES['long_shot_s']:.0f} s in an otherwise cut-driven edit",
            ["longest_shot_s", "cuts_per_min"], 0.4)
    if au["has_audio"] and (au["silence_share"] or 0) >= RULES["high_silence_share"]:
        add(weaknesses, "Large share of silence", ["audio.silence_share"], 0.5)
    if not au["has_audio"]:
        add(weaknesses, "No audio track (relies on visuals or captions only)", ["audio.has_audio"], 0.9)

    shape = pacing_shape(m)
    tempo = edit_tempo(m)
    add(techniques, f"{tempo['value']} edit tempo ({m['cuts_per_min']} cuts/min, median shot {m['median_shot_s']} s)",
        ["cuts_per_min", "median_shot_s"], 0.7)
    add(techniques, f"pacing shape: {shape['value']}", ["pacing_curve"], 0.5)
    if sp["available"] and sp["wpm_speaking"]:
        add(techniques, f"narration density {sp['wpm_speaking']} words per speaking minute", ["speech.wpm_speaking"], 0.7)
    return {"hook_type": hook_type(m), "pacing_shape": shape, "edit_tempo": tempo, "audio_mode": audio_mode(m),
            "engagement": eng, "strengths": strengths, "weaknesses": weaknesses, "techniques": techniques}


# ----------------------------------------------------------------------------------------------- optional router
LLM_SCHEMA = {"type": "object", "required": ["story_structure", "hook_summary"],
              "properties": {"story_structure": {"type": "string"}, "hook_summary": {"type": "string"},
                             "techniques": {"type": "array"}, "caveats": {"type": "array"}}}


def llm_interpretation(measured: dict, category: str, fmt: str) -> dict:
    """Ask the router (local only) for a short structural reading of the MEASUREMENTS (never media or full text)."""
    from ..model_router import NoProviderAvailable, complete
    slim = {k: v for k, v in measured.items() if k not in ("video",)}
    slim.get("opening", {}).pop("opening_phrase", None)
    prompt = ("Measurements of one reference video (structure only). Category: " + category + ", format: " + fmt +
              ". Describe the likely story structure and hook in one sentence each, list up to 4 editing techniques "
              "visible in the numbers, and list caveats. Do not invent numbers. Return JSON with keys story_structure, "
              "hook_summary, techniques, caveats.\n" + json.dumps(slim)[:6000])
    try:
        from ... import jobs, llm
        with jobs.heavy_lock():
            try:
                r = complete("reference_interpretation", prompt, schema=LLM_SCHEMA, privacy="private", max_tokens=400,
                             temperature=0.2)
            finally:
                try:
                    llm.unload()
                except Exception:  # noqa: BLE001
                    pass
        return {"status": "ok", "kind": "interpreted_by_model", "provider": r.provider, "model": r.model, "data": r.data,
                "confidence": 0.35, "note": "model reading of the measurements; not verified"}
    except NoProviderAvailable as e:
        return {"status": "not_available", "reasons": e.reasons[:6], "note": "rule-based interpretation only"}
    except Exception as e:  # noqa: BLE001 - never fail a profile over the optional model step
        return {"status": "failed", "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ----------------------------------------------------------------------------------------------- assembly
def build_profile(measured: dict | None, *, source: dict, creator: str = "unknown", category: str = "unknown",
                  fmt: str = "unknown", packaging: dict | None = None, llm: dict | None = None,
                  evidence: list | None = None) -> dict:
    """Assemble and validate a CreatorReferenceProfile from a measured block (None = metadata-only reference)."""
    ev = list(evidence or [])
    if measured is None:
        dur = (packaging or {}).get("duration_s", {}).get("value") if isinstance((packaging or {}).get("duration_s"), dict) else None
        return C.make("CreatorReferenceProfile", id=C.new_id("crp"), source=source, creator=creator, category=category,
                      format=fmt, duration_s=dur, hook={"status": "not_measured (metadata only)"},
                      story={"status": "not_measured"}, editing={"status": "not_measured"}, visual={"status": "not_measured"},
                      audio={"status": "not_measured"}, engagement=[], packaging=packaging or {}, strengths=[], weaknesses=[],
                      techniques=[], confidence=0.1, evidence=ev or [C.evidence(source.get("kind", "metadata"), source.get("url"))],
                      measured=None, provenance={"measured": [], "interpreted": [], "metadata": sorted((packaging or {}).keys())})
    it = interpret(measured)
    sp, au = measured["speech"], measured["audio"]
    hook = {"measured": {k: measured["opening"][k] for k in ("first_3s", "first_5s", "first_15s")},
            "loudness_jump_db": measured.get("loudness_jump_db"), "type": it["hook_type"]}
    story = {"pacing_shape": it["pacing_shape"], "pacing_curve": measured["pacing_curve"],
             "structure": (llm or {}).get("data", {}).get("story_structure") if (llm or {}).get("status") == "ok" else None,
             "structure_kind": "interpreted_by_model" if (llm or {}).get("status") == "ok" else "not_available (rules give pacing shape only)",
             "llm": llm or {"status": "not_requested"}}
    editing = {"cuts_per_min": measured["cuts_per_min"], "shots": measured["shots"], "mean_shot_s": measured["mean_shot_s"],
               "median_shot_s": measured["median_shot_s"], "longest_shot_s": measured["longest_shot_s"], "tempo": it["edit_tempo"]}
    visual = {**measured.get("video", {}), "quality": measured.get("quality"),
              "on_screen_text": "not_measured (no OCR model deployed)"}
    audio = {**au, "speech_share": sp["speech_share"], "wpm_speaking": sp["wpm_speaking"], "mode": it["audio_mode"]}
    confs = [it[k]["confidence"] for k in ("hook_type", "pacing_shape", "edit_tempo", "audio_mode")]
    conf = round(statistics.fmean(confs) * (1.0 if sp["available"] or not au["has_audio"] else 0.8), 2)
    ev.append(C.evidence("nexus.intelligence.creators.measure (ffmpeg scene/silence/loudness" +
                         (", faster-whisper word timings)" if sp["available"] else ")"), source.get("url"), kind="measurement"))
    return C.make("CreatorReferenceProfile", id=C.new_id("crp"), source=source, creator=creator, category=category, format=fmt,
                  duration_s=measured["duration_s"], hook=hook, story=story, editing=editing, visual=visual, audio=audio,
                  engagement=it["engagement"], packaging=packaging or {}, strengths=it["strengths"], weaknesses=it["weaknesses"],
                  techniques=it["techniques"], confidence=conf, evidence=ev, measured=measured,
                  provenance={"measured": ["duration_s", "measured", "hook.measured", "editing (numbers)", "story.pacing_curve",
                                           "audio (numbers)", "visual (resolution, quality)"],
                              "interpreted": ["hook.type", "story.pacing_shape", "story.structure", "editing.tempo", "audio.mode",
                                              "engagement", "strengths", "weaknesses", "techniques"],
                              "metadata": sorted((packaging or {}).keys()),
                              "rules": RULES, "not_proof": "interpretations describe structure; they do not show what caused any outcome"})


def _is_url(s: str) -> bool:
    return bool(re.match(r"https?://", s))


def analyze_reference(path_or_url: str | Path, *, rights: str, meta: dict | None = None, licence_url: str | None = None,
                      transcribe: bool = True, transcript: dict | None = None, language: str | None = None,
                      use_router: bool = False, keep_quote: bool = False) -> dict:
    """Profile one reference.

    * Local file: `rights` must be one of RIGHTS_OK (the owner states it). Measured locally; nothing leaves the machine.
    * URL: no download, ever. YouTube URLs get public metadata from the official Data API when NEXUS_KEY_YOUTUBE is set
      (and the host is allowed by the cost guard), otherwise owner-entered `meta` is used.
    `meta` (owner-entered): creator, category, format, title, published, views, likes, captions (a transcript dict the
    owner supplies, used instead of Whisper).
    """
    meta = dict(meta or {})
    rights = (rights or "").lower()
    creator, category, fmt = meta.get("creator", "unknown"), meta.get("category", "unknown"), meta.get("format", "unknown")
    packaging = meta_mod.owner_packaging(meta)
    s = str(path_or_url)
    if _is_url(s):
        source = {"kind": "url", "url": s, "rights": rights or "not_applicable (metadata only)", "downloaded": False}
        ev = []
        if meta_mod.youtube_id(s):
            yt = meta_mod.youtube_metadata(s)
            source["youtube_api"] = {k: v for k, v in yt.items() if k in ("status", "reason")}
            if yt.get("status") == "ok":
                packaging = {**packaging, **yt["packaging"]}
                creator = creator if creator != "unknown" else yt["packaging"].get("channel", {}).get("value", "unknown")
                ev.append(C.evidence("YouTube Data API v3 videos.list", "https://developers.google.com/youtube/v3/docs/videos/list",
                                     kind="platform_api"))
        if not ev:
            ev.append(C.evidence("owner-entered metadata" if packaging else "url only (no metadata available)", s, kind="metadata"))
        return build_profile(None, source=source, creator=creator, category=category, fmt=fmt, packaging=packaging, evidence=ev)
    if rights not in RIGHTS_OK:
        raise RightsError(f"rights '{rights}' not accepted; state one of {sorted(RIGHTS_OK)} for a local reference")
    p = Path(s)
    if not p.exists():
        raise FileNotFoundError(p)
    captions = transcript or meta.get("captions")
    measured = M.measure_file(p, transcribe=transcribe and captions is None, transcript=captions, language=language,
                              keep_quote=keep_quote)
    if captions is not None:
        measured["speech"]["source"] = "owner_supplied_captions"
    elif measured["speech"]["available"]:
        measured["speech"]["source"] = "faster-whisper (local)"
    source = {"kind": "local_file", "path": p.name, "sha256": _sha256(p), "bytes": p.stat().st_size, "rights": rights,
              "licence_url": licence_url or meta.get("licence_url"), "origin_url": meta.get("origin_url")}
    llm = llm_interpretation(measured, category, fmt) if use_router else None
    ev = [C.evidence(f"owner-stated rights: {rights}", licence_url or meta.get("licence_url"), kind="rights")]
    return build_profile(measured, source=source, creator=creator, category=category, fmt=fmt, packaging=packaging, llm=llm,
                         evidence=ev)

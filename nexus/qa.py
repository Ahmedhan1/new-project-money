"""Quality assurance: independent review stages, correction loop, approval report.

Independence principle: generated content is never approved by the component that
produced it. Technical checks are measured with FFmpeg; dubbed audio is verified
by transcribing it back with Whisper (a different model from the TTS and the
LLM); scripts are checked against the research sources by string grounding plus
a separate reviewer prompt. Creative judgement is explicitly left for a human.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

from . import db
from .media import ff
from .timeline.render import clip_duration, clip_starts, total_duration


def _c(name: str, ok: bool, detail: str, severity: str = "critical", fix: str | None = None) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, "severity": "info" if ok else severity, "fix": fix}


# ---------------------------------------------------------------- stage 5: technical
def technical_review(path: str | Path, expect: dict) -> dict:
    """expect: {width,height,fps,duration,audio_channels,target_lufs,subtitles?}"""
    checks = []
    path = Path(path)
    if not path.exists() or path.stat().st_size < 1000:
        return {"stage": "5-technical", "checks": [_c("file exists", False, "missing or empty output")], "passed": False}
    errs = ff.decode_check(path)
    checks.append(_c("file integrity (full decode)", not errs, "decodes cleanly" if not errs else f"{len(errs)} decoder errors: {errs[0][:120]}", fix="rerender"))
    info = ff.probe(path)
    v, a = info["video"], info["audio"]
    checks.append(_c("video track present", v is not None, "ok" if v else "no video stream", fix="rerender"))
    checks.append(_c("audio track present", a is not None, "ok" if a else "no audio stream", fix="rerender"))
    if v:
        checks.append(_c("codec", v["codec"] == expect.get("vcodec", "h264") and v["pix_fmt"] == "yuv420p", f"{v['codec']}/{v['pix_fmt']}", fix="rerender"))
        checks.append(_c("resolution", (v["width"], v["height"]) == (expect["width"], expect["height"]),
                         f"{v['width']}x{v['height']} (expected {expect['width']}x{expect['height']})", fix="rerender"))
        ar_ok = abs(v["width"] / v["height"] - expect["width"] / expect["height"]) < 0.01
        checks.append(_c("aspect ratio", ar_ok, f"{v['width'] / v['height']:.3f}", fix="rerender"))
        checks.append(_c("frame rate", abs(v["fps"] - expect.get("fps", 30)) < 0.05, f"{v['fps']} fps", fix="rerender"))
        black = [b for b in ff.detect_black(path) if b[1] - b[0] > expect.get("max_black", 1.5)]
        checks.append(_c("black frames", not black, "none beyond intentional fades" if not black else f"black sections: {black[:3]}", "major"))
        frozen = [f for f in ff.detect_freeze(path, expect.get("max_freeze", 6.0))]
        checks.append(_c("frozen frames", not frozen, "none" if not frozen else f"frozen sections: {frozen[:3]}", "major"))
    if expect.get("duration"):
        d_ok = abs(info["duration"] - expect["duration"]) <= max(0.25, expect["duration"] * 0.01)
        checks.append(_c("duration matches timeline", d_ok, f"{info['duration']:.2f}s vs timeline {expect['duration']:.2f}s", fix="rerender"))
    if v and a:
        checks.append(_c("A/V stream length", abs(v["duration"] - a["duration"]) < 0.15, f"video {v['duration']:.2f}s, audio {a['duration']:.2f}s", "major"))
    if a:
        checks.append(_c("audio channels", a["channels"] == expect.get("audio_channels", 2), f"{a['channels']} ch @ {a['sample_rate']} Hz"))
        L = ff.loudness(path)
        tgt = expect.get("target_lufs", -14.0)
        silent = L["lufs"] <= -60
        if expect.get("allow_silent") and silent:
            checks.append(_c("loudness", True, "intentionally silent programme"))
        else:
            checks.append(_c("loudness", abs(L["lufs"] - tgt) <= 1.5, f"{L['lufs']} LUFS (target {tgt} +/-1.5)", fix="renormalize"))
            checks.append(_c("clipping / true peak", L["true_peak"] <= -0.9, f"true peak {L['true_peak']} dBTP (limit -1.0)", fix="renormalize"))
            sil = [s for s in ff.detect_silence(path, -50, 4.0)]
            checks.append(_c("dead air", not sil, "none" if not sil else f"silent stretches >4s: {sil[:3]}", "major"))
    cues = expect.get("subtitles")
    if cues is not None:
        bad = [c for c in cues if c["end"] <= c["start"] or c["end"] > info["duration"] + 0.3]
        overlap = [1 for x, y in zip(cues, cues[1:]) if y["start"] < x["end"] - 0.001]
        fast = [c for c in cues if len(c["text"]) / max(c["end"] - c["start"], 0.01) > 25]
        checks.append(_c("subtitle timing", not bad and not overlap, f"{len(cues)} cues, {len(bad)} out of range, {len(overlap)} overlapping", "major", fix="retime-subtitles"))
        checks.append(_c("subtitle reading speed", len(fast) <= max(1, len(cues) // 10), f"{len(fast)} cues above 25 chars/s", "minor"))
    return {"stage": "5-technical", "checks": checks, "passed": all(c["ok"] or c["severity"] == "minor" for c in checks)}


# ---------------------------------------------------------------- stage 2: assets
def asset_review(tl: dict, known: dict | None = None) -> dict:
    """`known` maps media paths to rights records (project media the user imported and vouched for)."""
    checks, seen, done = [], {}, set()
    known = known or {}
    for c in tl["clips"]:
        src = c.get("src")
        if not src or (src in done and c["type"] != "image"):
            continue
        done.add(src)
        p = Path(src)
        if not p.exists():
            checks.append(_c(f"asset {p.name}", False, "missing media"))
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        prov = c.get("provenance") or known.get(src) or (db.query("SELECT * FROM assets WHERE sha256=?", (h,)) or [None])[0]
        if c["type"] == "image":
            from PIL import Image
            try:
                with Image.open(p) as im:
                    im.verify()
                with Image.open(p) as im:
                    w, hh = im.size
                checks.append(_c(f"resolution {p.name}", w >= tl["width"] * 0.6, f"{w}x{hh} for a {tl['width']}x{tl['height']} frame", "minor"))
            except Exception as e:  # corrupted file
                checks.append(_c(f"asset {p.name}", False, f"corrupt image: {e}"))
        if prov is None:
            checks.append(_c(f"provenance {p.name}", False, "no licence record for this asset", "major"))
        else:
            lic = prov.get("license") or prov.get("rights") or "unknown"
            checks.append(_c(f"provenance {p.name}", True, f"{lic} / {prov.get('source', 'user')}"))
        if c["type"] == "image":
            seen.setdefault(h, []).append(c["id"])
    dup = {h: ids for h, ids in seen.items() if len(ids) > 1}
    checks.append(_c("duplicate stills", not dup, "none" if not dup else f"{len(dup)} image(s) reused in several scenes", "minor"))
    return {"stage": "2-assets", "checks": checks, "passed": all(c["ok"] or c["severity"] == "minor" for c in checks)}


# ---------------------------------------------------------------- stage 4: editing
def editing_review(tl: dict) -> dict:
    checks = []
    durs = [clip_duration(c) for c in tl["clips"]]
    total = total_duration(tl)
    short = [round(d, 2) for d in durs if d < 0.5]
    checks.append(_c("cut length", not short, "no flash cuts" if not short else f"clips under 0.5s: {short}", "major"))
    longest = max(durs) if durs else 0
    checks.append(_c("pacing", longest <= 45 or len(durs) == 1, f"{len(durs)} shots, mean {total / max(len(durs), 1):.1f}s, longest {longest:.1f}s", "minor"))
    narration = [a for a in tl.get("audio", []) if a.get("role", "narration") == "narration"]  # sfx/ambience may overlap freely
    for a in tl.get("audio", []):
        if not Path(a["src"]).exists():
            checks.append(_c("audio media", False, f"missing {a['src']}"))
    for a in narration:
        if not Path(a["src"]).exists():
            continue
        end = a.get("start", 0) + ff.duration(a["src"])
        checks.append(_c(f"narration fits ({Path(a['src']).name})", end <= total + 0.3, f"ends {end:.2f}s / programme {total:.2f}s", fix="extend-scene"))
    if narration:
        spans = sorted((a.get("start", 0), a.get("start", 0) + ff.duration(a["src"])) for a in narration if Path(a["src"]).exists())
        clash = [1 for x, y in zip(spans, spans[1:]) if y[0] < x[1] - 0.05]
        checks.append(_c("narration overlap", not clash, "no overlapping narration" if not clash else f"{len(clash)} overlaps"))
    cues = tl.get("subtitles", [])
    if cues:
        out_of = [c for c in cues if c["end"] > total + 0.3]
        checks.append(_c("subtitle sync", not out_of, f"{len(cues)} cues inside programme" if not out_of else f"{len(out_of)} cues past the end", "major", fix="retime-subtitles"))
    return {"stage": "4-editing", "checks": checks, "passed": all(c["ok"] or c["severity"] == "minor" for c in checks)}


# ---------------------------------------------------------------- language checks
def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))  # strips Arabic diacritics too
    text = re.sub(r"[إأآا]", "ا", text).replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def text_similarity(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, _norm(a).split(), _norm(b).split()).ratio(), 3)


def dub_review(audio_path: str | Path, intended_text: str, lang: str, threshold: float = 0.6) -> dict:
    """Independent check of a narration/dub: transcribe it back and compare with the script."""
    from .speech import asr
    t = asr.transcribe(audio_path, language=lang.split("-")[0], words=False)
    sim = text_similarity(intended_text, t["text"])
    return _c(f"dub intelligibility [{lang}]", sim >= threshold,
              f"Whisper heard {sim:.0%} of the script words back" + ("" if sim >= threshold else f": \"{t['text'][:90]}\""), "major",
              fix="resynthesize") | {"similarity": sim, "heard": t["text"]}


def script_checks(text: str, lang: str) -> list[dict]:
    """Deterministic language sanity: right script, no leftover markup, no untranslated remainder."""
    checks = []
    letters = [ch for ch in text if ch.isalpha()]
    arabic = sum("؀" <= ch <= "ۿ" for ch in letters) / max(len(letters), 1)
    if lang.startswith("ar"):
        checks.append(_c(f"script [{lang}]", arabic > 0.85, f"{arabic:.0%} Arabic letters", "major", fix="retranslate"))
    else:
        checks.append(_c(f"script [{lang}]", arabic < 0.05, f"{1 - arabic:.0%} Latin letters", "major", fix="retranslate"))
    junk = re.search(r"<think>|```|\{\"|^\s*(Translation|Here is)", text, re.I | re.M)
    checks.append(_c(f"clean text [{lang}]", not junk, "no model artefacts" if not junk else f"artefact: {junk.group(0)[:20]}", "major", fix="retranslate"))
    return checks


# ---------------------------------------------------------------- stage 7: approval
def approve(project_dir: Path, name: str, stages: list[dict], extra: dict | None = None) -> dict:
    critical = [c for s in stages for c in s["checks"] if not c["ok"] and c["severity"] == "critical"]
    major = [c for s in stages for c in s["checks"] if not c["ok"] and c["severity"] == "major"]
    minor = [c for s in stages for c in s["checks"] if not c["ok"] and c["severity"] == "minor"]
    status = "BLOCKED" if critical else "NEEDS_FIXES" if major else "PASSED_AUTOMATED_CHECKS"
    report = {"output": name, "generated": time.strftime("%Y-%m-%d %H:%M:%S"), "status": status,
              "human_approval": "PENDING - automated checks never replace a human look at the final video",
              "creative_review": "NOT AUTOMATED - storytelling, visual appeal and brand fit need human review",
              "known_issues": [f"[{c['severity']}] {c['check']}: {c['detail']}" for c in critical + major + minor],
              "stages": stages, **(extra or {})}
    (project_dir / "reports").mkdir(parents=True, exist_ok=True)
    (project_dir / "reports" / f"{name}.qa.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    db.metric("qa_pass", 1.0 if status == "PASSED_AUTOMATED_CHECKS" else 0.0, output=name)
    return report


def expectation(tl: dict) -> dict:
    return {"width": tl["width"], "height": tl["height"], "fps": tl.get("fps", 30), "duration": total_duration(tl),
            "target_lufs": tl.get("target_lufs", -14.0), "subtitles": tl.get("subtitles") or None}


def render_with_qa(project, tl: dict, name: str, analyses: dict | None = None, max_rounds: int = 3,
                   extra_stages: list[dict] | None = None, extra: dict | None = None, draft: bool = False) -> tuple[dict, dict]:
    """Render, review, apply targeted corrections, re-check. Returns (render stats, approval report)."""
    from . import edit
    from .timeline import render, subs
    out = project.dir / "renders" / f"{name}.mp4"
    corrections = []
    stats: dict = {}
    for rnd in range(max_rounds):
        stats = render.render(tl, out, project.dir / "work", draft=draft)
        known = {m["path"]: {"license": m.get("rights", "user-supplied"), "source": "user upload"} for m in project.data.get("media", [])}
        known.update(project.data.get("generated", {}))
        stages = [asset_review(tl, known), editing_review(tl), technical_review(out, expectation(tl))]
        fixes = {c["fix"] for s in stages for c in s["checks"] if not c["ok"] and c.get("fix") and c["severity"] != "minor"}
        if not fixes or rnd == max_rounds - 1:
            break
        before = [f"{c['check']}: {c['detail']}" for s in stages for c in s["checks"] if not c["ok"]]
        applied = []
        if "retime-subtitles" in fixes:
            if analyses:
                edit.retime_subtitles(tl, analyses)
            total = total_duration(tl)
            tl["subtitles"] = subs.fix_timing([{**c, "end": min(c["end"], total)} for c in tl["subtitles"] if c["start"] < total - 0.2])
            applied.append("re-timed subtitles to the programme")
        if "renormalize" in fixes:
            tl["target_lufs"] = tl.get("target_lufs", -14.0) - 0.7  # back off and re-run the two-pass normaliser
            applied.append("re-ran loudness normalisation with extra headroom")
        if "extend-scene" in fixes:
            starts = clip_starts(tl)
            for a in tl.get("audio", []):
                end = a.get("start", 0) + ff.duration(a["src"])
                idx = max(i for i, s in enumerate(starts) if s <= a.get("start", 0) + 1e-3)
                clip_end = starts[idx] + clip_duration(tl["clips"][idx])
                if end > clip_end and tl["clips"][idx]["type"] != "video":
                    tl["clips"][idx]["duration"] = round(tl["clips"][idx]["duration"] + end - clip_end + 0.3, 3)
            applied.append("extended scenes to fit narration")
        corrections.append({"round": rnd + 1, "problems": before, "applied": applied})
        if not applied:
            break
    stages = (extra_stages or []) + stages
    report = approve(project.dir, name, stages, {"corrections": corrections, "render": stats, **(extra or {})})
    return stats, report

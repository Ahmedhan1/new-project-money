"""Human review of the automatic decisions that a person checks fastest in a list (REV-1).

Speakers  - rename role / give a display name / merge two voices / reassign a turn; edits are recorded and the automatic
            result is kept (`diarize._editable`). A named speaker gets a lower third on the first clip they speak in.
Lines     - approve or correct each translated caption line of a localised branch; a branch whose lines are all approved
            counts as natively reviewed in the subtitle package.
Cutaways  - reject a B-roll insert; the clip shows the speaker again and the rejection is remembered.
Every timeline change is a new version (undo with the existing `undo` op).
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from .media import diarize as dz
from .timeline import subs
from .timeline.render import clip_duration


# ---------------------------------------------------------------- speakers
def _analysis_files(project) -> list[tuple[dict, Path]]:
    out = []
    for m in project.data["media"]:
        f = project.dir / "reports" / f"analysis_{m['id']}.json"
        if f.exists():
            out.append((m, f))
    return out


def speakers(project) -> list[dict]:
    rows = []
    for m, f in _analysis_files(project):
        spk = (json.loads(f.read_text(encoding="utf-8")).get("speakers")) or {}
        if not spk.get("available"):
            rows.append({"media": m["id"], "available": False, "why": spk.get("why") or spk.get("note")})
            continue
        for label, v in (spk.get("speakers") or {}).items():
            rows.append({"media": m["id"], "speaker": label, "role": v.get("role"), "name": v.get("name"),
                         "talk_share": v.get("talk_share"), "role_reason": v.get("role_reason"),
                         "turns": sum(1 for t in spk.get("turns", []) if t["speaker"] == label)})
    return rows


def edit_speaker(project, media_id: str, op: str, speaker: str, value: str | None = None, who: str = "reviewer") -> dict:
    """op: rename (value = interviewer|guest|narrator) | name (value = display name) | merge (value = label to merge
    INTO `speaker`) | reassign (value = turn index; `speaker` = new label)."""
    m, f = next(((m, f) for m, f in _analysis_files(project) if m["id"] == media_id), (None, None))
    if f is None:
        raise KeyError(f"no analysis for media {media_id}")
    an = json.loads(f.read_text(encoding="utf-8"))
    spk = an.get("speakers") or {}
    if not spk.get("available"):
        raise ValueError("this video has no speaker detection to edit")
    if op == "rename":
        if value not in ("interviewer", "guest", "narrator"):
            raise ValueError("role must be interviewer, guest or narrator")
        new = dz.rename(spk, speaker, value, who)
    elif op == "name":
        if not (value or "").strip():
            raise ValueError("a name is required")
        new = dz.set_name(spk, speaker, value, who)
    elif op == "merge":
        new = dz.merge(spk, speaker, value, who)
    elif op == "reassign":
        idx = int(value)
        new = dz.reassign_turn(spk, idx, speaker, who)
        t = new["turns"][idx]  # the segments inside the turn carry the label that captions/shorts/director read
        for i, sg in enumerate(an["transcript"]["segments"]):
            if t["start"] - 0.05 <= (sg["start"] + sg["end"]) / 2 <= t["end"] + 0.05 and i < len(new["segment_labels"]):
                new["segment_labels"][i] = speaker
    else:
        raise ValueError(f"unknown speaker op '{op}'")
    if speaker not in new.get("speakers", {}) and op != "reassign":
        raise KeyError(f"unknown speaker {speaker}")
    for s in an["transcript"]["segments"]:
        s.pop("speaker", None)
    an = dz.apply_labels(an, new)
    f.write_text(json.dumps(an, ensure_ascii=False), encoding="utf-8")
    return {"media": media_id, "op": op, "speaker": speaker, "value": value, "edits": len(new.get("edits", []))}


def apply_names(tl: dict, analyses: dict) -> tuple[dict, list[str]]:
    """Lower third with the person's name on the first clip in which each named speaker does most of the talking."""
    from .director import speaker_names
    out = copy.deepcopy(tl)
    names = speaker_names(list((analyses or {}).values()))
    if not names:
        return out, []
    seen, added = set(), []
    for c in out["clips"]:
        an = (analyses or {}).get(c.get("src"))
        if c.get("type") != "video" or not an:
            continue
        talk: dict = {}
        for s in an["transcript"]["segments"]:
            ov = min(c["out"], s["end"]) - max(c["in"], s["start"])
            if ov > 0 and s.get("speaker"):
                talk[s["speaker"]] = talk.get(s["speaker"], 0.0) + ov
        if not talk:
            continue
        sp = max(talk, key=talk.get)
        if sp in names and sp not in seen and talk[sp] >= 0.5 * (c["out"] - c["in"]):
            seen.add(sp)
            c["text"] = [t for t in c.get("text", []) if t.get("style") != "lower_third"] + [
                {"text": names[sp], "style": "lower_third", "start": 0.3, "end": round(min(3.5, clip_duration(c)), 3)}]
            added.append(names[sp])
    return out, added


# ---------------------------------------------------------------- translated lines
def lines(tl: dict) -> dict:
    rv = (tl.get("review") or {}).get("lines") or {}
    rows = [{"index": i, "start": q["start"], "end": q["end"], "text": q["text"], **(rv.get(str(i)) or {})}
            for i, q in enumerate(tl.get("subtitles") or [])]
    approved = sum(1 for r in rows if r.get("approved"))
    return {"lang": tl.get("lang"), "lines": rows, "approved": approved, "total": len(rows),
            "native_review_done": bool(rows) and approved == len(rows)}


def edit_line(tl: dict, index: int, text: str | None = None, approved: bool | None = None, who: str = "reviewer") -> dict:
    out = copy.deepcopy(tl)
    cues = out.get("subtitles") or []
    if not 0 <= index < len(cues):
        raise IndexError(f"line {index} does not exist (0..{len(cues) - 1})")
    rv = out.setdefault("review", {}).setdefault("lines", {})
    rec = rv.setdefault(str(index), {})
    if text is not None and text.strip() and text.strip() != cues[index]["text"]:
        rec.setdefault("machine_text", cues[index]["text"])
        cues[index] = {**cues[index], "text": text.strip()}
        cues[index].pop("words", None)  # word timings belonged to the machine text
        rec["edited"] = True
    if approved is not None:
        rec["approved"] = bool(approved)
    rec["by"], rec["at"] = who, time.strftime("%Y-%m-%dT%H:%M:%S")
    return out


def write_sidecars(project, tl: dict, render_name: str) -> list[str]:
    """Rewrite the SRT/VTT next to an existing render so the subtitle package carries the reviewed text."""
    out = []
    base = project.dir / "renders" / render_name
    for ext, fn in (("srt", subs.write_srt), ("vtt", subs.write_vtt)):
        p = base.with_suffix(f".{ext}")
        if p.exists():
            fn(tl.get("subtitles") or [], p)
            out.append(p.name)
    return out


# ---------------------------------------------------------------- cutaways
def cutaways(tl: dict) -> list[dict]:
    rows = []
    for c in tl["clips"]:
        p = c.get("pip") or {}
        if p.get("full") and p.get("provenance"):
            pv = p["provenance"]
            rows.append({"clip": c["id"], "title": pv.get("title"), "source": pv.get("source"), "license": pv.get("license"),
                         "page": pv.get("page"), "matched": pv.get("matched"), "kind": pv.get("kind"),
                         "start": p.get("start"), "end": p.get("end")})
    return rows


def reject_cutaway(tl: dict, clip_id: str, why: str = "", who: str = "reviewer") -> dict:
    out = copy.deepcopy(tl)
    c = next((c for c in out["clips"] if c["id"] == clip_id), None)
    if c is None or not (c.get("pip") or {}).get("full"):
        raise KeyError(f"clip {clip_id} has no cutaway")
    pv = c.pop("pip").get("provenance") or {}
    out.setdefault("broll_rejected", []).append({"clip": clip_id, "id": pv.get("id"), "title": pv.get("title"),
                                                 "why": why, "by": who, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    used = {(cc.get("pip") or {}).get("provenance", {}).get("source") for cc in out["clips"] if cc.get("pip")}
    if pv.get("source") and pv["source"] not in used:  # no other cutaway from that library: drop its credit line
        out["credits"] = [x for x in out.get("credits") or [] if pv["source"].split()[0] not in x]
    return out


# ---------------------------------------------------------------- shorts titles (K-23: verbatim titles are often fragments)
def shorts_titles(project) -> list[dict]:
    rows = []
    for b in sorted(project.data["branches"]):
        if not b.startswith("short-"):
            continue
        tl = project.timeline(b)
        c0 = tl["clips"][0] if tl.get("clips") else {}
        t = next((x for x in c0.get("text", []) if x.get("style") not in ("lower_third",)), None)
        sm = tl.get("source_moment") or {}
        rows.append({"branch": b, "title": t["text"] if t else "", "start": sm.get("start"), "end": sm.get("end"),
                     "edited": bool((tl.get("review") or {}).get("title"))})
    return rows


def set_short_title(tl: dict, title: str, who: str = "reviewer") -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("a title is required")
    out = copy.deepcopy(tl)
    if not out.get("clips"):
        raise ValueError("this branch has no clips")
    c0 = out["clips"][0]
    texts = c0.get("text", [])
    k = next((i for i, x in enumerate(texts) if x.get("style") != "lower_third"), None)
    old = texts[k]["text"] if k is not None else ""
    if k is None:
        texts.append({"text": title, "style": "callout", "start": 0.0, "end": round(min(2.5, clip_duration(c0)), 3)})
    else:
        texts[k] = {**texts[k], "text": title}
    c0["text"] = texts
    out.setdefault("review", {})["title"] = {"machine_text": old, "by": who, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return out

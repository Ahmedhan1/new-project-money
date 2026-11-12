"""DIA-1 + face tracking: role assignment (incl. several interviewers), manual corrections that keep the automatic result,
clustering, filterbank features, face-path smoothing/spans/focus lookup and shorts reframing from face data. No models."""
import numpy as np

from nexus import shorts
from nexus.media import diarize as dz
from nexus.media import faces


def _segs():
    rows = [("Hi, can you hear me okay?", 0, 3, "A"), ("Yes, I can hear you loud and clear.", 3, 6, "G"),
            ("What can you tell us about your training in the caves?", 6, 10, "A"),
            ("We lived underground for six days and practised rescue techniques.", 10, 30, "G"),
            ("We also mapped two new passages with laser scanners.", 30, 45, "G"),
            ("How do you feel about the Artemis selection?", 45, 49, "B"),
            ("It is a long process and we keep training every day for it.", 49, 70, "G")]
    segs = [{"text": t, "start": a, "end": b} for t, a, b, _ in rows]
    return segs, {i: r[3] for i, r in enumerate(rows)}


def test_roles_find_the_guest_and_several_interviewers():
    segs, lab = _segs()
    r = dz.roles(segs, lab)
    assert r["G"]["role"] == "guest" and r["A"]["role"] == "interviewer" and r["B"]["role"] == "interviewer"
    assert all(v["role_reason"] for v in r.values())


def test_single_voice_is_a_narrator_and_unclear_evidence_stays_unknown():
    segs, _ = _segs()
    assert dz.roles(segs, {i: "S1" for i in range(len(segs))})["S1"]["role"] == "narrator"
    flat = [{"text": "We went into the cave.", "start": i * 5, "end": i * 5 + 5} for i in range(4)]
    r = dz.roles(flat, {0: "S1", 1: "S2", 2: "S1", 3: "S2"})
    assert {v["role"] for v in r.values()} == {"unknown"}


def test_manual_corrections_keep_the_automatic_result():
    segs, lab = _segs()
    spk = {"available": True, "turns": [{"start": 0, "end": 3, "speaker": "A", "confidence": 0.4}],
           "speakers": dz.roles(segs, lab), "segment_labels": [lab[i] for i in range(len(segs))]}
    s2 = dz.reassign_turn(dz.merge(dz.rename(spk, "B", "moderator"), "A", "B"), 0, "G")
    assert "B" not in s2["speakers"] and "B" not in s2["segment_labels"] and s2["turns"][0]["speaker"] == "G"
    assert s2["auto"]["segment_labels"] == spk["segment_labels"] and len(s2["edits"]) == 3


def test_clustering_recovers_two_voices_from_embeddings():
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=32), rng.normal(size=32)
    E = np.vstack([a + 0.15 * rng.normal(size=(12, 32)), b + 0.15 * rng.normal(size=(9, 32))])
    lab, sil, k = dz._cluster(E, 4)
    assert k == 2 and sil > 0.3 and len(set(lab[:12])) == 1 and len(set(lab[12:])) == 1 and lab[0] != lab[-1]
    lab1, _, k1 = dz._cluster(a + 0.15 * rng.normal(size=(15, 32)), 4)
    assert k1 == 1  # one voice is not split


def test_fbank_shape_and_normalisation():
    x = np.sin(2 * np.pi * 220 * np.arange(16000) / 16000).astype(np.float32) * 0.3
    f = dz.fbank80(x)
    assert f.shape == (98, 80) and abs(float(f.mean())) < 1e-3


def test_face_path_smoothing_spans_and_focus_lookup():
    samples = [{"t": float(t), "x": 0.3 if t < 10 else 0.7, "y": 0.35} for t in range(20)]
    samples[4]["x"] = 0.9  # a single detector glitch
    sm = faces.smooth_path(samples)
    assert abs(sm[4]["x"] - 0.3) < 1e-6
    spans = faces.focus_spans(sm, end=20.0)
    assert len(spans) == 2 and abs(spans[0]["x"] - 0.3) < 0.02 and abs(spans[1]["x"] - 0.7) < 0.02
    an = {"faces": {"available": True, "focus": spans}}
    assert abs(faces.focus_at(an, 0, 5)[0] - 0.3) < 0.02 and abs(faces.focus_at(an, 12, 18)[0] - 0.7) < 0.02
    assert faces.focus_at({"faces": {"available": False}}, 0, 5) is None


def test_shorts_reframing_uses_face_focus():
    an = {"probe": {"video": {"width": 1280, "height": 720}}, "faces": {"available": True, "focus": [
        {"start": 0, "end": 100, "x": 0.72, "y": 0.33}]}}
    mode, focus, note = shorts.reframe_for(an, 10, 20, 1080, 1920)
    assert mode == "cover" and focus[0] > 0.6 and "subject" in note

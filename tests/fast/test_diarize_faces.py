"""DIA-1 + face tracking: role assignment (incl. several interviewers), manual corrections that keep the automatic result,
clustering, filterbank features, face-path smoothing/spans/focus lookup and shorts reframing from face data. No models."""
import pytest
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


def _two_voice_fixture(tmp_path):
    """Whisper merged interviewer question + guest answer: the voice change inside the segment is found from audio."""
    import subprocess as sp
    from nexus import config
    from nexus.media import diarize as dz
    try:
        from nexus.speech import tts
        if dz.ensure_emb_model() is None:
            raise FileNotFoundError("speaker model")
        a_voice, b_voice = (config.MODELS / "piper" / f"{v}.onnx" for v in ("en_US-joe-medium", "fr_FR-siwis-medium"))
        if not (a_voice.exists() and b_voice.exists()):
            raise FileNotFoundError("voices")
        parts = []
        lines = [("joe", "en_US-joe-medium", "en", "So tell me, what did you find down in the cave that day?"),
                 ("siwis", "fr_FR-siwis-medium", "fr", "Nous avons trouvé une zone de sable plate, ce qui est très rare dans "
                                                        "les grottes, et cela nous a beaucoup surpris pendant la mission.")]
        for k in range(3):
            for who, voice, lang, text in lines:
                w = tmp_path / f"{k}_{who}.wav"
                tts.synthesize(text, w, lang, voice=voice, master=False, cache=False)
                parts.append((who, text, w))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no local voices/speaker model for a two-voice fixture ({type(e).__name__})")
    lst = tmp_path / "l.txt"
    lst.write_text("".join(f"file '{w}'\n" for _, _, w in parts))
    wav = tmp_path / "talk.wav"
    sp.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-ar", "16000", "-ac", "1", str(wav)], check=True)
    segs, t = [], 0.0
    for who, text, w in parts:
        d = float(sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(w)],
                         capture_output=True, text=True).stdout)
        ws = text.split()
        step = d / len(ws)
        words = [{"start": round(t + i * step, 3), "end": round(t + (i + 0.8) * step, 3), "word": x} for i, x in enumerate(ws)]
        segs.append({"start": words[0]["start"], "end": words[-1]["end"], "text": text, "words": words, "who": who})
        t += d
    return wav, segs


def test_a_question_and_answer_merged_in_one_segment_are_split_by_voice(tmp_path):
    """Whisper merged interviewer question + guest answer: the voice change inside the segment is found from audio."""
    from nexus.media import diarize as dz
    wav, segs = _two_voice_fixture(tmp_path)
    q, a = segs[0], segs[1]
    merged = {"start": q["start"], "end": a["end"], "text": q["text"] + " " + a["text"], "words": q["words"] + a["words"]}
    segs = [merged] + segs[2:]
    spk = dz.diarize(wav, {"transcript": {"segments": segs}})
    assert spk["available"] and spk["n_speakers"] == 2 and spk["segments_split"] >= 1
    new = spk["segments"]
    first, second = new[0], new[1]
    assert first["text"].endswith("that day?") and second["text"].startswith("Nous")       # split at the question mark
    assert spk["segment_labels"][0] != spk["segment_labels"][1]
    an = dz.apply_labels({"transcript": {"segments": segs}}, spk)
    assert len(an["transcript"]["segments"]) == len(new) and "segments" not in an["speakers"]


def test_question_answer_question_in_one_segment_split_at_sentence_ends(tmp_path):
    """Each sentence takes the majority voice of the windows inside it (session 6: a change-point estimate had put an
    interviewer question on the guest)."""
    from nexus.media import diarize as dz
    wav, segs = _two_voice_fixture(tmp_path)
    q1, a1, q2 = segs[2], segs[3], segs[4]
    merged = {"start": q1["start"], "end": q2["end"], "text": " ".join(x["text"] for x in (q1, a1, q2)),
              "words": q1["words"] + a1["words"] + q2["words"]}
    segs = segs[:2] + [merged] + segs[5:]
    spk = dz.diarize(wav, {"transcript": {"segments": segs}})
    new = spk["segments"]
    k = next(i for i, x in enumerate(new) if x.get("split_from"))
    texts, labs = [x["text"] for x in new[k:k + 3]], spk["segment_labels"][k:k + 3]
    assert texts[0].endswith("that day?") and texts[1].startswith("Nous") and texts[2].endswith("that day?"), texts
    assert labs[0] == labs[2] != labs[1]

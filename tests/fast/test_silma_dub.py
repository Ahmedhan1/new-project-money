"""SILMA voice: used for ar/en dubbing when available (cloned from the speaker), otherwise the Piper path is unchanged."""
import wave

from nexus.speech import silma, tts


def _wav(path, seconds=1.0, rate=24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


def test_pick_reference_prefers_a_wordy_line_after_the_intro():
    an = {"transcript": {"segments": [
        {"start": 1.0, "end": 6.0, "text": "welcome everybody to our show today friends"},
        {"start": 30.0, "end": 36.0, "text": "we trained in caves for weeks to learn how to work as a team"},
        {"start": 50.0, "end": 52.0, "text": "short"}]}}
    ref = silma.pick_reference(an)
    assert ref["start"] == 30.0 and "caves" in ref["text"]
    assert silma.pick_reference({"transcript": {"segments": []}}) is None


def test_tts_uses_silma_with_a_reference_and_falls_back_without(home, tmp_path, monkeypatch):
    ref = tmp_path / "ref.wav"
    _wav(ref, 5)
    calls = []

    def fake_synth(text, out, ref_wav, ref_text, speed=1.0, seed=1):
        calls.append((text, ref_text, round(speed, 2)))
        _wav(out, 2.0)
        return 2.0

    monkeypatch.setattr(silma, "available", lambda: True)
    monkeypatch.setattr(silma, "synthesize", fake_synth)
    info = tts.synthesize("مرحبا بكم", tmp_path / "a.wav", "ar", reference={"wav": str(ref), "text": "hello there"},
                          length_scale=0.8, master=False, cache=False)
    assert info["engine"] == "silma" and info["commercial"] is True and calls == [("مرحبا بكم", "hello there", 1.25)]
    monkeypatch.setattr(silma, "available", lambda: False)
    monkeypatch.setattr(tts.costguard, "run_with_fallback", lambda *a, **k: ("piper", {"voice": "p", "license": "x", "commercial": True}))
    _wav(tmp_path / "b.wav", 1.0)
    info2 = tts.synthesize("hi", tmp_path / "b.wav", "en", reference={"wav": str(ref), "text": "x"}, master=False, cache=False)
    assert info2["engine"] == "piper" and len(calls) == 1

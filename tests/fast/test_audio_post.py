"""AUD-2: clean_speech chain (RNNoise + gate before the leveller) vs the previous voice_level chain on a synthetic
speech-like signal with steady room noise. The RNNoise part runs only when the model file is installed."""
import subprocess

import numpy as np
import pytest

from nexus.audio import post


def _levels(wav):
    x = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-f", "f32le", "-ac", "1", "-ar", "48000", "-"],
                                     capture_output=True, check=True).stdout, dtype=np.float32)
    b = 2400
    n = len(x) // b
    db = 20 * np.log10(np.sqrt((x[: n * b].reshape(n, b) ** 2).mean(1) + 1e-12))
    return float(np.percentile(db, 10)), float(np.percentile(db, 90))


@pytest.fixture(scope="module")
def noisy(tmp_path_factory):
    """Real synthetic SPEECH (Piper, CC0 voice) with pauses, over steady pink room noise at about -45 dBFS.
    RNNoise is trained on speech: a tone fixture would (correctly) be removed as noise."""
    from nexus import models
    from nexus.speech import tts
    try:
        models.ensure_voice  # noqa: B018
        voice = models.config.MODELS / "piper" / "en_US-joe-medium.onnx"
        if not voice.exists():
            raise FileNotFoundError(voice)
        d = tmp_path_factory.mktemp("ap")
        sp = d / "speech.wav"
        tts.synthesize("The crew trained in a cave for six days. It was cold and very dark. We used our helmet lamps.", sp, "en",
                       voice="en_US-joe-medium", master=False, cache=False)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no local TTS voice to make a speech fixture ({type(e).__name__})")
    f = d / "noisy.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(sp), "-f", "lavfi", "-i", "anoisesrc=d=30:c=pink:a=0.006:r=48000",
                    "-filter_complex", "[0:a]aresample=48000,adelay=500|500,apad=pad_dur=1[s];[s][1:a]amix=inputs=2:normalize=0:duration=first",
                    "-ac", "1", str(f)], check=True)
    return f


def test_falls_back_to_voice_level_without_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(post, "model_path", lambda: tmp_path / "missing.rnnn")
    assert post.segment_chain("clean_speech") == post.VOICE_LEVEL
    assert post.chain_name_in_use() == "voice_level (fallback)"


def test_clean_speech_lowers_the_noise_floor_without_lowering_speech(noisy, tmp_path):
    if not post.model_path().exists():
        pytest.skip("RNNoise model not installed on this machine (downloaded on first use); fallback path tested above")
    out = {}
    for name in ("voice_level", "clean_speech"):
        o = tmp_path / f"{name}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(noisy), "-af", post.segment_chain(name), str(o)], check=True)
        out[name] = _levels(o)
    (f_old, s_old), (f_new, s_new) = out["voice_level"], out["clean_speech"]
    assert f_new < f_old - 3          # quieter pauses
    assert abs(s_new - s_old) < 3     # speech level kept

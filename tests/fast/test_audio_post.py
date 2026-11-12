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


def test_leveller_gives_a_spiky_voice_nearly_the_same_gain_as_a_smooth_one(tmp_path):
    """Acceptance edit: the peak-based leveller left the interviewer (spiky line) 7.5 dB below the guest on equal input
    (the RMS-targeted setting: 0 dB on that real span). Synthetic: same RMS, crest factor +17 dB."""
    import subprocess
    import numpy as np
    from nexus.audio import post
    src = tmp_path / "two.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc='if(lt(t,4),0.05*sin(2*PI*220*t),0.05*sqrt(50)*lt(mod(t*110,1),0.02)*sin(2*PI*900*t))':s=16000:d=8",
                    str(src)], check=True)
    def gap(af):
        x = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-af", af, "-f", "f32le", "-ac", "1", "-"],
                                         capture_output=True).stdout, dtype=np.float32)
        return 10 * np.log10((x[16000:56000] ** 2).mean()) - 10 * np.log10((x[72000:120000] ** 2).mean())
    assert abs(gap("anull")) < 0.5
    old = gap("dynaudnorm=f=250:g=15:p=0.9:m=8:s=6")
    new = gap(post.LEVEL.split(",")[0])
    assert new < old - 1.2 and new < 4.0, (old, new)
    assert post.chain_signature() != post.chain_signature("voice_level")


def test_speaker_gains_match_the_main_speaker_and_render_inside_the_clip(home, tmp_path):
    import subprocess
    import numpy as np
    from nexus.audio import post
    from nexus.timeline import ops, render
    assert post.speaker_gains({"G": {"db": -20, "seconds": 300}, "I": {"db": -29, "seconds": 40},
                               "X": {"db": -21.5, "seconds": 10}}) == {"I": 9.0}          # < 3 dB differences are left
    assert post.speaker_gains({"I": {"db": -40, "seconds": 5}, "G": {"db": -20, "seconds": 50}})["I"] == post.SPK_MAX_DB
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=25:d=4", "-f", "lavfi",
                    "-i", "sine=f=440:d=4", "-filter_complex", "[1:a]volume=0.1[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True)
    c = ops.video_clip(str(src), 1.0, 4.0)
    c["gain_spans"] = [{"in": 2.0, "out": 3.0, "db": 9.0}]                                   # source time
    tl = {"version": 1, "width": 160, "height": 90, "fps": 25, "lang": "en", "grade": "none", "clips": [c], "audio": [],
          "music": None, "subtitles": [], "markers": [], "target_lufs": -14.0}
    out = tmp_path / "o.mp4"
    render.render(tl, out, tmp_path / "w", workers=1)
    x = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                                     capture_output=True).stdout, dtype=np.float32)
    db = lambda a, b: 10 * np.log10((x[int(a * 16000):int(b * 16000)] ** 2).mean())  # noqa: E731
    assert db(1.2, 1.8) - db(0.2, 0.8) > 7.0          # programme 1-2 s = source 2-3 s is boosted by ~9 dB
    assert abs(db(2.2, 2.8) - db(0.2, 0.8)) < 1.5

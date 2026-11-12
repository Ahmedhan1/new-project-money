"""Account 2 / agent A: A/V sync does not drift across many concatenated clips. Each source clip starts with a white
flash frame and a tone burst at the same instant; in the rendered programme every burst must start within one video
frame of its flash, for the 1st and the 20th clip alike (AAC priming/padding must not accumulate)."""
import subprocess

import numpy as np
import pytest

from nexus.timeline import ops, render

W, H, FPS, SR = 64, 36, 30, 48000


def _src(path):
    # 1.0 s: frame 0 white, rest black; 1 kHz burst for the first 60 ms, then silence
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=black:s={W}x{H}:r={FPS}:d=1.0,geq=lum='if(eq(N,0),235,16)':cb=128:cr=128",
                    "-f", "lavfi", "-i", f"sine=f=1000:r={SR}:d=1.0,volume='if(lt(t,0.06),1,0)':eval=frame",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True)
    return str(path)


def _onsets(path, n):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    v = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W).mean(axis=(1, 2))
    flashes = [i / FPS for i in range(len(v)) if v[i] > 128 and (i == 0 or v[i - 1] < 128)]
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = np.abs(np.frombuffer(pcm, dtype=np.int16).astype(float))
    env = np.convolve(a, np.ones(96) / 96, mode="same")
    thr = env.max() * 0.3
    on = np.where((env[1:] > thr) & (env[:-1] <= thr))[0] / SR
    bursts = [float(t) for k, t in enumerate(on) if k == 0 or t - on[k - 1] > 0.3]
    assert len(flashes) == n and len(bursts) == n, (len(flashes), len(bursts))
    return np.array(bursts) - np.array(flashes)


@pytest.mark.parametrize("pipeline", ["pieces", "legacy"])
def test_no_av_drift_over_twenty_clips(home, tmp_path, pipeline):
    src = _src(tmp_path / "s.mp4")
    clips = [ops.video_clip(src, 0, 1.0) for _ in range(20)]
    tl = {"version": 1, "width": W, "height": H, "fps": FPS, "lang": "en", "grade": "none", "audio_fx": "none",
          "target_lufs": -16.0, "clips": clips, "audio": [], "music": None, "subtitles": [], "burn_subtitles": False,
          "markers": [], "render_pipeline": pipeline}
    render.render(tl, tmp_path / "o.mp4", tmp_path / "w", workers=2)
    off = _onsets(tmp_path / "o.mp4", 20)
    assert np.abs(off).max() < 1.0 / FPS, [round(x * 1000, 1) for x in off]
    assert abs(off[-1] - off[0]) < 0.003, "offset grows along the programme"  # measured: 0.6 ms over 20 clips

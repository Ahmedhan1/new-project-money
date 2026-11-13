"""Filter-graph pieces for the renderer's programme mix (Account 2, Agent C). The renderer (nexus/timeline/render.py,
`_mix_graph`) is owned elsewhere: these builders return FFmpeg filter text it can adopt without other changes.
Integration request: docs in the Agent C report and in the docstrings below.

    music_line(idx, music, dur, speech_segments)   music bed line; with `duck: "keyframes"` the duck is volume
                                                   automation computed from speech density (design.ducking) instead of
                                                   the sidechain compressor, so it never pumps between words
    item_line(i, item)                             one extra audio item with optional `pan` (0..1) and `gain_db`
    dialogue_chain(name)                           per-clip dialogue filter text: 'clean_speech' (existing) or
                                                   'dialogue_v2_ffmpeg' (the FFmpeg-expressible part of dialogue.py)
"""
from __future__ import annotations

from . import design


def music_line(idx: int, music: dict, dur: float, speech_segments: list[dict] | None = None, rate: int = 48000) -> tuple[str, bool]:
    """Returns (filter line producing [mus], needs_sidechain). needs_sidechain=True means: keep the renderer's existing
    sidechaincompress step; False means the duck is already inside [mus]."""
    fo = max(0.0, dur - 2.0)
    base = (f"[{idx}:a]aresample={rate},aformat=channel_layouts=stereo,volume={music.get('volume', 0.5)},"
            f"atrim=0:{dur:.3f},afade=t=in:st=0:d=1,afade=t=out:st={fo:.3f}:d=2")
    mode = music.get("duck", True)
    if mode == "keyframes" and speech_segments is not None:
        d = design.ducking(speech_segments, dur)
        expr = design.volume_expr(d["volume_keyframes"]).replace(",", "\\,")
        return f"{base},volume='{expr}':eval=frame[mus]", False
    return f"{base}[mus]", bool(mode)


def item_line(i: int, item: dict, rate: int = 48000) -> str:
    """[i+1:a] -> [n{i}] like the renderer's current line, plus constant-power `pan` and `gain_db` when present."""
    import math
    ms = int(float(item.get("start", 0)) * 1000)
    parts = [f"aresample={rate}", "aformat=channel_layouts=stereo", f"volume={item.get('volume', 1.0)}"]
    if item.get("gain_db"):
        parts.append(f"volume={float(item['gain_db']):.2f}dB")
    if item.get("pan") is not None and abs(float(item["pan"]) - 0.5) > 1e-3:
        th = max(0.0, min(1.0, float(item["pan"]))) * math.pi / 2
        gl, gr = math.cos(th) * math.sqrt(2), math.sin(th) * math.sqrt(2)
        parts.append(f"pan=stereo|c0={gl:.4f}*c0|c1={gr:.4f}*c1")
    parts.append(f"adelay={ms}:all=1")
    return f"[{i + 1}:a]{','.join(parts)}[n{i}]"


# FFmpeg-only subset of dialogue chain v2 (no WPE / breath / per-speaker / room-tone steps, which need the numpy pre-pass
# dialogue.process_file). Noise reduction stays the measured clean_speech default.
def dialogue_chain(name: str = "dialogue_v2_ffmpeg") -> str:
    from . import post
    if name == "clean_speech":
        return post.segment_chain("clean_speech")
    if name == "dialogue_v2_ffmpeg":
        dn = post.segment_chain("clean_speech").split(",agate")[0]   # highpass + arnndn (or the afftdn fallback)
        return (f"{dn},deesser=i=0.3:m=0.5:f=0.5:s=o,"
                "acompressor=threshold=-24dB:ratio=2:attack=12:release=180:makeup=1")
    raise KeyError(name)

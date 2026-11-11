"""Self-generated, fully-owned test footage: narrated slide videos with deliberate flaws
(background hiss, low level, a long pause, a repeated take) so the editor has real work to do."""
from __future__ import annotations

from pathlib import Path

from nexus import project
from nexus.media import ff
from nexus.speech import tts
from nexus.timeline import ops, render

PALETTE = ["0x14324a", "0xb04a32", "0x1f7a34", "0xd0b040", "0x5a2a9a", "0x20a0a4", "0x7a1414"]


def make_talk(out: Path, sentences: list[str], lang: str = "en", voice: str | None = None, pause_after: int | None = None,
              repeat: int | None = None, noisy: bool = True, size=(1280, 720)) -> Path:
    if out.exists():
        return out
    work = out.parent / f"_{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    seq = list(enumerate(sentences))
    if repeat is not None:
        seq.insert(repeat + 1, (repeat, sentences[repeat]))  # the speaker says the line twice (a retake)
    tl = project.new_timeline(*size, lang=lang)
    tl["burn_subtitles"] = False
    t = 0.0
    for n, (i, s) in enumerate(seq):
        r = tts.synthesize(s, work / f"s{n}.wav", lang, voice)
        gap = 3.0 if pause_after is not None and i == pause_after and (repeat is None or n != repeat) else 0.35
        d = r["duration"] + gap
        tl["clips"].append(ops.color_clip(d, PALETTE[i % len(PALETTE)], text=[{"text": f"Part {i + 1}", "style": "title"}]))
        tl["audio"].append({"src": r["path"], "start": t + 0.1})
        t += d
    clean = work / "clean.mp4"
    render.render(tl, clean, work)
    if noisy:  # quiet + hiss, like a laptop microphone
        ff.run(["-i", clean, "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.02", "-filter_complex",
                "[0:a]volume=0.25[a];[a][1:a]amix=inputs=2:duration=first:normalize=0[o]", "-map", "0:v", "-map", "[o]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", out])
    else:
        clean.rename(out)
    return out


SOLAR = ["Solar panels turn sunlight directly into electricity.",
         "In the last ten years the price of solar power fell by almost ninety percent.",
         "That makes it the cheapest source of new electricity in most countries.",
         "The main challenge is storage, because the sun does not shine at night.",
         "Batteries are solving that problem faster than anyone expected."]
WIND = ["Wind turbines capture the energy of moving air.",
        "A single modern offshore turbine can power sixteen thousand homes.",
        "Wind is strongest in winter, exactly when solar power is weakest.",
        "Together, wind and solar complement each other through the year."]
GRID = ["The electricity grid has to balance supply and demand every second.",
        "Smart grids use software to shift demand to the hours when clean power is abundant.",
        "Electric cars can even feed power back into the grid during the evening peak."]

"""Transitions between clips: FFmpeg `xfade` names that were verified on the installed FFmpeg, plus friendly aliases.

NAMES was produced by running every transition `ffmpeg -h filter=xfade` lists on two tiny lavfi clips with
FFmpeg 6.1.1 (2026-11-12); `custom`/`expr` were dropped (they need an expression). `verify()` re-runs that check
on the current machine if FFmpeg is upgraded or replaced.
"""
from __future__ import annotations

import subprocess

NAMES: tuple[str, ...] = (
    "fade", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft", "slideright", "slideup", "slidedown",
    "circlecrop", "rectcrop", "distance", "fadeblack", "fadewhite", "radial", "smoothleft", "smoothright", "smoothup",
    "smoothdown", "circleopen", "circleclose", "vertopen", "vertclose", "horzopen", "horzclose", "dissolve", "pixelize",
    "diagtl", "diagtr", "diagbl", "diagbr", "hlslice", "hrslice", "vuslice", "vdslice", "hblur", "fadegrays", "wipetl",
    "wipetr", "wipebl", "wipebr", "squeezeh", "squeezev", "zoomin", "fadefast", "fadeslow", "hlwind", "hrwind", "vuwind",
    "vdwind", "coverleft", "coverright", "coverup", "coverdown", "revealleft", "revealright", "revealup", "revealdown",
)

ALIASES: dict[str, str] = {
    "crossfade": "fade", "cross": "fade", "mix": "fade",
    "whip_left": "slideleft", "whip_right": "slideright", "whip_up": "slideup", "whip_down": "slidedown",
    "push_left": "coverleft", "push_right": "coverright", "push_up": "coverup", "push_down": "coverdown",
    "dip_black": "fadeblack", "dip_to_black": "fadeblack", "dip_white": "fadewhite", "dip_to_white": "fadewhite",
    "flash": "fadewhite", "wipe": "wipeleft", "wipe_left": "wipeleft", "wipe_right": "wiperight",
    "iris_open": "circleopen", "iris_close": "circleclose", "iris": "circleopen", "zoom": "zoomin", "zoom_in": "zoomin",
    "blur": "hblur", "smooth": "smoothleft", "pixel": "pixelize", "pixelate": "pixelize", "glitch": "pixelize",
    "slice": "hlslice", "radial_wipe": "radial", "gray": "fadegrays", "grey": "fadegrays", "dissolve_noise": "dissolve",
    "barn_door": "horzopen", "split": "vertopen", "squeeze": "squeezeh", "wind": "hlwind",
    "reveal_left": "revealleft", "reveal_right": "revealright",
}


def resolve(name: str | None, default: str = "fade") -> str:
    """Friendly name or xfade name -> a verified xfade name (unknown names fall back to `default`)."""
    if not name:
        return default
    n = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    if n in NAMES:
        return n
    if n in ALIASES:
        return ALIASES[n]
    n2 = n.replace("_", "")
    return n2 if n2 in NAMES else default


def is_transition(name: str | None) -> bool:
    """True when `name` names a real transition (not a hard cut)."""
    if not name:
        return False
    n = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    return n in NAMES or n in ALIASES or n.replace("_", "") in NAMES


def verify(names: tuple[str, ...] | list[str] = NAMES) -> dict[str, bool]:
    """Run every transition once on two 1 s, 64x36 lavfi clips; returns {name: works}."""
    from .. import costguard
    costguard.authorize("ffmpeg", "verify-xfade")
    out = {}
    for n in names:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=s=64x36:r=10:d=1",
                            "-f", "lavfi", "-i", "color=red:s=64x36:r=10:d=1", "-filter_complex",
                            f"[0][1]xfade=transition={n}:duration=0.5:offset=0.5,format=yuv420p", "-f", "null", "-"],
                           capture_output=True, text=True)
        out[n] = p.returncode == 0
    return out

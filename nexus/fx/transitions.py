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


# ---------------------------------------------------------------- extended transitions (xfade custom expressions)
# FFmpeg's xfade evaluates `expr` per pixel and plane: X, Y, W, H are the current plane's coordinates and size, P goes
# 1 -> 0 over the transition, A/B are the two inputs' values and a0..a3(x,y)/b0..b3(x,y) sample any pixel of plane 0..3
# (FFmpeg >= 6.0). Verified on FFmpeg 6.1.1; `custom_supported()` re-checks, and a machine without it falls back to the
# nearest built-in transition (FALLBACK), so a render never fails because of a transition.
_Q = "(1-P)"  # progress 0 -> 1


def _smp(src: str, x: str, y: str) -> str:
    xs, ys = f"clip({x},0,W-1)", f"clip({y},0,H-1)"
    return (f"if(eq(PLANE,0),{src}0({xs},{ys}),if(eq(PLANE,1),{src}1({xs},{ys}),"
            f"if(eq(PLANE,2),{src}2({xs},{ys}),{src}3({xs},{ys}))))")


def _bg() -> str:
    return "if(eq(PLANE,0),16,if(eq(PLANE,3),255,128))"


def _mixmask(m: str, soft: float = 0.12) -> str:
    """A -> B where the 0..1 threshold map `m` is passed by the progress, with a soft edge."""
    return f"(A*(1-clip(({_Q}*(1+{soft})-({m}))/{soft},0,1))+B*clip(({_Q}*(1+{soft})-({m}))/{soft},0,1))"


def _luma(m: str, soft: float = 0.12) -> str:
    return _mixmask(m, soft)


_DX, _DY = "(X-W/2)", "(Y-H/2)"
_R = f"(hypot({_DX},{_DY})/hypot(W/2,H/2))"
_ANG = f"((atan2({_DY},{_DX})+PI)/(2*PI))"
_NOISE = "(0.5+0.25*sin(X*0.031+Y*0.017)+0.15*sin(X*0.013-Y*0.029+2.1)+0.1*sin((X+Y)*0.061+0.7))"


def _spin() -> str:
    q = _Q
    t, s1, s2 = f"({q}*PI)", f"(1-{q}*1.8)", f"(({q}-0.5)*2)"
    ax = f"(W/2+({_DX}*cos({t})+{_DY}*sin({t}))/max({s1},0.01))"
    ay = f"(H/2+(-{_DX}*sin({t})+{_DY}*cos({t}))/max({s1},0.01))"
    t2 = f"(({q}-1)*PI)"
    bx = f"(W/2+({_DX}*cos({t2})+{_DY}*sin({t2}))/max({s2},0.01))"
    by = f"(H/2+(-{_DX}*sin({t2})+{_DY}*cos({t2}))/max({s2},0.01))"
    ina = f"between({ax},0,W-1)*between({ay},0,H-1)"
    inb = f"between({bx},0,W-1)*between({by},0,H-1)"
    return (f"if(lt({q},0.5),if({ina},{_smp('a', ax, ay)},{_bg()}),"
            f"if({inb},{_smp('b', bx, by)},{_bg()}))")


def _whip(direction: int) -> str:
    q = _Q
    taps = []
    for k in (-2, -1, 0, 1, 2):
        u = f"(X+{direction}*({q}*W+{k}*sin(PI*{q})*W*0.035))"
        if direction > 0:
            taps.append(f"if(lt({u},W),{_smp('a', u, 'Y')},{_smp('b', f'({u}-W)', 'Y')})")
        else:
            taps.append(f"if(gte({u},0),{_smp('a', u, 'Y')},{_smp('b', f'({u}+W)', 'Y')})")
    return "((" + "+".join(taps) + ")/5)"


def _zoom_blur() -> str:
    q = _Q
    za, zb = f"(1+{q}*2.2)", f"(1+(1-{q})*2.2)"
    ta, tb = [], []
    for k in (0.0, 0.04, 0.08):
        fa, fb = f"({za}*(1+{k}*sin(PI*{q})))", f"({zb}*(1+{k}*sin(PI*{q})))"
        ta.append(_smp("a", f"(W/2+{_DX}/{fa})", f"(H/2+{_DY}/{fa})"))
        tb.append(_smp("b", f"(W/2+{_DX}/{fb})", f"(H/2+{_DY}/{fb})"))
    mix = f"clip(({q}-0.35)/0.3,0,1)"
    return f"((({'+'.join(ta)})/3)*(1-{mix})+(({'+'.join(tb)})/3)*{mix})"


def _blur_dissolve() -> str:
    q, r = _Q, "(sin(PI*(1-P))*W*0.012+0.5)"
    pts = [("X", "Y"), (f"X+{r}", "Y"), (f"X-{r}", "Y"), ("X", f"Y+{r}"), ("X", f"Y-{r}")]
    ta = "+".join(_smp("a", x, y) for x, y in pts)
    tb = "+".join(_smp("b", x, y) for x, y in pts)
    return f"((({ta})/5)*P+(({tb})/5)*{q})"


def _glitch() -> str:
    q = _Q
    band = "floor(Y/(H/18))"
    r = f"abs(sin({band}*12.9898+floor({q}*11)*78.233))"
    sh = f"(({r}-0.5)*W*0.25*sin(PI*{q})*gt({r},0.55))"
    xa = f"(X+{sh}+if(eq(PLANE,1),W*0.01,if(eq(PLANE,2),-W*0.01,0))*sin(PI*{q}))"
    return f"if(lt({q},0.5),{_smp('a', xa, 'Y')},{_smp('b', xa, 'Y')})"


def _film_burn() -> str:
    q, soft = _Q, 0.22
    m = _NOISE
    mix = f"clip(({q}*(1+{soft})-{m})/{soft},0,1)"
    edge = f"(1-abs({mix}*2-1))"
    base = f"(A*(1-{mix})+B*{mix})"
    hot = "if(eq(PLANE,0),245,if(eq(PLANE,1),96,if(eq(PLANE,2),196,255)))"
    return f"({base}*(1-{edge}*0.85)+{hot}*{edge}*0.85)"


def _shape(r: str) -> str:
    """`r` is a 0..1 'distance' of the shape (0 at the centre); B grows out of the centre in that shape."""
    return _mixmask(f"clip({r},0,1)", 0.08)


_STAR = f"({_R}/(0.62+0.38*abs(cos(2.5*atan2({_DY},{_DX})))))"
_TH = f"atan2(-{_DY},{_DX})"  # polar angle, y up
_HEART_R = f"(2-2*sin({_TH})+sin({_TH})*sqrt(abs(cos({_TH})))/(sin({_TH})+1.4))"  # polar heart: level sets are scaled hearts
_HEART = f"(hypot({_DX},{_DY})/(H*0.8*max({_HEART_R},0.02)))"

CUSTOM: dict[str, str] = {
    "spin": _spin(),
    "whip_pan_left": _whip(1), "whip_pan_right": _whip(-1),
    "zoom_blur": _zoom_blur(),
    "blur_dissolve": _blur_dissolve(),
    "glitch_slices": _glitch(),
    "film_burn": _film_burn(),
    "luma_radial": _luma(_R, 0.15),
    "luma_clouds": _luma(_NOISE, 0.18),
    "luma_diagonal": _luma("((X/W+Y/H)/2)", 0.1),
    "luma_clock": _luma(f"((atan2({_DX},-{_DY})+PI)/(2*PI))", 0.04),
    "luma_ripple": _luma(f"(0.75*{_R}+0.25*(0.5+0.5*sin({_R}*40)))", 0.12),
    "luma_bars": _luma("(0.5+0.5*sin(X/W*PI*12))*0.3+X/W*0.7", 0.08),
    "shape_diamond": _shape(f"(abs({_DX})/(W/2)+abs({_DY})/(H/2))/2"),
    "shape_star": _shape(_STAR),
    "shape_heart": _shape(_HEART),
    "shape_cross": _shape(f"min(abs({_DX})/(W/2),abs({_DY})/(H/2))*2.2"),
    "shape_hexagon": _shape(f"max(abs({_DX})/(W/2)*0.866+abs({_DY})/(H/2)*0.5,abs({_DY})/(H/2))"),
}
FALLBACK = {"spin": "radial", "whip_pan_left": "slideleft", "whip_pan_right": "slideright", "zoom_blur": "zoomin",
            "blur_dissolve": "hblur", "glitch_slices": "pixelize", "film_burn": "fadewhite", "luma_radial": "circleopen",
            "luma_clouds": "dissolve", "luma_diagonal": "diagbr", "luma_clock": "radial", "luma_ripple": "circleopen",
            "luma_bars": "hlslice", "shape_diamond": "circleopen", "shape_star": "circleopen", "shape_heart": "circleopen",
            "shape_cross": "vertopen", "shape_hexagon": "circleopen"}
ALIASES.update({"whip_pan": "whip_pan_left", "whip": "whip_pan_left", "spin_in": "spin", "zoom_blur_in": "zoom_blur",
                "glitch_rgb": "glitch_slices", "burn": "film_burn", "luma": "luma_clouds", "luma_wipe": "luma_clouds",
                "clock_wipe": "luma_clock", "diamond": "shape_diamond", "star": "shape_star", "heart": "shape_heart",
                "hexagon": "shape_hexagon"})
ALL: tuple[str, ...] = NAMES + tuple(CUSTOM)
_custom_ok: bool | None = None


def custom_supported() -> bool:
    """True when this FFmpeg can sample other pixels in xfade expressions (a0(x,y), FFmpeg >= 6.0)."""
    global _custom_ok
    if _custom_ok is None:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=red:s=16x16:r=5:d=1",
                            "-f", "lavfi", "-i", "color=blue:s=16x16:r=5:d=1", "-filter_complex",
                            "[0][1]xfade=transition=custom:expr='a0(X,Y)*P+B*(1-P)':duration=0.4:offset=0.2", "-f", "null", "-"],
                           capture_output=True, text=True)
        _custom_ok = p.returncode == 0
    return _custom_ok


def filter_for(name: str, duration: float, offset: float) -> str:
    """The xfade filter (without labels) for a resolved transition name."""
    n = resolve(name)
    if n in CUSTOM:
        if custom_supported():
            return f"xfade=transition=custom:expr='{CUSTOM[n]}':duration={duration:.6f}:offset={offset:.6f}"
        n = FALLBACK[n]
    return f"xfade=transition={n}:duration={duration:.6f}:offset={offset:.6f}"


def resolve(name: str | None, default: str = "fade") -> str:
    """Friendly name or xfade name -> a verified xfade name (unknown names fall back to `default`)."""
    if not name:
        return default
    n = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    if n in NAMES or n in CUSTOM:
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
    return n in NAMES or n in CUSTOM or n in ALIASES or n.replace("_", "") in NAMES


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

"""3D LUT management (.cube): validate, store, list, generate, and render with FFmpeg lut3d.

    validate(path)            -> {"ok", "size", "domain", "why"?}      parses the Adobe/Resolve .cube text format
    install(path, name)       -> stored copy in NEXUS_HOME/assets/luts/<name>.cube (validated first)
    builtin(name)             -> path of a LUT NEXUS generates itself (own work, no third-party licence)
    resolve(name_or_path)     -> absolute path for a timeline `lut` value
    filter_for(value)         -> "lut3d=file='...':interp=tetrahedral" (+ optional intensity mix)

Timeline: `lut` on a clip or the timeline: "name" | "/path/x.cube" | {"name"|"path", "intensity": 0..1}. Applied after
technical correction and the creative grade. LUTs from the internet carry their own licences: NEXUS stores the
user's own files and records where they came from; it does not download LUT packs.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .. import config

BUILTIN = ("identity", "warm_film", "teal_orange", "bleach_bypass", "rec709_lift")


def library() -> Path:
    d = config.ASSETS / "luts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate(path: str | Path) -> dict:
    """Check a .cube file: LUT_3D_SIZE present (2..256), N^3 rows of three floats, domain sane."""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "why": "file not found"}
    size, rows, dmin, dmax = None, 0, [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.upper().startswith("LUT_1D_SIZE"):
                return {"ok": False, "why": "1D LUT: only 3D .cube LUTs are supported"}
            if s.upper().startswith("LUT_3D_SIZE"):
                size = int(s.split()[1])
                continue
            if s.upper().startswith("DOMAIN_MIN"):
                dmin = [float(x) for x in s.split()[1:4]]
                continue
            if s.upper().startswith("DOMAIN_MAX"):
                dmax = [float(x) for x in s.split()[1:4]]
                continue
            if s.upper().startswith("TITLE") or re.match(r"^[A-Z_]+\b", s):
                continue
            vals = s.split()
            if len(vals) != 3:
                return {"ok": False, "why": f"bad data row: {s[:40]}"}
            [float(v) for v in vals]
            rows += 1
    except (ValueError, IndexError) as e:
        return {"ok": False, "why": f"unreadable: {e}"}
    if not size or not 2 <= size <= 256:
        return {"ok": False, "why": "missing or invalid LUT_3D_SIZE"}
    if rows != size ** 3:
        return {"ok": False, "why": f"expected {size ** 3} rows for size {size}, found {rows}"}
    if any(b <= a for a, b in zip(dmin, dmax)):
        return {"ok": False, "why": "DOMAIN_MAX must exceed DOMAIN_MIN"}
    return {"ok": True, "size": size, "domain": [dmin, dmax]}


def write_cube(path: Path, fn, size: int = 17, title: str = "") -> Path:
    """Write a 3D LUT from fn(r, g, b) -> (r, g, b) in 0..1 (red varies fastest, as .cube requires)."""
    lines = [f'TITLE "{title or path.stem}"', f"LUT_3D_SIZE {size}"]
    n = size - 1
    for b in range(size):
        for g in range(size):
            for r in range(size):
                o = fn(r / n, g / n, b / n)
                lines.append(" ".join(f"{min(1.0, max(0.0, v)):.6f}" for v in o))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _looks():
    def lum(r, g, b):
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def warm(r, g, b):
        return (r * 1.06 + 0.02, g * 1.01 + 0.01, b * 0.90)

    def teal_orange(r, g, b):
        y = lum(r, g, b)
        k = 0.18
        return (r + k * (y - 0.35), g + 0.03 * (0.5 - abs(y - 0.5)), b - k * (y - 0.45))

    def bleach(r, g, b):
        y = lum(r, g, b)
        c = 1.15
        return tuple(min(1, max(0, (0.55 * v + 0.45 * y - 0.5) * c + 0.5)) for v in (r, g, b))

    def lift(r, g, b):
        return tuple(0.04 + 0.94 * v for v in (r, g, b))

    return {"identity": lambda r, g, b: (r, g, b), "warm_film": warm, "teal_orange": teal_orange,
            "bleach_bypass": bleach, "rec709_lift": lift}


def builtin(name: str) -> Path:
    if name not in BUILTIN:
        raise KeyError(f"unknown built-in LUT '{name}' (have: {', '.join(BUILTIN)})")
    p = library() / f"_builtin_{name}.cube"
    if not p.exists():
        write_cube(p, _looks()[name], 17, f"NEXUS {name} (generated, own work)")
    return p


def install(path: str | Path, name: str | None = None, source: str = "user file") -> dict:
    v = validate(path)
    if not v["ok"]:
        raise ValueError(f"not a usable 3D LUT: {v['why']}")
    nm = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or Path(path).stem)[:60]
    dest = library() / f"{nm}.cube"
    shutil.copyfile(path, dest)
    (library() / f"{nm}.source.txt").write_text(source, encoding="utf-8")
    return {"name": nm, "path": str(dest), **v}


def list_luts() -> list[dict]:
    for b in BUILTIN:
        builtin(b)
    out = []
    for p in sorted(library().glob("*.cube")):
        nm = p.stem.removeprefix("_builtin_")
        src = p.with_suffix(".source.txt")
        out.append({"name": nm, "path": str(p), "builtin": p.stem.startswith("_builtin_"),
                    "source": src.read_text(encoding="utf-8") if src.exists() else "NEXUS (generated)"})
    return out


def resolve(value) -> tuple[Path, float]:
    """(path, intensity) for a timeline `lut` value."""
    if isinstance(value, dict):
        ref, k = value.get("path") or value.get("name"), float(value.get("intensity", 1.0))
    else:
        ref, k = value, 1.0
    ref = str(ref)
    if ref in BUILTIN:
        return builtin(ref), k
    p = Path(ref)
    if not p.is_absolute():
        p = library() / (ref if ref.endswith(".cube") else f"{ref}.cube")
    v = validate(p)
    if not v["ok"]:
        raise ValueError(f"LUT '{ref}': {v['why']}")
    return p, max(0.0, min(1.0, k))


def _esc(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _mixed(p: Path, k: float) -> Path:
    """LUT blended with identity by k: equal to mixing the graded and original pixels (a LUT is per-pixel)."""
    import hashlib
    out = library() / f"_mix_{hashlib.sha1(p.read_bytes()).hexdigest()[:10]}_{int(round(k * 1000)):04d}.cube"
    if out.exists():
        return out
    size, rows, head = None, [], []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        t = line.strip()
        if t.upper().startswith("LUT_3D_SIZE"):
            size = int(t.split()[1])
            head.append(t)
        elif t.upper().startswith(("DOMAIN_MIN", "DOMAIN_MAX")):
            head.append(t)
        elif t and not t.startswith("#") and len(t.split()) == 3 and not re.match(r"^[A-Z_]+\b", t):
            rows.append([float(v) for v in t.split()])
    n = size - 1
    lines = [f'TITLE "{p.stem} at {k:.0%}"', *head]
    i = 0
    for b in range(size):
        for g in range(size):
            for r in range(size):
                ident = (r / n, g / n, b / n)
                lines.append(" ".join(f"{k * v + (1 - k) * o:.6f}" for v, o in zip(rows[i], ident)))
                i += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def filter_for(value) -> str:
    """lut3d filter text; intensity < 1 uses a LUT blended with identity."""
    p, k = resolve(value)
    if k < 0.999:
        p = _mixed(p, k)
    return f"lut3d=file='{_esc(p)}':interp=tetrahedral"


def signature(value) -> str:
    import hashlib
    p, k = resolve(value)
    return f"{hashlib.sha1(p.read_bytes()).hexdigest()[:12]}|{k:.3f}"

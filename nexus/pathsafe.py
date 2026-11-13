"""Path containment: resolve a user-supplied relative path under a root and refuse anything that escapes it.

String-prefix checks (`str(p).startswith(str(root))`) accept sibling directories such as `<root>-evil/x`; this module
compares resolved path components instead. `resolve()` follows symlinks, so a link inside the root that points outside
is rejected too.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePath


class PathEscape(ValueError):
    """The requested path is not inside the allowed root."""


def contained(root: str | os.PathLike, rel: str | os.PathLike, *, must_be_file: bool = False) -> Path:
    """Return `root/rel` resolved, or raise PathEscape if it is absolute, uses `..`, or resolves outside `root`."""
    if rel is None or str(rel).strip() == "" or "\x00" in str(rel):
        raise PathEscape("empty or invalid path")
    rp = PurePath(rel)
    if rp.is_absolute() or rp.anchor or str(rel).startswith(("/", "\\")):
        raise PathEscape(f"absolute path not allowed: {rel}")
    if ".." in rp.parts or ".." in Path(str(rel).replace("\\", "/")).parts:
        raise PathEscape(f"parent traversal not allowed: {rel}")
    base = Path(root).resolve()
    target = (base / rp).resolve()
    if not target.is_relative_to(base):
        raise PathEscape(f"path escapes its root: {rel}")
    if must_be_file and not target.is_file():
        raise PathEscape(f"not a file: {rel}")
    return target


def is_within(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    """True if `path` (after resolving symlinks) is `root` or inside it."""
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False

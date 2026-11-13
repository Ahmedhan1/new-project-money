"""Import an earlier NEXUS data folder (a source checkout's `.nexus`) into this install's NEXUS_HOME.

Copy, never move: the old folder is left exactly as it was. Refuses when this NEXUS_HOME already holds a database (no
merging, no overwriting). Transient folders (run/, logs/, cache/, tmp/) are not copied. The encrypted vault is copied
with its key so existing connections keep working; nothing is decrypted or printed.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import config

SKIP = {"run", "logs", "cache", "tmp", "test-home"}


class ImportRefused(RuntimeError):
    pass


def import_home(src: str | os.PathLike, dst: str | os.PathLike | None = None) -> dict:
    s, d = Path(src).resolve(), Path(dst or config.HOME).resolve()
    if not (s / "nexus.db").is_file():
        raise ImportRefused(f"{s} is not a NEXUS data folder (no nexus.db)")
    if s == d or d in s.parents or s in d.parents:
        raise ImportRefused("source and destination overlap")
    if (d / "nexus.db").exists():
        raise ImportRefused(f"{d} already has NEXUS data; nothing imported (the existing data is never overwritten)")
    d.mkdir(parents=True, exist_ok=True)
    copied = []
    for item in sorted(s.iterdir()):
        if item.name in SKIP or item.is_symlink():
            continue
        if item.is_dir():
            shutil.copytree(item, d / item.name, symlinks=False, dirs_exist_ok=False)
        else:
            shutil.copy2(item, d / item.name)
        copied.append(item.name)
    sec = d / "secrets"
    if sec.is_dir():
        os.chmod(sec, 0o700)
        for f in sec.iterdir():
            os.chmod(f, 0o600)
    return {"from": str(s), "to": str(d), "copied": copied}

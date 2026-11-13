"""Content-addressed cache for media-to-media outputs (generation, enhancement, VFX) + resumable checkpoints.

    k = key(op, params, inputs=[paths], model="realesr-general-x4v3@rev")
    hit = MediaCache().get(k, ".mp4")            -> Path | None (touches last-used time)
    out = MediaCache().path_for(k, ".mp4")       -> where to write; then .put(k, out, meta)
    MediaCache().usage() / .prune(max_gb)        least-recently-used eviction
    ck = Checkpoint(path); ck.done("chunk-3"); ck.mark("chunk-3", {...}); ck.state

Input identity = size + mtime + sha256 of the first and last MiB (fast for multi-GB sources, and any edit changes it).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .. import config


def file_identity(path: str | Path) -> str:
    p = Path(path)
    st = p.stat()
    h = hashlib.sha256(f"{st.st_size}:{st.st_mtime_ns}".encode())
    with p.open("rb") as f:
        h.update(f.read(1 << 20))
        if st.st_size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()[:24]


def key(op: str, params: dict, inputs: list[str | Path] = (), model: str | None = None) -> str:
    blob = json.dumps({"op": op, "params": params, "model": model,
                       "inputs": [file_identity(i) for i in inputs]}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class MediaCache:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else config.CACHE / "media"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, k: str, ext: str) -> Path:
        d = self.root / k[:2]
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{k}{ext}"

    def _meta(self, k: str) -> Path:
        return self.root / k[:2] / f"{k}.json"

    def get(self, k: str, ext: str) -> Path | None:
        p = self.path_for(k, ext)
        m = self._meta(k)
        if p.exists() and p.stat().st_size > 0 and m.exists():
            try:
                meta = json.loads(m.read_text(encoding="utf-8"))
                meta["last_used"] = time.time()
                m.write_text(json.dumps(meta), encoding="utf-8")
            except ValueError:
                return None
            return p
        return None

    def meta(self, k: str) -> dict | None:
        m = self._meta(k)
        return json.loads(m.read_text(encoding="utf-8")) if m.exists() else None

    def put(self, k: str, path: str | Path, meta: dict | None = None) -> Path:
        p = Path(path)
        if not p.exists() or p.stat().st_size == 0:
            raise FileNotFoundError(f"refusing to cache a missing/empty output {p}")
        self._meta(k).write_text(json.dumps({**(meta or {}), "file": p.name, "bytes": p.stat().st_size,
                                             "created": time.time(), "last_used": time.time()}, default=str), encoding="utf-8")
        return p

    def entries(self) -> list[dict]:
        out = []
        for m in self.root.glob("*/*.json"):
            try:
                meta = json.loads(m.read_text(encoding="utf-8"))
            except ValueError:
                continue
            f = m.parent / meta.get("file", "")
            out.append({"key": m.stem, "meta": m, "file": f, "bytes": f.stat().st_size if f.exists() else 0,
                        "last_used": meta.get("last_used", 0)})
        return out

    def usage(self) -> dict:
        e = self.entries()
        return {"entries": len(e), "bytes": sum(x["bytes"] for x in e), "gb": round(sum(x["bytes"] for x in e) / 2**30, 3)}

    def prune(self, max_gb: float) -> dict:
        e = sorted(self.entries(), key=lambda x: x["last_used"])
        total = sum(x["bytes"] for x in e)
        removed = 0
        while e and total > max_gb * 2**30:
            x = e.pop(0)
            for f in (x["file"], x["meta"]):
                Path(f).unlink(missing_ok=True)
            total -= x["bytes"]
            removed += 1
        return {"removed": removed, "bytes_left": total}


class Checkpoint:
    """Which work items of a long job are finished (JSON on disk, atomic replace). Survives a crash or a lost worker;
    RenderExecutionResult.checkpoint carries `state` back so a retry skips finished items."""

    def __init__(self, path: str | Path, initial: dict | None = None):
        self.path = Path(path)
        self.state = {"done": {}, "started": time.time()}
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        if initial:
            self.state["done"].update((initial.get("done") or {}))

    def done(self, item: str) -> bool:
        return item in self.state["done"]

    def mark(self, item: str, result: dict | None = None) -> None:
        self.state["done"][item] = result or {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, default=str), encoding="utf-8")
        tmp.replace(self.path)

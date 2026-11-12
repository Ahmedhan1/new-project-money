"""Resumable rendering: a checkpoint file next to the render's work files, in the shape of
RenderExecutionResult.checkpoint (nexus/contracts, docs/ACCOUNT2_CONTRACTS.md).

Every expensive intermediate (clip segments, burned pieces, the finished audio mix) is cached by a content key and
written atomically (tmp file + rename), so an interrupted render loses at most the files that were being written.
The checkpoint records which of them were finished, so a retry (same process or another worker, same NEXUS_HOME)
can say what it reused. The final output is also written atomically: a killed render never leaves a truncated
programme at the output path.

    checkpoint = {"version": 1, "timeline_sha": str, "output": str, "draft": bool, "stage": "segments"|"pieces"|
                  "audio"|"final"|"done", "stages_done": [str], "segments_total": int, "segments_done": [key],
                  "pieces_total": int, "pieces_done": [key], "audio_done": bool, "attempt": int, "resumed": bool,
                  "reused": {"segments": int, "pieces": int, "audio": bool}, "updated": float}
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

VERSION = 1
STALE_TMP_S = 900


def timeline_sha(tl: dict, draft: bool) -> str:
    return hashlib.sha1(json.dumps([tl, bool(draft)], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def path_for(workdir: Path, out: Path) -> Path:
    return Path(workdir) / f"checkpoint_{Path(out).stem}.json"


def load(workdir: Path, out: Path) -> dict | None:
    p = path_for(workdir, out)
    try:
        cp = json.loads(p.read_text(encoding="utf-8"))
        return cp if isinstance(cp, dict) and cp.get("version") == VERSION else None
    except (OSError, ValueError):
        return None


class Checkpoint:
    """Thread-safe checkpoint writer used by render.render()."""

    def __init__(self, workdir: Path, out: Path, tl: dict, draft: bool, seed: dict | None = None):
        self.path = path_for(workdir, out)
        sha = timeline_sha(tl, draft)
        prev = seed if seed is not None else load(workdir, out)
        same = bool(prev) and prev.get("timeline_sha") == sha and prev.get("stage") != "done"
        self.prev = prev if same else None
        self.lock = threading.Lock()
        self.data = {"version": VERSION, "timeline_sha": sha, "output": str(out), "draft": bool(draft), "stage": "segments",
                     "stages_done": [], "segments_total": 0, "segments_done": [], "pieces_total": 0, "pieces_done": [],
                     "audio_done": False, "attempt": (int(prev.get("attempt", 1)) + 1) if same else 1, "resumed": same,
                     "previous": ({k: prev.get(k) for k in ("stage", "stages_done", "segments_done", "pieces_done",
                                                             "audio_done", "updated")} if same else None),
                     "reused": {"segments": 0, "pieces": 0, "audio": False}, "updated": time.time()}
        self.save()

    def save(self) -> None:
        self.data["updated"] = time.time()
        tmp = self.path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)

    def done(self, kind: str, key: str, cached: bool) -> None:
        with self.lock:
            lst = self.data[f"{kind}_done"]
            if key not in lst:
                lst.append(key)
            if cached:
                self.data["reused"][kind] += 1
            self.save()

    def stage(self, name: str, **extra) -> None:
        with self.lock:
            if self.data["stage"] not in self.data["stages_done"] and self.data["stage"] != name:
                self.data["stages_done"].append(self.data["stage"])
            self.data["stage"] = name
            self.data.update(extra)
            self.save()

    def finish(self) -> dict:
        self.stage("done")
        return dict(self.data)


def clean_stale_tmp(cache: Path, max_age_s: float = STALE_TMP_S) -> int:
    """Remove temporary files left by a killed render (older than max_age_s, so a concurrent render's live tmp
    files are never touched). Returns how many were removed."""
    n, now = 0, time.time()
    if not cache.exists():
        return 0
    for p in cache.glob("*.tmp.*"):
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def execution_result(request_id: str, stats: dict | None = None, error: BaseException | None = None,
                     checkpoint: dict | None = None) -> dict:
    """A RenderExecutionResult for a finished or failed render (interrupted renders are retryable and carry the
    checkpoint a retry resumes from)."""
    from .. import contracts
    if error is None and stats:
        outs = [contracts.artifact(stats["output"], role="programme", media_type="video/mp4", hash_it=False)]
        outs += [contracts.artifact(p, role=k, hash_it=False) for k, p in (stats.get("sidecars") or {}).items()]
        metrics = {k: stats[k] for k in ("duration", "elapsed", "segments", "segments_from_cache", "pieces",
                                         "pieces_from_cache", "audio_from_cache", "workers", "draft") if k in stats}
        return contracts.make("RenderExecutionResult", request_id=request_id, status="done", outputs=outs, metrics=metrics,
                              checkpoint=stats.get("checkpoint") or checkpoint)
    code = "TIMEOUT" if "timed out" in str(error) else "INTERNAL"
    if isinstance(error, (FileNotFoundError,)) or "missing" in str(error).lower():
        code = "INPUT_MISSING"
    if isinstance(error, (KeyboardInterrupt, InterruptedError)):
        code = "PROVIDER_LOST"
    return contracts.make("RenderExecutionResult", request_id=request_id, status="failed", outputs=[], metrics={},
                          error={"code": code, "message": str(error)[:2000], "retryable": code != "INPUT_MISSING"},
                          checkpoint=checkpoint)

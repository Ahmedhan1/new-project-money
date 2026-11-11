"""Projects: originals are never modified; every timeline save is a new version (undo/redo/branches)."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time
from pathlib import Path

from . import config, db


def new_timeline(width: int = 1920, height: int = 1080, lang: str = "en") -> dict:
    return {"version": 1, "width": width, "height": height, "fps": config.FPS, "lang": lang, "grade": "none",
            "audio_fx": "none", "target_lufs": -14.0, "clips": [], "audio": [], "music": None, "subtitles": [],
            "burn_subtitles": True, "markers": []}


class Project:
    def __init__(self, pid: str):
        rows = db.query("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise KeyError(pid)
        self.id = pid
        self.name = rows[0]["name"]
        self.kind = rows[0]["kind"]
        self.data: dict = json.loads(rows[0]["data"])
        self.dir = config.PROJECTS / pid

    # ---- lifecycle
    @classmethod
    def create(cls, name: str, kind: str = "edit", **data) -> "Project":
        pid = db.new_id("prj")
        d = config.PROJECTS / pid
        for sub in ("media", "timelines", "renders", "reports", "work", "audio"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        base = {"media": [], "branches": {"main": {"versions": [], "head": -1}}, "analysis": {}, "style": {}, **data}
        db.execute("INSERT INTO projects(id,name,kind,created,data) VALUES(?,?,?,?,?)",
                   (pid, name, kind, time.time(), json.dumps(base, ensure_ascii=False)))
        return cls(pid)

    @staticmethod
    def list() -> list[dict]:
        return db.query("SELECT id,name,kind,created FROM projects ORDER BY created DESC")

    def save(self) -> None:
        db.execute("UPDATE projects SET data=?, name=? WHERE id=?", (json.dumps(self.data, ensure_ascii=False), self.name, self.id))

    # ---- media (originals preserved, read-only)
    def import_media(self, src: str | Path) -> dict:
        src = Path(src)
        sha = hashlib.sha256(src.read_bytes()).hexdigest()
        for m in self.data["media"]:
            if m["sha256"] == sha:
                return m
        dest = self.dir / "media" / f"{len(self.data['media']):02d}_{src.name}"
        shutil.copy2(src, dest)
        dest.chmod(0o444)
        m = {"id": f"m{len(self.data['media'])}", "name": src.name, "path": str(dest), "sha256": sha, "rights": "user-supplied"}
        self.data["media"].append(m)
        self.save()
        return m

    # ---- timeline versions
    def _branch(self, branch: str) -> dict:
        return self.data["branches"].setdefault(branch, {"versions": [], "head": -1})

    def save_timeline(self, tl: dict, note: str = "", branch: str = "main") -> str:
        b = self._branch(branch)
        b["versions"] = b["versions"][: b["head"] + 1]  # a new edit discards the redo tail
        vid = f"{branch}-{len(b['versions']):03d}-{int(time.time() * 1000) % 100000}"
        (self.dir / "timelines" / f"{vid}.json").write_text(json.dumps(tl, indent=1, ensure_ascii=False))
        b["versions"].append({"id": vid, "note": note, "ts": time.time()})
        b["head"] = len(b["versions"]) - 1
        self.save()
        return vid

    def timeline(self, branch: str = "main", vid: str | None = None) -> dict:
        b = self._branch(branch)
        if vid is None:
            if b["head"] < 0:
                raise KeyError(f"no timeline on branch {branch}")
            vid = b["versions"][b["head"]]["id"]
        return json.loads((self.dir / "timelines" / f"{vid}.json").read_text())

    def undo(self, branch: str = "main") -> dict:
        b = self._branch(branch)
        b["head"] = max(0, b["head"] - 1)
        self.save()
        return self.timeline(branch)

    def redo(self, branch: str = "main") -> dict:
        b = self._branch(branch)
        b["head"] = min(len(b["versions"]) - 1, b["head"] + 1)
        self.save()
        return self.timeline(branch)

    def fork(self, new_branch: str, from_branch: str = "main", note: str = "") -> dict:
        tl = copy.deepcopy(self.timeline(from_branch))
        self.save_timeline(tl, note or f"forked from {from_branch}", new_branch)
        return tl

    def history(self) -> dict:
        return {k: {"head": v["head"], "versions": v["versions"]} for k, v in self.data["branches"].items()}

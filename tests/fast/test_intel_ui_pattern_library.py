"""Creator References page: the pattern library's hooks are objects ({"text": ...}); the UI must print their text, not
"[object Object]" (found by the independent QA browser pass over every page, 2026-11-13)."""
import re
from pathlib import Path

from nexus.intelligence.creators import library

JS = Path(__file__).resolve().parents[2] / "nexus" / "ui" / "intelligence.js"


def test_library_hooks_have_text_and_the_ui_renders_it():
    hooks = [h for a in library.archetypes() for h in a.get("hook_patterns") or []]
    assert hooks and all(isinstance(h, str) or (isinstance(h, dict) and h.get("text")) for h in hooks)
    src = JS.read_text(encoding="utf-8")
    m = re.search(r'\["Hooks",x=>esc\(\(x\.hook_patterns\|\|\[\]\)\.map\(h=>(.*?)\)\.', src)
    assert m, "pattern library Hooks column not found"
    assert "h.text" in m.group(1) and 'typeof h==="string"' in m.group(1)

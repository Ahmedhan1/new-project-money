"""QA-D5: a page whose data arrives after the user navigated away must not overwrite the new page. The browser proof
is scripts/qa/ui_nav_race.js (docs/results/final_qa/ui/nav_race_after_fix.json); this test pins the mechanism."""
import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "nexus" / "ui" / "index.html"


def test_qa_d5_go_guards_late_renders_with_a_navigation_token():
    s = UI.read_text(encoding="utf-8")
    go = re.search(r"function go\(id,arg\)\{(.*?\n.*?)\n", s, re.S).group(1)
    assert "++NAV" in go and "view(el,tok)" in go and "tok===NAV" in go
    view = re.search(r"function view\(el,tok\)\{.*?\}\)\}", s, re.S).group(0)
    assert "set(t,k,v){if(tok===NAV)" in view          # stale innerHTML writes are dropped


def test_every_wan_model_has_a_pinned_revision():
    src = (UI.parents[1] / "cloud" / "worker_kit" / "tasks" / "a2_wan_video.py").read_text(encoding="utf-8")
    repos = re.search(r"_WAN_REPOS = \{(.*?)\}\n", src, re.S).group(1)
    assert "None" not in repos and len(re.findall(r'"[0-9a-f]{40}"', repos)) == 2

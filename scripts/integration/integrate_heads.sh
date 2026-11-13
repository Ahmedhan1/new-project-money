#!/bin/bash
# Reproducible three-account integration: A1 -> merge A2 -> merge A3 -> apply.sh (keep-both resolutions).
# Usage: scripts/integration/integrate_heads.sh <A1 sha> <A2 sha> <A3 sha> <worktree dir> <local branch>
# Then: NEXUS_HOME=$(mktemp -d) python -m pytest tests/fast -p no:cacheprovider --junitxml=junit.xml (in the worktree,
# venv from its requirements-ci.txt). Nothing is pushed.
set -e
T=$(mktemp -d)
A1=$1; A2=$2; A3=$3; DIR=$4; BR=$5
G="git -c user.name=nexus -c user.email=nexus@localhost"
cd "$(git rev-parse --show-toplevel)"
git branch -f $BR $A1
rm -rf $DIR; git worktree prune; git worktree add -q $DIR $BR
cd $DIR
union() { git show :1:$1 > "$T/b"; git show :2:$1 > "$T/o"; git show :3:$1 > "$T/t"; git merge-file -p --union "$T/o" "$T/b" "$T/t" > $1; }
ours_docs() { for f in NEXUS_HANDOFF.md NEXUS_STATE.json docs/DECISIONS.md docs/MASTER_ROADMAP.md docs/results/fast_tests.json docs/AGENT_WORK_LOG.md; do
  git ls-files -u -- $f | grep -q . && git checkout --ours -- $f; done; true; }
$G merge --no-ff -q $A2 -m "integration: merge Account 2 ${A2:0:7}" || { ours_docs; union nexus/registry/tasks.yaml; git add -A; $G commit -q --no-edit; }
$G merge --no-ff -q $A3 -m "integration: merge Account 3 ${A3:0:7}" || {
  ours_docs; union nexus/registry/tasks.yaml
  python3 - <<'PY'
import re
from pathlib import Path
def resolve(path, fn):
    s = Path(path).read_text()
    s2, n = re.subn(r"<<<<<<< HEAD\n(.*?)=======\n(.*?)>>>>>>> [0-9a-f]+\n", lambda m: fn(m.group(1), m.group(2)), s, flags=re.S)
    Path(path).write_text(s2); return n
resolve("nexus/api.py", lambda a, b: a + "\n" + b.replace('app = FastAPI(title="NEXUS Video AI", version="0.1.0")\n\n', ""))
resolve("nexus/workflows.py", lambda a, b: a + b)
resolve("nexus/ui/index.html", lambda a, b: "")
p = Path("nexus/ui/index.html"); s = p.read_text()
s = s.replace('<script src="/ui/intelligence.js"></script>\n<script>go("home");</script>',
  '<script src="/ui/intelligence.js"></script>\n<script>go((location.hash.slice(1) in PAGES) ? location.hash.slice(1) : "home");  // deep links such as /#review</script>')
p.write_text(s)
PY
  ! grep -rl "^<<<<<<<\|^>>>>>>>" nexus tests docs NEXUS_*.* 2>/dev/null
  git add -A; $G commit -q --no-edit; }
bash integration/contracts_v1/apply.sh
git add -A; $G commit -q -m "integration: apply contracts_v1 (Account 3 apply.sh)"
git log --oneline -4

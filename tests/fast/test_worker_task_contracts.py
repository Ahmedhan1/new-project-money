"""Every worker task's ctx.artifact(...) kind must be a valid ArtifactKind: the dispatcher rejects the whole result
otherwise (found live on Kaggle 2026-11-13: a2_wan_video used kind 'generated-video' and a finished 5B run was discarded)."""
import ast
import typing
from pathlib import Path

from nexus import contracts

TASKS = Path(contracts.__file__).resolve().parents[1] / "cloud" / "worker_kit"


def _kinds():
    for f in [TASKS / "nexus_worker.py", *sorted((TASKS / "tasks").glob("*.py"))]:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("artifact", "write_json_artifact"):
                pos = 1
                arg = node.args[pos] if len(node.args) > pos else next((k.value for k in node.keywords if k.arg == "kind"), None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    yield f.name, node.lineno, arg.value


def test_worker_artifact_kinds_are_contract_valid():
    from nexus.contracts.cloud import WorkerExecutionResult
    art = typing.get_args(WorkerExecutionResult.model_fields["artifacts"].annotation)[0]
    allowed = set(typing.get_args(art.model_fields["kind"].annotation))
    assert "source" in allowed
    found = list(_kinds())
    assert found, "no ctx.artifact calls found - the scan is broken"
    bad = [(f, ln, k) for f, ln, k in found if k not in allowed]
    assert not bad, f"invalid artifact kinds {bad}; allowed: {sorted(allowed)}"

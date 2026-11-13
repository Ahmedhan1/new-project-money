"""Integration contracts v1: documented examples validate, extra fields are rejected, committed JSON Schemas equal the
code (a contract change without a version bump fails CI), task names are validated.

If test_committed_schemas_match fails you changed a contract. Additive optional fields: re-export with
`python -c "from nexus import contracts; contracts.export_schemas('docs/contracts')"` and update
docs/ACCOUNT1_CONTRACTS.md. Anything else (removed/renamed/retyped/now-required field, narrowed enum): add a v2 model
next to v1 instead - see the versioning policy in docs/ACCOUNT1_CLOUD_ARCHITECTURE.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus import contracts

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "docs" / "contracts"
DOC = ROOT / "docs" / "ACCOUNT1_CONTRACTS.md"
MODELS = {m.__name__: m for m in contracts.ALL}


def doc_examples() -> dict[str, list[dict]]:
    """Every ```json block in ACCOUNT1_CONTRACTS.md preceded by `<!-- example: ModelName -->`."""
    out: dict[str, list[dict]] = {}
    text = DOC.read_text(encoding="utf-8")
    for m in re.finditer(r"<!-- example: (\w+) -->\s*```json\n(.*?)```", text, re.S):
        out.setdefault(m.group(1), []).append(json.loads(m.group(2)))
    return out


def test_every_contract_has_a_valid_documented_example():
    ex = doc_examples()
    assert set(ex) == set(MODELS), f"missing examples: {set(MODELS) - set(ex)}; unknown: {set(ex) - set(MODELS)}"
    for name, items in ex.items():
        for e in items:
            obj = MODELS[name].model_validate(e)
            # round trip is lossless
            assert MODELS[name].model_validate(json.loads(obj.model_dump_json())) == obj


@pytest.mark.parametrize("name", sorted(MODELS))
def test_extra_fields_rejected(name):
    e = doc_examples()[name][0]
    with pytest.raises(ValidationError):
        MODELS[name].model_validate({**e, "unexpected_field": 1})


def test_contract_names_and_version():
    for name, m in MODELS.items():
        if "contract" not in m.model_fields:
            assert name == "ArtifactLocation"  # embedded value object, versioned with ArtifactManifest
            continue
        lit = m.model_fields["contract"].default
        assert lit.endswith(f"/{contracts.VERSION}")
        assert lit.split("/")[0] in (name, name.removesuffix("Info"))
        with pytest.raises(ValidationError):  # a v2 payload is never silently accepted as v1
            m.model_validate({**doc_examples()[name][0], "contract": lit.replace("/v1", "/v2")})


def test_committed_schemas_match(tmp_path):
    names = contracts.export_schemas(tmp_path)
    committed = sorted(p.name for p in SCHEMAS.glob("*.schema.json"))
    assert sorted(names) == committed, "docs/contracts/ out of date: re-export (see module docstring)"
    for n in names:
        fresh = json.loads((tmp_path / n).read_text(encoding="utf-8"))
        old = json.loads((SCHEMAS / n).read_text(encoding="utf-8"))
        assert fresh == old, f"{n}: contract changed without updating the committed schema / bumping the version"


@pytest.mark.parametrize("task,ok", [("probe", True), ("ffmpeg_transcode", True), ("render_timeline2", True),
                                     ("a", False), ("Probe", False), ("1probe", False), ("probe-x", False),
                                     ("probe x", False), ("p" * 49, True), ("p" * 50, False), ("../etc", False),
                                     ("probe;rm", False)])
def test_task_name_validation(task, ok):
    if ok:
        assert contracts.ProductionJobManifest(task=task).task == task
    else:
        with pytest.raises(ValidationError):
            contracts.ProductionJobManifest(task=task)


def test_field_constraints():
    with pytest.raises(ValidationError):
        contracts.ArtifactManifest(kind="master", name="x", mime="video/mp4", size=1, sha256="ABC")
    with pytest.raises(ValidationError):
        contracts.ArtifactManifest(kind="master", name="x", mime="video/mp4", size=-1, sha256="0" * 64)
    with pytest.raises(ValidationError):
        contracts.ProductionJobManifest(task="probe", priority=10)
    with pytest.raises(ValidationError):
        contracts.ProductionJobManifest(task="probe", max_retries=11)
    with pytest.raises(ValidationError):
        contracts.ProductionJobManifest(task="probe", max_runtime_s=0)
    with pytest.raises(ValidationError):
        contracts.ProductionEvent(type="job.exploded")
    a, b = contracts.ProductionJobManifest(task="probe"), contracts.ProductionJobManifest(task="probe")
    assert a.job_id != b.job_id and a.job_id.startswith("rjob_")


def test_no_secret_fields_in_contracts():
    """Contracts reference providers by connection_id; no field may be named like a credential."""
    bad = re.compile(r"token|secret|password|api_key|credential|refresh", re.I)
    for name, m in MODELS.items():
        for f in m.model_fields:
            assert not bad.search(f), f"{name}.{f} looks like a credential field"


def test_api_serves_the_same_schemas(home, monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDED_WORKER", "0")
    from fastapi.testclient import TestClient

    from nexus import api
    r = TestClient(api.app).get("/api/v1/contracts").json()
    assert r["version"] == contracts.VERSION and set(r["schemas"]) == set(MODELS)
    for n, s in r["schemas"].items():
        assert s == json.loads((SCHEMAS / f"{n}.{contracts.VERSION}.schema.json").read_text(encoding="utf-8"))

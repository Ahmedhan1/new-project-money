import pytest

from nexus import contracts as C


def test_valid_documents_round_trip(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 10)
    ref = C.artifact(f, role="master", media_type="video/mp4")
    assert ref["bytes"] == 10 and len(ref["sha256"]) == 64 and ref["uri"].startswith("file://")
    req = C.make("RenderExecutionRequest", request_id="r1", environment={"provider": "local", "gpu": None},
                 manifest=C.make("RenderJobManifest", job_id="j1", kind="render", inputs=[ref], settings={"draft": False},
                                 outputs=[{"uri": "file:///out.mp4", "role": "master"}], requirements={"gpu": False}))
    assert req["schema"] == "nexus.RenderExecutionRequest/1" and not C.validate(req)


def test_missing_wrong_type_and_semantic_errors():
    errs = C.validate({"schema": "nexus.RenderExecutionResult/1", "request_id": "r", "status": "exploded", "outputs": [], "metrics": []})
    assert any("status must be one of" in e for e in errs) and any("metrics must be dict" in e for e in errs)
    assert C.validate({"schema": "nexus.MediaAsset/1", "id": "a"})  # ref, kind, rights missing
    with pytest.raises(C.ContractError):
        C.make("GeneratedMediaManifest", model={}, task="t2v", inputs=[], output={}, classification="video",  # not a known class
               licence={}, compute={})
    assert any("version" in e for e in C.validate({"schema": "nexus.QualityReport/2", "output": "o", "status": "s", "stages": [],
                                                   "human_approval": "PENDING"}))
    assert C.validate({"schema": "nexus.Nope/1"}) == ["unknown schema 'nexus.Nope/1'"]


def test_nested_artifact_refs_are_checked():
    errs = C.validate({"schema": "nexus.ArtifactManifest/1", "producer": "x", "artifacts": [{"uri": "file:///a", "bytes": "big"}]})
    assert errs == ["ArtifactManifest.artifacts[0].ArtifactRef.bytes must be int, got str"]

"""Shared contract package: all three accounts' contracts load together, tags are unique, and a real document from each
account validates through the one entry point (no mocks: the real producers' builders are used)."""
from __future__ import annotations

import importlib

from nexus import contracts


def test_both_original_apis_survive_in_one_package():
    # Account 1 names (pydantic models + helpers) and Account 2 names (dict schemas) from the same import
    for n in ("ProductionJobManifest", "ArtifactLocation", "ArtifactManifest", "QuotaStatus", "MediaIngestionJob", "MediaSource",
              "ProviderHealth", "RemoteWorkerCapability", "WorkerExecutionResult", "ALL", "export_schemas", "new_id"):
        assert hasattr(contracts, n), n
    for n in ("make", "validate", "check", "artifact", "dumps", "SCHEMAS", "ContractError"):
        assert hasattr(contracts, n), n
    assert contracts.VERSION == "v1"  # Account 1's meaning, served by GET /api/v1/contracts
    assert importlib.import_module("nexus.contracts.cloud").VERSION == "v1"
    assert importlib.import_module("nexus.contracts.media").VERSION == 1


def test_registry_has_every_account_and_no_tag_collision():
    reg = contracts.registry()  # raises on a duplicate tag
    owners = {v["owner"] for v in reg.values()}
    assert owners == {"account-1", "account-2", "account-3"}
    assert "ArtifactManifest/v1" in reg and "nexus.ArtifactManifest/1" in reg  # same name, two forms, both addressable
    assert reg["nexus.ContentScript/1"]["module"] == "nexus.intelligence.contracts"


def test_validate_any_accepts_real_documents_from_each_account(tmp_path):
    from nexus.intelligence import contracts as intel
    a1 = contracts.ArtifactManifest(kind="master", name="a.mp4", sha256="0" * 64, size=1,
                                    mime="video/mp4", locations=[contracts.ArtifactLocation(provider="local", connection_id="local",
                                                                                                     ref="a.mp4")])
    assert contracts.validate_any(a1.model_dump(mode="json")) == []
    bad = a1.model_dump(mode="json") | {"unexpected": 1}  # extra="forbid" still enforced through the shared entry point
    assert contracts.validate_any(bad)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    a2 = contracts.make("RenderJobManifest", job_id="j", kind="render", inputs=[contracts.artifact(f)], settings={}, outputs=[],
                        requirements={})
    assert contracts.validate_any(a2) == []
    a3 = intel.make("Evidence", source="unit test", url="https://example.org")
    assert contracts.validate_any(a3) == []
    assert contracts.validate_any({"schema": "nexus.Nope/1"})
    assert contracts.validate_any({"contract": "Nope/v1"})
    assert contracts.validate_any({"schema": "nexus.RenderJobManifest/1", "job_id": 3})

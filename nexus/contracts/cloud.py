"""NEXUS integration contracts, version 1 (Account 1: cloud platform, storage, compute, automation).

Stable, versioned schemas that Accounts 2 (editing/rendering/video generation) and 3 (research, translation,
localization, publishing, analytics) use to submit work and receive artifacts. Rules:

* Every model carries `contract` = "<Name>/v1". A breaking change creates v2 alongside v1; v1 is never edited in place.
* Unknown extra fields are rejected (`extra="forbid"`) so a typo is an error, not silent data loss.
* Secrets never appear in any contract: providers are referenced by `connection_id`, never by token.
* Times are UNIX seconds (float, UTC). Sizes are bytes. Checksums are lowercase hex SHA-256.

Export: `contracts.export_schemas("docs/contracts")` writes docs/contracts/*.schema.json (JSON Schema for non-Python
clients); tests/fast/test_contracts_cloud.py fails if a contract changes without re-exporting (and versioning) it.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VERSION = "v1"
SHA256 = r"^[0-9a-f]{64}$"


def _now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class _C(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# ---------------------------------------------------------------- artifacts and storage
class ArtifactLocation(_C):
    """Where one copy of an artifact lives. `provider` is a StorageProvider id; `connection_id` names the authorised
    account (e.g. drive_a) - never a credential."""
    provider: str
    connection_id: str
    ref: str = Field(description="provider-native id or key (Drive file id, object key, local relative path)")
    path: str = Field("", description="human-readable path, e.g. NEXUS/Masters/prj_x/master.mp4")
    verified_at: float | None = None


class ArtifactManifest(_C):
    contract: Literal["ArtifactManifest/v1"] = "ArtifactManifest/v1"
    artifact_id: str = Field(default_factory=lambda: new_id("art"))
    kind: Literal["source", "proxy", "analysis", "master", "short", "localized", "thumbnail", "audio", "subtitle",
                  "project_file", "archive", "log", "checkpoint", "other"]
    name: str
    mime: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256)
    project_id: str | None = None
    produced_by_job: str | None = None
    locations: list[ArtifactLocation] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict, description="licence, source URL, generator, model...")
    retention: Literal["keep", "temporary", "archive"] = "keep"
    expires_at: float | None = None
    created_at: float = Field(default_factory=_now)


class QuotaStatus(_C):
    contract: Literal["QuotaStatus/v1"] = "QuotaStatus/v1"
    provider: str
    connection_id: str
    resource: Literal["storage_bytes", "gpu_seconds_week", "runtime_seconds", "api_requests", "egress_bytes", "other"]
    limit: float | None = Field(None, description="None = not exposed by the provider (never guessed)")
    used: float | None = None
    remaining: float | None = None
    period: str | None = Field(None, description="e.g. 'weekly', 'per-session'; None = not periodic")
    source: Literal["provider_api", "measured", "documented", "unknown"]
    measured_at: float = Field(default_factory=_now)
    exhausted: bool = False


class ProviderHealth(_C):
    contract: Literal["ProviderHealth/v1"] = "ProviderHealth/v1"
    provider: str
    connection_id: str
    status: Literal["ok", "degraded", "unavailable", "unauthorized", "quota_exhausted", "not_configured"]
    detail: str = ""
    identity: str | None = Field(None, description="authorised account e-mail/username, shown to the owner only")
    scopes: list[str] = Field(default_factory=list)
    last_success: float | None = None
    last_error: str | None = None
    checked_at: float = Field(default_factory=_now)
    quotas: list[QuotaStatus] = Field(default_factory=list)


class StorageProviderInfo(_C):
    """Static description of a StorageProvider implementation (the behaviour contract is nexus.cloud.storage.StorageProvider)."""
    contract: Literal["StorageProvider/v1"] = "StorageProvider/v1"
    provider: str
    display: str
    capabilities: list[Literal["upload", "resumable_upload", "download", "list", "delete", "move", "checksum",
                               "versions", "quota", "folders"]]
    max_connections: int = 1
    billing_required: bool = False
    notes: str = ""


# ---------------------------------------------------------------- media intake
class MediaSource(_C):
    contract: Literal["MediaSource/v1"] = "MediaSource/v1"
    kind: Literal["upload", "http_url", "drive_file", "provider_api", "artifact"]
    ref: str = Field(description="URL, Drive file id, provider item id, or artifact id")
    connection_id: str | None = None
    rights: Literal["owned", "licensed", "public_domain", "authorised", "unknown"] = "unknown"
    licence: str | None = None
    attribution: str | None = None
    declared_by: str = Field("owner", description="who asserted the rights (recorded with the import)")


class MediaIngestionJob(_C):
    contract: Literal["MediaIngestionJob/v1"] = "MediaIngestionJob/v1"
    ingestion_id: str = Field(default_factory=lambda: new_id("ing"))
    source: MediaSource
    project_id: str | None = None
    destination: str | None = Field(None, description="connection_id of the storage that keeps the original")
    max_bytes: int = Field(4 * 2 ** 30, ge=1)
    allowed_mime: list[str] = Field(default_factory=lambda: ["video/", "audio/", "image/"])
    status: Literal["queued", "validating", "downloading", "verifying", "stored", "rejected", "failed"] = "queued"
    reason: str | None = None
    artifact: ArtifactManifest | None = None


# ---------------------------------------------------------------- compute
class RemoteWorkerCapability(_C):
    contract: Literal["RemoteWorkerCapability/v1"] = "RemoteWorkerCapability/v1"
    provider: Literal["local", "kaggle", "colab", "user_gpu", "other"]
    connection_id: str
    accelerator: str | None = Field(None, description="measured, e.g. 'Tesla T4 x2'; None = CPU only")
    vram_gb: float | None = None
    ram_gb: float | None = None
    cpus: int | None = None
    max_runtime_s: float | None = None
    frameworks: list[str] = Field(default_factory=list)
    internet: bool = False
    measured: bool = Field(False, description="True only when the values come from a completed probe job")
    measured_at: float | None = None


class JobCheckpoint(_C):
    contract: Literal["JobCheckpoint/v1"] = "JobCheckpoint/v1"
    job_id: str
    step: str
    seq: int = Field(ge=0)
    state: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list, description="artifact ids produced so far")
    created_at: float = Field(default_factory=_now)


class ProductionJobManifest(_C):
    """A unit of remote work. Account 2/3 modules set `task` to their own registered task name."""
    contract: Literal["ProductionJobManifest/v1"] = "ProductionJobManifest/v1"
    job_id: str = Field(default_factory=lambda: new_id("rjob"))
    task: str = Field(description="registered task name, e.g. 'probe', 'ffmpeg_transcode', 'render_timeline', 'translate'")
    owner_account: Literal["account1", "account2", "account3"] = "account1"
    project_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    inputs: list[str] = Field(default_factory=list, description="artifact ids the worker needs")
    output_destination: str | None = Field(None, description="storage connection_id for results")
    requires: dict[str, Any] = Field(default_factory=dict, description="e.g. {'gpu': true, 'vram_gb': 12}")
    priority: int = Field(5, ge=0, le=9, description="0 = most urgent")
    max_runtime_s: float = Field(3600, gt=0)
    max_retries: int = Field(2, ge=0, le=10)
    preferred_providers: list[str] = Field(default_factory=list)
    idempotency_key: str | None = Field(None, description="same key = same job; resubmission returns the existing one")
    created_at: float = Field(default_factory=_now)

    @field_validator("task")
    @classmethod
    def _task_name(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,48}", v):
            raise ValueError("task must be lower_snake_case, 2-49 chars")
        return v


class WorkerExecutionResult(_C):
    contract: Literal["WorkerExecutionResult/v1"] = "WorkerExecutionResult/v1"
    job_id: str
    provider: str
    connection_id: str
    status: Literal["succeeded", "failed", "cancelled", "lost", "quota_exhausted"]
    started_at: float | None = None
    finished_at: float | None = None
    duration_s: float | None = None
    accelerator: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict, description="gpu util, peak vram, cpu, sizes, download time...")
    artifacts: list[ArtifactManifest] = Field(default_factory=list)
    log_tail: str = ""
    error: str | None = None
    checkpoint: JobCheckpoint | None = None


class AIProviderCapability(_C):
    """Declared by Account 3 for its AI providers; Account 1 only routes and meters, never hardcodes a provider."""
    contract: Literal["AIProviderCapability/v1"] = "AIProviderCapability/v1"
    provider: str
    connection_id: str
    tasks: list[str]
    models: list[str] = Field(default_factory=list)
    local: bool = False
    commercial_use: Literal["yes", "no", "unverified"] = "unverified"
    quotas: list[QuotaStatus] = Field(default_factory=list)


# ---------------------------------------------------------------- events
class ProductionEvent(_C):
    contract: Literal["ProductionEvent/v1"] = "ProductionEvent/v1"
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    job_id: str | None = None
    type: Literal["job.submitted", "job.dispatched", "job.progress", "job.checkpoint", "job.succeeded", "job.failed",
                  "job.cancelled", "job.retrying", "worker.lost", "quota.exhausted", "storage.uploaded",
                  "storage.failed", "ingest.stored", "ingest.rejected", "scheduler.paused", "scheduler.resumed",
                  "scheduler.stopped", "provider.health"]
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    at: float = Field(default_factory=_now)


class NotificationEvent(_C):
    contract: Literal["NotificationEvent/v1"] = "NotificationEvent/v1"
    notification_id: str = Field(default_factory=lambda: new_id("ntf"))
    level: Literal["info", "success", "warning", "error"]
    title: str
    body: str = ""
    source_event: str | None = None
    read: bool = False
    at: float = Field(default_factory=_now)


ALL = [ArtifactLocation, ArtifactManifest, QuotaStatus, ProviderHealth, StorageProviderInfo, MediaSource, MediaIngestionJob,
       RemoteWorkerCapability, JobCheckpoint, ProductionJobManifest, WorkerExecutionResult, AIProviderCapability,
       ProductionEvent, NotificationEvent]


def export_schemas(dest) -> list[str]:
    import json
    from pathlib import Path
    d = Path(dest)
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for m in ALL:
        p = d / f"{m.__name__}.{VERSION}.schema.json"
        p.write_text(json.dumps(m.model_json_schema(), indent=1), encoding="utf-8")
        out.append(p.name)
    return out

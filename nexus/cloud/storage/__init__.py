"""Account 1 multi-account cloud storage (sub-agent C).

    base.py         StorageProvider contract + crash-safe resumable upload engine
    local.py        LocalProvider (NEXUS_HOME/storage/local) - default destination, used by tests
    gdrive.py       GoogleDriveProvider (REST, drive.file scope), connections drive_a / drive_b / drive_c
    oauth.py        Google OAuth: web-server flow (state + PKCE) and device flow, refresh, revocation
    connections.py  cloud_connections rows (identity, scopes, status, last_success, last_error)
    transfers.py    transfers ledger (session URI + acknowledged bytes) for resume after a crash
    artifacts.py    store / dedupe / manifests / copy + move between connections / retention cleanup / placement
    fake_drive.py   protocol-faithful local fake of Google OAuth + Drive v3 (tests and offline evidence only)
    setup_google.py `python -m nexus.cloud.storage.setup_google <client.json>` (owner stores the OAuth client)

The older backup backends in nexus/storage (local folder / rclone) are separate and unchanged.
"""
from __future__ import annotations

from ...contracts import StorageProviderInfo
from . import connections
from .base import (ChecksumMismatch, NotConfigured, QuotaExceeded, ReauthRequired, RemoteFile, StorageError,
                   StorageProvider)

__all__ = ["get_provider", "provider_infos", "StorageProvider", "RemoteFile", "StorageError", "NotConfigured",
           "ReauthRequired", "QuotaExceeded", "ChecksumMismatch"]


def get_provider(connection_id: str) -> StorageProvider:
    kind = connections.provider_of(connection_id)
    connections.ensure(connection_id)
    if kind == "local":
        from .local import LocalProvider
        return LocalProvider()
    from .gdrive import GoogleDriveProvider
    return GoogleDriveProvider(connection_id)


def provider_infos() -> list[StorageProviderInfo]:
    from .gdrive import INFO as G
    from .local import INFO as L
    return [L, G]

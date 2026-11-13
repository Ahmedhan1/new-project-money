"""Account 1 cloud platform: remote compute, multi-account storage, media ingestion, scheduler, Remote Studio.

Package layout and ownership (see docs/ACCOUNT1_CLOUD_ARCHITECTURE.md):
    events.py      ProductionEvent / NotificationEvent bus (lead)
    security.py    owner authentication, rate limits, audit (sub-agent A)
    compute/       remote workers: Kaggle, Colab handoff, user GPU, dispatcher, checkpoints (sub-agent B)
    storage/       StorageProvider adapters: local, Google Drive (A/B/C), transfers, manifests (sub-agent C)
    ingest.py      safe URL / Drive / upload intake (sub-agent C)
    scheduler.py   autonomous production scheduler (sub-agent D)
    routes_*.py    FastAPI routers mounted under /api/v1 (one per owner)
"""

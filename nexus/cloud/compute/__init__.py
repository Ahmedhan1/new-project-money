"""Account 1 remote compute (sub-agent B): providers, dispatcher, checkpoints, quota, owner pull workers.

    base.py        Provider interface, ProviderError / QuotaExhausted, per-connection state
    kaggle.py      KaggleProvider - official `kaggle` CLI only, one account ("kaggle_primary"), private kernels
    local.py       LocalProvider - the same worker script in a local subprocess (CPU failover)
    pull.py        user_gpu / colab pull workers (per-worker tokens, hashed in the vault), Colab notebook generator
    tasks.py       dispatcher-side task registry (TaskSpec: gpu need, resumable...)
    dispatcher.py  submit / route / dispatch / poll with backoff / retrieve / SHA-256 verify / checkpoint resume / cancel
    poller.py      background poll thread + a durable-job handler ("remote-compute-tick")
Worker sources: nexus/cloud/worker_kit/.
"""
from .base import Provider, ProviderError, QuotaExhausted, RemoteStatus  # noqa: F401

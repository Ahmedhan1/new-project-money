"""Thin adapter over Account 1's secret vault (nexus/vault.py on work/account-1-cloud-platform), else environment vars.

This package never stores a token itself: the DB keeps only a *reference* (secret name). Without the vault, secrets
are read from environment variables `NEXUS_PUB_<NAME>` (dots/dashes -> underscores, upper case) and cannot be written.
"""
from __future__ import annotations

import os
import re

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,63}$")  # same rule as Account 1's vault


class CredentialStoreUnavailable(RuntimeError):
    pass


def _vault():
    try:
        from ... import vault  # type: ignore[attr-defined]  # Account 1
        return vault
    except Exception:
        return None


def backend() -> str:
    return "account1-vault" if _vault() else "env"


def secret_name(platform: str, account_ref: str, kind: str) -> str:
    n = re.sub(r"[^a-z0-9_.-]", "-", f"pub.{platform}.{account_ref}.{kind}".lower())[:64]
    if not NAME_RE.match(n):
        raise ValueError(f"invalid secret name {n!r}")
    return n


def env_name(name: str) -> str:
    return "NEXUS_PUB_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def get(name: str) -> str | None:
    v = _vault()
    if v is not None:
        try:
            got = v.get(name)
            if got:
                return got
        except Exception:
            pass
    return os.environ.get(env_name(name)) or None


def put(name: str, value: str, meta: dict | None = None) -> str:
    v = _vault()
    if v is None:
        raise CredentialStoreUnavailable(
            f"Account 1's vault (nexus/vault.py) is not installed on this branch; set the environment variable "
            f"{env_name(name)} instead, or install the vault. NEXUS will not write tokens to disk itself.")
    v.put(name, value, meta or {})
    return name


def delete(name: str) -> bool:
    v = _vault()
    return bool(v and v.delete(name))

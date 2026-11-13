"""Production deployment profile (Account 1).

Development is the default: loopback-only access without a token, a vault key generated next to the data, interactive
API docs. `NEXUS_ENV=production` switches to the production profile. There the app refuses to start unless every
required gate below passes, and it never falls back to development behaviour:

  owner_token        NEXUS_API_TOKEN set (>= 32 chars); loopback mode is not available in production
  vault_key          NEXUS_VAULT_KEY injected by the host's secret store and a valid Fernet key
  vault_key_offdisk  no vault.key file inside NEXUS_HOME (the key must not sit next to the data it protects)
  vault_readable     every stored secret decrypts with that key (a wrong key fails here, not at the first upload)
  public_origin      NEXUS_PUBLIC_ORIGIN set and https-only (CSRF Origin check)
  bind_loopback      the API binds to a loopback address; TLS terminates in the owner's reverse proxy
  trusted_proxies    NEXUS_TRUSTED_PROXIES set (per-client rate limiting behind the proxy)
  database           schema readable
  disk               free space >= settings.min_free_disk_gb
  backup_offsite     at least one enabled backup destination outside NEXUS_HOME
  backup_encryption  NEXUS_BACKUP_PASSPHRASE set when a remote destination is configured
  single_instance    no other API process holds NEXUS_HOME/run/api.lock

Every check reports a name, ok, a non-secret detail and a fix; no value of any secret is ever read into a detail.
`python -m nexus production-check` prints them; GET /api/v1/health/ready returns 503 while a required gate fails.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
from pathlib import Path

from .. import config

MIN_TOKEN_LEN = 32
LOOPBACK_BIND = {"127.0.0.1", "::1", "localhost"}


class ProductionConfigError(RuntimeError):
    """Raised at startup in production when a required gate fails. The message names checks and fixes only."""


def mode() -> str:
    return "production" if os.environ.get("NEXUS_ENV", "").strip().lower() in ("production", "prod") else "development"


def is_production() -> bool:
    return mode() == "production"


def _under(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def checks(bind_host: str | None = None) -> list[dict]:
    out: list[dict] = []

    def add(name: str, ok: bool, detail: str, fix: str = "", required: bool = True) -> None:
        out.append({"check": name, "ok": bool(ok), "required": required, "detail": detail, "fix": "" if ok else fix})

    tok = os.environ.get("NEXUS_API_TOKEN") or ""
    add("owner_token", len(tok) >= MIN_TOKEN_LEN,
        "set" + (f", {len(tok)} chars" if tok else "") if tok else "NEXUS_API_TOKEN is not set",
        f"inject a random owner token of at least {MIN_TOKEN_LEN} characters via the host's secret store "
        "(e.g. python -c \"import secrets;print(secrets.token_urlsafe(48))\" run on the host, never pasted in chat)")

    key = os.environ.get("NEXUS_VAULT_KEY") or ""
    key_ok = False
    if key:
        try:
            from cryptography.fernet import Fernet
            Fernet(key.encode())
            key_ok = True
        except Exception:  # noqa: BLE001
            key_ok = False
    add("vault_key", key_ok, "injected, valid Fernet key" if key_ok else ("injected but not a valid Fernet key" if key else "NEXUS_VAULT_KEY is not set"),
        "inject the vault key via the host's secret store (docs/ACCOUNT1_DISASTER_RECOVERY.md, 'Vault key')")

    kf = config.HOME / "secrets" / "vault.key"
    add("vault_key_offdisk", not kf.exists(), "no key file in NEXUS_HOME" if not kf.exists() else "NEXUS_HOME/secrets/vault.key exists",
        "move the key to the host's secret store and an offline copy (python -m nexus vault-key export --to <removable drive>), "
        "check it (vault-key verify), then delete NEXUS_HOME/secrets/vault.key")

    if key_ok:
        try:
            from .routes_security import vault_status
            v = vault_status()
            add("vault_readable", v["ok"], f"{v['secrets']} secret(s), {v['undecryptable']} undecryptable",
                "the injected key is not the key these secrets were encrypted with: inject the right key "
                "(compare with python -m nexus vault-key verify --from-file <offline copy>)")
        except Exception as e:  # noqa: BLE001
            add("vault_readable", False, type(e).__name__, "check NEXUS_HOME/secrets permissions")
    else:
        add("vault_readable", False, "not checked: no valid vault key", "fix vault_key first")

    origins = [o.strip() for o in os.environ.get("NEXUS_PUBLIC_ORIGIN", "").split(",") if o.strip()]
    add("public_origin", bool(origins) and all(o.lower().startswith("https://") for o in origins),
        ", ".join(origins) if origins else "NEXUS_PUBLIC_ORIGIN is not set",
        "set NEXUS_PUBLIC_ORIGIN to the https origin the owner opens (e.g. https://nexus.example.lan); http is refused")

    host = (bind_host or config.settings().get("host") or "").strip().strip("[]")
    add("bind_loopback", host in LOOPBACK_BIND, f"bind {host or '?'}",
        "bind the API to 127.0.0.1 (settings.json \"host\") and expose it only through the reverse proxy that terminates TLS")

    proxies = [p.strip() for p in os.environ.get("NEXUS_TRUSTED_PROXIES", "").split(",") if p.strip()]
    bad = []
    for p in proxies:
        try:
            ipaddress.ip_address(p)
        except ValueError:
            bad.append(p)
    add("trusted_proxies", bool(proxies) and not bad, (", ".join(proxies) if proxies else "NEXUS_TRUSTED_PROXIES is not set") +
        (f" (not IP addresses: {', '.join(bad)})" if bad else ""),
        "set NEXUS_TRUSTED_PROXIES to the reverse proxy's address as the API sees it (usually 127.0.0.1)")

    try:
        from .. import db
        add("database", True, f"schema v{db.schema_version()}")
    except Exception as e:  # noqa: BLE001
        add("database", False, type(e).__name__, "restore the database from a verified backup (python -m nexus restore)")

    try:
        need = float(config.settings().get("min_free_disk_gb", 2.0))
        free = shutil.disk_usage(config.HOME).free / 2 ** 30
        add("disk", free >= need, f"{free:.1f} GiB free (minimum {need:g})", "free disk space on the NEXUS_HOME volume")
    except OSError as e:
        add("disk", False, type(e).__name__, "check NEXUS_HOME")

    try:
        from .. import storage
        dests = [d for d in storage.destinations() if d.get("enabled", True)]
    except Exception:  # noqa: BLE001
        dests = []
    offsite = [d for d in dests if d.get("provider") != "local" or not _under(Path(d.get("root", "")), config.HOME)]
    add("backup_offsite", bool(offsite), ", ".join(d["name"] for d in offsite) if offsite else
        "every backup destination is inside NEXUS_HOME (same disk)",
        "add a backup destination on another disk or an authorised Drive connection (docs/ACCOUNT1_DISASTER_RECOVERY.md)")
    remote = [d for d in dests if d.get("provider") != "local"]
    add("backup_encryption", not remote or bool(os.environ.get("NEXUS_BACKUP_PASSPHRASE")),
        "no remote destination" if not remote else ("passphrase injected" if os.environ.get("NEXUS_BACKUP_PASSPHRASE") else
                                                    "remote destination without NEXUS_BACKUP_PASSPHRASE"),
        "inject NEXUS_BACKUP_PASSPHRASE via the host's secret store (remote backups are only ever uploaded encrypted)")

    from . import instance
    add("single_instance", instance.available("api"),
        "lock held by this process" if instance.held_here("api") else ("free" if instance.available("api") else "another API process holds it"),
        "stop the other NEXUS API process on this NEXUS_HOME (python -m nexus stop); only one is supported")
    return out


def failing(bind_host: str | None = None) -> list[dict]:
    return [c for c in checks(bind_host) if c["required"] and not c["ok"]]


def enforce(bind_host: str | None = None) -> None:
    """Called at API startup. No-op in development; in production raise unless every required gate passes."""
    if not is_production():
        return
    bad = failing(bind_host)
    if bad:
        lines = "\n".join(f"  - {c['check']}: {c['detail']}\n    fix: {c['fix']}" for c in bad)
        raise ProductionConfigError(f"NEXUS_ENV=production but {len(bad)} required gate(s) fail:\n{lines}")

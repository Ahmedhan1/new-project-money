"""Store the owner's Google OAuth client in the NEXUS vault (run by the OWNER on the server; never commit the file).

    python -m nexus.cloud.storage.setup_google path/to/client_secret_XXXX.json [--delete-file]
    python -m nexus.cloud.storage.setup_google --status
    python -m nexus.cloud.storage.setup_google --remove

Create a FREE OAuth client (no billing account needed) - see docs/ACCOUNT1_PROVIDER_COMPARISON.md section
"Owner setup: Google OAuth client":
  1. https://console.cloud.google.com/ -> create a project (free; do NOT enable billing).
  2. APIs & Services -> Library -> enable "Google Drive API".
  3. Google Auth Platform / OAuth consent screen -> User type "External", app name "NEXUS", your e-mail; add the
     scopes .../auth/drive.file, openid, email; add your own Google accounts (drive_a/b/c) as TEST USERS.
     (While the app is in "Testing", refresh tokens expire after 7 days - publish the app to "In production" for
     long-lived tokens; drive.file is a non-sensitive scope, so no verification review is needed for personal use.)
  4. Clients -> Create client:
       "TVs and Limited Input devices"  -> device flow (works from any browser while NEXUS runs in a cloud container)
       "Web application"                -> web flow; add the redirect URI https://<your-host>/api/v1/storage/oauth/callback
                                           (http://localhost:<port>/... is accepted for local use)
  5. Download the JSON and run this command with its path. The file contents go into the encrypted vault only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ... import vault
from .oauth import CLIENT_SECRET_NAME


def parse_client(raw: dict) -> dict:
    kind = "flat"
    for k in ("installed", "web"):
        if k in raw:
            kind, raw = k, raw[k]
            break
    cid, secret = raw.get("client_id", ""), raw.get("client_secret", "")
    if not cid.endswith(".apps.googleusercontent.com") and not raw.get("allow_nonstandard"):
        raise ValueError("client_id does not look like a Google OAuth client id (*.apps.googleusercontent.com)")
    typ = raw.get("type") or {"installed": "limited_input_or_desktop", "web": "web"}.get(kind, "unknown")
    out = {"client_id": cid, "client_secret": secret, "type": typ}
    if raw.get("redirect_uris"):
        out["redirect_uris"] = raw["redirect_uris"]
    return out


def store(path: str | Path) -> dict:
    c = parse_client(json.loads(Path(path).read_text(encoding="utf-8")))
    vault.put(CLIENT_SECRET_NAME, json.dumps(c), {"client_id": c["client_id"], "type": c["type"]})
    return {"stored": True, "client_id": c["client_id"], "type": c["type"], "has_secret": bool(c["client_secret"])}


def status() -> dict:
    m = vault.meta(CLIENT_SECRET_NAME)
    return {"configured": m is not None, **({"client_id": m.get("client_id"), "type": m.get("type")} if m else {})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nexus.cloud.storage.setup_google")
    ap.add_argument("client_json", nargs="?")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--delete-file", action="store_true", help="delete the downloaded JSON after storing it")
    a = ap.parse_args(argv)
    if a.status:
        print(json.dumps(status()))
        return 0
    if a.remove:
        print(json.dumps({"removed": vault.delete(CLIENT_SECRET_NAME)}))
        return 0
    if not a.client_json:
        ap.print_help()
        return 2
    try:
        out = store(a.client_json)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if a.delete_file:
        Path(a.client_json).unlink()
        out["file_deleted"] = True
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

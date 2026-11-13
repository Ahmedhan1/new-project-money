"""OAuth 2.0 installed-app flow for YouTube with the OWNER's own Google Cloud client (loopback redirect + PKCE S256).

Documented at https://developers.google.com/identity/protocols/oauth2/native-app (read 2026-11-12): loopback
http://127.0.0.1:<port> is the recommended redirect for desktop apps, the out-of-band copy/paste flow "is no longer
supported", PKCE should always be used. Tokens go to Account 1's vault via `creds.put` (never into Git or the DB).

    python -m nexus.intelligence.publishing.youtube_oauth --label main     (owner, on a machine with a browser)
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import urllib.parse

from . import creds
from .adapters.base import AdapterBlocked, Transport

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]


def names(label: str) -> dict:
    return {k: creds.secret_name("youtube", label, k) for k in ("client_id", "client_secret", "refresh_token")}


def owner_instructions(label: str = "main") -> list[str]:
    n = names(label)
    return [
        "1. In https://console.cloud.google.com create (or pick) YOUR OWN project; it costs nothing and needs no card for this API.",
        "2. APIs & Services > Library: enable 'YouTube Data API v3' and 'YouTube Analytics API'.",
        "3. OAuth consent screen: External, add your Google account as a test user (or publish the app).",
        "4. Credentials > Create credentials > OAuth client ID > Application type 'Desktop app'. Keep the client ID + secret private.",
        f"5. Run: python -m nexus.intelligence.publishing.youtube_oauth --label {label}  (paste the client ID/secret when asked, "
        "sign in with the channel's account in the browser that opens).",
        f"6. Without Account 1's vault, set environment variables instead: {', '.join(creds.env_name(v) for v in n.values())}.",
        f"7. Register the account: engine.connect_account('youtube', '{label}', secret_ref='{n['refresh_token']}').",
        "NEXUS uploads as PRIVATE unless you approve a manifest yourself (approval = 'approved_by_owner').",
    ]


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(client_id: str, redirect_uri: str, challenge: str, state: str, scopes: list[str] | None = None) -> str:
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(scopes or SCOPES),
         "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "access_type": "offline", "prompt": "consent"}
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str, redirect_uri: str, transport=None) -> dict:
    body = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret, "code": code, "code_verifier": verifier,
                                   "grant_type": "authorization_code", "redirect_uri": redirect_uri}).encode()
    r = (transport or Transport()).request("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
    if r.status != 200:
        raise AdapterBlocked("oauth_failed", f"token exchange failed: HTTP {r.status} {r.body[:200]!r}", owner_instructions())
    return r.json()


def refresh(client_id: str, client_secret: str, refresh_token: str, transport=None) -> dict:
    body = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token,
                                   "grant_type": "refresh_token"}).encode()
    r = (transport or Transport()).request("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
    if r.status != 200:
        raise AdapterBlocked("auth_expired", f"refresh failed: HTTP {r.status}; reconnect the channel", owner_instructions())
    return r.json()


def loopback_flow(label: str, client_id: str, client_secret: str, timeout: float = 300.0, open_browser: bool = True,
                  transport=None) -> dict:
    """Run the consent flow on 127.0.0.1, store client id/secret + refresh token in the vault. Returns non-secret info."""
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    got: dict = {}
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                got.update(q)
                done.set()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"NEXUS: authorisation received, you can close this tab.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = auth_url(client_id, redirect, challenge, state)
    print("Open this URL and sign in with the channel's Google account:\n" + url)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        if not done.wait(timeout) or "code" not in got:
            raise AdapterBlocked("oauth_failed", f"no authorisation code received ({got.get('error', 'timeout')})", owner_instructions(label))
    finally:
        srv.shutdown()
    tok = exchange_code(client_id, client_secret, got["code"], verifier, redirect, transport)
    if not tok.get("refresh_token"):
        raise AdapterBlocked("oauth_failed", "Google returned no refresh token", owner_instructions(label))
    n = names(label)
    meta = {"platform": "youtube", "scopes": tok.get("scope", "").split()}
    creds.put(n["client_id"], client_id, meta)
    creds.put(n["client_secret"], client_secret, meta)
    creds.put(n["refresh_token"], tok["refresh_token"], meta)
    return {"label": label, "secret_ref": n["refresh_token"], "scopes": meta["scopes"]}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass
    ap = argparse.ArgumentParser(description="Connect a YouTube channel with your own Google OAuth client")
    ap.add_argument("--label", default="main")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    cid = input("OAuth client ID: ").strip()
    sec = getpass.getpass("OAuth client secret (hidden): ").strip()
    try:
        info = loopback_flow(a.label, cid, sec, open_browser=not a.no_browser)
    except (AdapterBlocked, creds.CredentialStoreUnavailable) as e:
        print(f"Not connected: {e}")
        return 2
    from . import engine
    engine.connect_account("youtube", a.label, secret_ref=info["secret_ref"], scopes=info["scopes"])
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Independent break-test of a running NEXUS server (normal-user and hostile-client behaviour). Standard library only.

    NEXUS_API_TOKEN=<test token> python scripts/qa/break_probe.py --base http://127.0.0.1:8899 --home <NEXUS_HOME of that server> \
        --out docs/results/final_qa/break_probe.json

Every probe records expected vs actual; nothing is retried to make it pass. The lockout probe runs last because it
blocks the client for the lockout window. Uses a disposable NEXUS_HOME and a disposable test token only.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

RESULTS: list[dict] = []


SRC = {"ip": "127.0.0.2"}


def use(n: int) -> None:
    """Each probe group talks from its own loopback address, so the per-client brute-force lockout of one group does not
    hide the results of the next (all of 127.0.0.0/8 is loopback on Linux)."""
    SRC["ip"] = f"127.0.0.{n}"


def req(base, method, path, *, headers=None, body=None, raw=None):
    import http.client
    u = urllib.parse.urlsplit(base)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=60, source_address=(SRC["ip"], 0))
    try:
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        return r.status, r.read(), {k.lower(): v for k, v in r.getheaders()}
    except (ConnectionError, OSError) as e:   # server closed the connection before reading the whole body
        return "connection_closed", str(e).encode(), {}
    finally:
        c.close()


def probe(area, name, expect, got, detail="", severity_if_fail="major"):
    ok = got in expect if isinstance(expect, (set, tuple, list)) else got == expect
    RESULTS.append({"area": area, "probe": name, "expected": sorted(map(str, expect)) if isinstance(expect, set) else expect, "actual": got,
                    "ok": ok, "detail": detail, "severity_if_fail": severity_if_fail})
    print(("PASS " if ok else "FAIL ") + f"[{area}] {name}: expected {expect}, got {got} {detail}")
    return ok


def png_bytes(w=64, h=64):
    raw = b"".join(b"\0" + bytes([120, 90, 60]) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def multipart(field, filename, content, ctype="application/octet-stream"):
    b = "----nexusqa" + str(int(time.time() * 1000))
    buf = io.BytesIO()
    buf.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
    buf.write(content)
    buf.write(f"\r\n--{b}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={b}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--home", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    base, tok = a.base.rstrip("/"), os.environ["NEXUS_API_TOKEN"]
    bearer = {"Authorization": f"Bearer {tok}"}
    origin = {"Origin": base}

    # --- authentication
    use(2)
    probe("auth", "anonymous GET /api/projects", 401, req(base, "GET", "/api/projects")[0])
    probe("auth", "anonymous GET /api/v1/system/status", 401, req(base, "GET", "/api/v1/system/status")[0])
    probe("auth", "wrong bearer token", 401, req(base, "GET", "/api/projects", headers={"Authorization": "Bearer nope"})[0])
    probe("auth", "correct bearer token", 200, req(base, "GET", "/api/projects", headers=bearer)[0])
    probe("auth", "anonymous POST /api/v1/system/stop", 401, req(base, "POST", "/api/v1/system/stop", body={})[0], severity_if_fail="critical")
    probe("auth", "anonymous /docs", {401, 404}, req(base, "GET", "/docs")[0])

    # --- sessions + CSRF
    use(3)
    st, _, hd = req(base, "POST", "/api/v1/session", body={"token": tok}, headers=origin)
    cookie = (hd.get("set-cookie") or "").split(";")[0]
    probe("session", "open session with the token", 200, st, f"cookie set: {bool(cookie)}")
    probe("session", "cookie GET", 200, req(base, "GET", "/api/projects", headers={"Cookie": cookie})[0])
    probe("csrf", "cookie POST without X-NEXUS-CSRF", 403, req(base, "POST", "/api/series", body={"concept": "x"}, headers={"Cookie": cookie, **origin})[0],
          severity_if_fail="critical")
    probe("csrf", "cookie POST with header but foreign Origin", 403,
          req(base, "POST", "/api/series", body={"concept": "x"}, headers={"Cookie": cookie, "X-NEXUS-CSRF": "1", "Origin": "https://evil.example"})[0],
          severity_if_fail="critical")
    name, val = cookie.split("=", 1) if "=" in cookie else ("nexus_session", "")
    probe("session", "tampered session cookie", 401, req(base, "GET", "/api/projects", headers={"Cookie": f"{name}={val[:-4]}AAAA"})[0])
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from nexus.cloud import security
        old = security.make_session_value(tok, time.time() - security.SESSION_TTL_S - 120)
        probe("session", "expired session (TTL + 2 min)", 401, req(base, "GET", "/api/projects", headers={"Cookie": f"{name}={old}"})[0])
    except Exception as e:  # noqa: BLE001
        probe("session", "expired session", 401, None, f"could not build: {e}")
    st_lo = req(base, "POST", "/api/v1/studio/logout", headers={"Cookie": cookie, "X-NEXUS-CSRF": "1", **origin})[0]
    probe("session", "copied session cookie after logout", 401, req(base, "GET", "/api/projects", headers={"Cookie": cookie})[0],
          f"logout returned {st_lo}; a stateless signed cookie stays valid until its TTL unless revoked server-side", severity_if_fail="minor")
    probe("session", "legacy raw-token cookie", 401, req(base, "GET", "/api/projects", headers={"Cookie": f"{name}={tok}"})[0],
          "a cookie holding the raw token never expires", severity_if_fail="minor")

    # --- uploads
    use(4)
    raw, ct = multipart("file", "evil.exe", b"MZ")
    probe("upload", "executable extension refused", 415, req(base, "POST", "/api/upload", raw=raw, headers={**bearer, "Content-Type": ct})[0])
    raw, ct = multipart("file", "../../../escape.png", png_bytes(), "image/png")
    st, body, _ = req(base, "POST", "/api/upload", raw=raw, headers={**bearer, "Content-Type": ct})
    probe("upload", "traversal filename is reduced to its base name", 200, st)
    pid = json.loads(body).get("project") if st == 200 else None
    escaped = list(Path(a.home).parent.glob("escape.png")) + list(Path(a.home).glob("escape.png"))
    probe("upload", "nothing written outside the project", 0, len(escaped), str(escaped))
    raw, ct = multipart("file", "broken.mp4", b"\x00\x00\x00\x18ftypmp42" + os.urandom(4096), "video/mp4")
    st, body, _ = req(base, "POST", "/api/upload", raw=raw, headers={**bearer, "Content-Type": ct})
    probe("upload", "corrupt video accepted for analysis without a server error", {200, 400, 415, 422}, st, body[:160].decode(errors="replace"))
    corrupt_job = json.loads(body).get("job") if st == 200 else None

    # --- path traversal on the project file route
    use(5)
    if pid:
        pdir = Path(a.home) / "projects" / pid
        media = [p for p in pdir.rglob("*.png")]
        rel = str(media[0].relative_to(pdir)) if media else None
        if rel:
            probe("paths", "valid project file", 200, req(base, "GET", f"/api/projects/{pid}/file?path={urllib.parse.quote(rel)}", headers=bearer)[0])
        for bad in ("../../token", "/etc/passwd", "..%2F..%2Fetc%2Fpasswd", "media/../../../etc/passwd", "....//....//etc/passwd"):
            probe("paths", f"project file path {bad!r}", {400, 404, 422},
                  req(base, "GET", f"/api/projects/{pid}/file?path={bad if '%' in bad else urllib.parse.quote(bad)}", headers=bearer)[0],
                  severity_if_fail="critical")
        try:
            link = pdir / "media" / "qa_link.txt"
            link.parent.mkdir(parents=True, exist_ok=True)
            secret = Path(a.home).parent / "outside_secret.txt"
            secret.write_text("outside")
            link.symlink_to(secret)
            probe("paths", "symlink inside the project pointing outside", {400, 404},
                  req(base, "GET", f"/api/projects/{pid}/file?path=media/qa_link.txt", headers=bearer)[0], severity_if_fail="critical")
        except OSError as e:
            probe("paths", "symlink inside the project pointing outside", {400, 404}, None, f"could not create symlink: {e}")
    probe("paths", "project id traversal", {400, 404, 422}, req(base, "GET", "/api/projects/..%2F..%2Fetc", headers=bearer)[0])
    probe("paths", "unknown project id", 404, req(base, "GET", "/api/projects/prj_doesnotexist", headers=bearer)[0], severity_if_fail="minor")

    # --- malformed input
    use(6)
    probe("input", "malformed JSON body", {400, 422}, req(base, "POST", "/api/series", raw=b"{not json", headers={**bearer, "Content-Type": "application/json"})[0])
    probe("input", "JSON body over the 8 MiB limit", {413, "connection_closed"},
          req(base, "POST", "/api/series", raw=b'{"concept":"' + b"a" * (9 * 1024 * 1024) + b'"}', headers={**bearer, "Content-Type": "application/json"})[0])

    # --- corrupt media job outcome (worker must fail the job with a readable error, not hang or crash the server)
    if corrupt_job:
        status = None
        for _ in range(90):
            s, b, _ = req(base, "GET", "/api/jobs", headers=bearer)
            js = json.loads(b) if s == 200 else {}
            rows = js.get("jobs", js) if isinstance(js, dict) else js
            row = next((j for j in rows or [] if j.get("id") == corrupt_job), None)
            status = row and row.get("status")
            if status in ("failed", "succeeded", "done", "interrupted"):
                break
            time.sleep(2)
        probe("recovery", "corrupt video analysis ends (not stuck)", {"failed", "succeeded", "done"}, status,
              (row or {}).get("error", "")[:200] if row else "job not found")
        err = str((row or {}).get("error") or "")
        probe("ux", "failed job error is a plain message (no traceback, no server paths)", False, "Traceback" in err or a.home in err,
              "the API returns the Python traceback and absolute paths to the client", severity_if_fail="minor")
        probe("recovery", "an undecodable file is not retried", 0, int((row or {}).get("retries") or 0),
              "deterministic input errors are retried like transient ones", severity_if_fail="minor")
    probe("server", "server still healthy after the probes", 200, req(base, "GET", "/api/projects", headers=bearer)[0], severity_if_fail="critical")

    # --- brute force lockout
    use(7)
    codes = [req(base, "GET", "/api/projects", headers={"Authorization": "Bearer wrong"})[0] for _ in range(12)]
    probe("auth", "repeated wrong tokens are locked out", 429, codes[-1], f"codes {codes}")
    # --- owner lock-out by anonymous traffic from the same address (a desktop install: everything is 127.0.0.1)
    use(8)
    anon = [req(base, "GET", "/api/projects")[0] for _ in range(11)]
    probe("auth", "owner's correct token after 11 anonymous requests from the same address", 200,
          req(base, "GET", "/api/projects", headers=bearer)[0],
          f"anonymous codes {anon}: plain unauthenticated requests (an old browser tab polling, a page load before login) count "
          "as failed logins, and the lockout then refuses the owner's valid token for the window", severity_if_fail="major")

    out = {"schema": "nexus.BreakProbe/1", "base": base, "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "passed": sum(r["ok"] for r in RESULTS), "failed": sum(not r["ok"] for r in RESULTS), "results": RESULTS}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"{out['passed']} passed, {out['failed']} failed -> {a.out}")


if __name__ == "__main__":
    main()

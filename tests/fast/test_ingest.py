"""Media intake: SSRF defences, platform refusal, rights, size limits, magic-byte typing, dedupe, storage + provenance.
All network traffic goes to a loopback fixture server reached through an injected resolver (no internet)."""
import io
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from nexus.cloud import ingest
from nexus.cloud.ingest import Policy, Rejected, ip_problem, validate_url
from nexus.cloud.storage import artifacts
from nexus.cloud.storage.local import LocalProvider
from nexus.contracts import MediaIngestionJob, MediaSource

HOST = "media.nexus-fixture.org"


def _png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (32, 24), (200, 40, 40)).save(b, "PNG")
    return b.getvalue()


PNG = _png()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    port = 0

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/octet-stream", headers=None, length=True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if length:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p == "/clip.png":
            return self._send(200, PNG, "image/png")
        if p == "/disguised.mp4":
            return self._send(200, b"<!doctype html><html><body>not a video</body></html>", "video/mp4")
        if p == "/r1":
            return self._send(302, headers={"Location": "/r2"})
        if p == "/r2":
            return self._send(301, headers={"Location": f"http://{HOST}:{self.port}/clip.png"})
        if p.startswith("/loop/"):
            n = int(p.rsplit("/", 1)[1])
            return self._send(302, headers={"Location": f"/loop/{n + 1}"})
        if p == "/to-private":
            return self._send(302, headers={"Location": f"http://internal.nexus-fixture.org:{self.port}/clip.png"})
        if p == "/to-metadata":
            return self._send(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        if p == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(50 * 2 ** 20))
            self.send_header("Connection", "close")
            self.end_headers()
            return
        if p == "/stream":  # no Content-Length: the limit must be enforced while reading
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(PNG + b"\0" * (3 * 2 ** 20))
            return
        if p == "/clip.mp4" and getattr(self.server, "mp4", None):
            return self._send(200, self.server.mp4, "video/mp4")
        return self._send(404, b"nope")


@pytest.fixture()
def server(home):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    H.port = srv.server_address[1]
    srv.mp4 = None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def policy(port, extra=None):
    table = {HOST: ["127.0.0.1"], "internal.nexus-fixture.org": ["10.0.0.7"], **(extra or {})}
    return Policy(resolver=lambda h, p: table.get(h, ["93.184.215.14"]), allowed_ports={port},
                  allow_ips={"127.0.0.1"}, host_check=None, use_proxy=False)


def job(url, rights="public_domain", **kw):
    return MediaIngestionJob(source=MediaSource(kind="http_url", ref=url, rights=rights, licence=kw.pop("licence", None)), **kw)


# ---------------------------------------------------------------- address policy
@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                                "0.0.0.0", "224.0.0.1", "240.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fc00::1",
                                "fd00:ec2::254", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "2002:7f00:1::1",
                                "64:ff9b::a00:1", "ff02::1", "100.100.100.200", "198.18.0.1"])
def test_unsafe_addresses(ip):
    assert ip_problem(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.215.14", "2606:4700:4700::1111"])
def test_public_addresses(ip):
    assert ip_problem(ip) is None


BAD_URLS = ["ftp://example.org/a.mp4", "file:///etc/passwd", "gopher://x.org/", "javascript:alert(1)",
            "http://localhost/a.mp4", "http://127.0.0.1/a.mp4", "http://2130706433/", "http://0x7f000001/",
            "http://0177.0.0.1/", "http://127.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:a9fe:a9fe]/", "http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/",
            "http://service.internal/x", "http://user:pw@media.example.org/a.mp4", "http://media.example.org:8080/a.mp4",
            "http://intranet/a.mp4", "http://internal.nexus-fixture.org/a.mp4"]


@pytest.mark.parametrize("url", BAD_URLS)
def test_ssrf_rejections(url):
    with pytest.raises(Rejected):
        validate_url(url, Policy(resolver=lambda h, p: {"internal.nexus-fixture.org": ["10.0.0.7"]}.get(h, ["8.8.8.8"]),
                                 host_check=None))


def test_every_dns_answer_must_be_public():
    with pytest.raises(Rejected, match="10.0.0.1"):
        validate_url("https://cdn.nexus-fixture.org/v.mp4",
                     Policy(resolver=lambda h, p: ["8.8.8.8", "10.0.0.1"], host_check=None))


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=abc", "https://youtu.be/abc", "https://m.youtube.com/x",
                                 "https://www.tiktok.com/@a/video/1", "https://www.instagram.com/reel/x/",
                                 "https://www.facebook.com/watch/?v=1", "https://x.com/a/status/1",
                                 "https://twitter.com/a/status/1", "https://vimeo.com/123", "https://player.vimeo.com/video/1",
                                 "https://rr1---sn-x.googlevideo.com/videoplayback"])
def test_platform_pages_refused(url, home):
    with pytest.raises(Rejected, match="creator's own export"):
        validate_url(url, Policy(host_check=None))
    j = ingest.run(job(url))
    assert j.status == "rejected" and "Upload the file you own" in j.reason


def test_costguard_egress_applies(home):
    with pytest.raises(Rejected, match="zero-spend"):
        validate_url("https://media.nexus-fixture.org/a.mp4", Policy(resolver=lambda h, p: ["8.8.8.8"]))


# ---------------------------------------------------------------- end to end over HTTP
def test_ingest_stores_with_provenance_and_dedupes(server):
    port = server.server_address[1]
    j = ingest.run(job(f"http://{HOST}:{port}/r1", project_id="prj_x"), policy(port))
    assert j.status == "stored", j.reason
    m = j.artifact
    assert m.mime == "image/png" and m.kind == "source" and m.size == len(PNG)
    assert m.provenance["rights"] == "public_domain" and m.provenance["redirects"] == [
        f"http://{HOST}:{port}/r1", f"http://{HOST}:{port}/r2"]
    assert m.provenance["connect_mode"] == "pinned_ip"  # system DNS cannot resolve HOST: we connected to the validated IP
    lo = m.locations[0]
    assert lo.provider == "local" and lo.path == "NEXUS/OriginalMedia/prj_x/clip.png"
    assert LocalProvider().checksum(lo.ref)["sha256"] == m.sha256
    assert ingest.get(j.ingestion_id).status == "stored"
    j2 = ingest.run(job(f"http://{HOST}:{port}/clip.png"), policy(port))
    assert j2.status == "stored" and j2.artifact.artifact_id == m.artifact_id and "duplicate" in j2.reason
    assert len(artifacts.list_artifacts()) == 1


@pytest.mark.parametrize("path,match", [("/to-private", "10.0.0.7"), ("/to-metadata", "metadata"),
                                         ("/loop/0", "redirects"), ("/big", "limit"), ("/stream", "limit"),
                                         ("/disguised.mp4", "magic bytes")])
def test_ingest_rejections_over_http(server, path, match):
    port = server.server_address[1]
    j = ingest.run(job(f"http://{HOST}:{port}{path}", max_bytes=2 ** 20), policy(port))
    assert j.status == "rejected" and match in j.reason, j.reason


def test_rights_required(server):
    port = server.server_address[1]
    j = ingest.run(job(f"http://{HOST}:{port}/clip.png", rights="unknown"), policy(port))
    assert j.status == "rejected" and j.reason == "declare rights or upload a file you own"
    j = ingest.run(job(f"http://{HOST}:{port}/clip.png", rights="licensed"), policy(port))
    assert j.status == "rejected" and "licence" in j.reason
    j = ingest.run(job(f"http://{HOST}:{port}/clip.png", rights="licensed", licence="CC-BY-4.0"), policy(port))
    assert j.status == "stored" and j.artifact.provenance["licence"] == "CC-BY-4.0"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_video_checked_with_ffprobe(server, tmp_path):
    out = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10",
                    "-pix_fmt", "yuv420p", str(out)], check=True)
    server.mp4 = out.read_bytes()
    port = server.server_address[1]
    j = ingest.run(job(f"http://{HOST}:{port}/clip.mp4"), policy(port))
    assert j.status == "stored", j.reason
    assert j.artifact.mime == "video/mp4" and j.artifact.provenance["ffprobe"]["streams"][0]["codec_type"] == "video"


def test_upload_and_artifact_sources(home):
    up = ingest.upload_dir() / "abc__photo.png"
    up.write_bytes(PNG)
    j = ingest.run(MediaIngestionJob(source=MediaSource(kind="upload", ref=str(up), rights="owned")))
    assert j.status == "stored" and j.artifact.name == "photo.png" and not up.exists()
    ref = ingest.run(MediaIngestionJob(source=MediaSource(kind="artifact", ref=j.artifact.artifact_id, rights="owned")))
    assert ref.status == "stored" and ref.artifact.artifact_id == j.artifact.artifact_id
    outside = home / "elsewhere.png"
    outside.write_bytes(PNG)
    bad = ingest.run(MediaIngestionJob(source=MediaSource(kind="upload", ref=str(outside), rights="owned")))
    assert bad.status == "rejected"


def test_drive_file_source(home, tmp_path):
    from nexus import vault
    from nexus.cloud.storage import oauth
    from nexus.cloud.storage.fake_drive import FakeGoogle
    from nexus.cloud.storage.gdrive import GoogleDriveProvider
    g = FakeGoogle()
    client = {"client_id": "t.apps.googleusercontent.com", "client_secret": "s-EXAMPLE"}
    g.add_client(client["client_id"], client["client_secret"])
    oauth.set_endpoints(oauth.fake_endpoints(g.start()))
    oauth.reset_cache()
    try:
        vault.put(oauth.CLIENT_SECRET_NAME, json.dumps(client))
        s = oauth.device_start("drive_b")
        g.approve(s["user_code"], "owner@example.com")
        d = json.loads(vault.get("gdevice_drive_b"))
        d["next_poll"] = 0
        vault.put("gdevice_drive_b", json.dumps(d))
        assert oauth.device_poll("drive_b")["status"] == "connected"
        src = tmp_path / "in.png"
        src.write_bytes(PNG)
        f = GoogleDriveProvider("drive_b").upload(src, "NEXUS/Inbox/in.png", mime="image/png")
        j = ingest.run(MediaIngestionJob(source=MediaSource(kind="drive_file", ref=f.ref, connection_id="drive_b",
                                                            rights="owned")))
        assert j.status == "stored" and j.artifact.provenance["drive_file_id"] == f.ref
        assert j.artifact.locations[0].connection_id == "local"
        miss = ingest.run(MediaIngestionJob(source=MediaSource(kind="drive_file", ref="nope", connection_id="drive_b",
                                                               rights="owned")))
        assert miss.status == "rejected"
    finally:
        g.stop()
        oauth.set_endpoints(None)

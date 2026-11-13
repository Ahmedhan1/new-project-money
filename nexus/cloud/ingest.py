"""Safe media intake (contracts v1 MediaIngestionJob -> ArtifactManifest).

Sources: direct HTTP(S) media URLs, Drive files of a connected account, local uploads, existing artifact references.
Rules enforced here:
  * Rights are required: MediaSource.rights == "unknown" is rejected ("declare rights or upload a file you own");
    "licensed" also needs the licence name. Provenance (who declared what, from where, when) is stored in the manifest.
  * Platform PAGE URLs (YouTube, TikTok, Instagram, Facebook, X/Twitter, Vimeo, ...) are refused: NEXUS does not scrape
    them or get around their download controls. Upload the file or use the creator's own export instead.
  * SSRF: only http/https; hostnames that name internal services are refused; non-canonical numeric hosts (decimal,
    octal, hex, short forms like 127.1) are refused; every resolved address must be public (no private, loopback,
    link-local incl. 169.254.169.254, CGNAT, multicast, reserved, unspecified, IPv4-mapped/6to4/Teredo/NAT64 forms of
    those); non-default ports only when allowlisted; redirects (max 5) are re-validated hop by hop, and in direct mode
    the socket connects to the validated IP itself (TLS still verifies the hostname) so a DNS answer cannot change
    between check and use. Behind an egress proxy (HTTPS_PROXY) the proxy performs the connection; the addresses are
    validated immediately before and the result records connect_mode="proxy".
  * Size: Content-Length is checked before reading and the byte count is enforced while streaming.
  * Type: decided by magic bytes, not by the Content-Type header; video/audio are confirmed with ffprobe.
  * Integrity: SHA-256; an identical file that is already stored is reported as a duplicate, not stored twice.
  * Zero-spend egress: the URL host must also pass costguard.check_host (declared in registry/components.yaml).
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

from .. import config, costguard, db
from ..contracts import MediaIngestionJob
from .events import emit

MAX_REDIRECTS = 5
RIGHTS_MSG = "declare rights or upload a file you own"
PLATFORM_DOMAINS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com", "ytimg.com", "tiktok.com", "tiktokcdn.com",
    "tiktokv.com", "instagram.com", "cdninstagram.com", "facebook.com", "fb.com", "fb.watch", "fbcdn.net", "x.com",
    "twitter.com", "t.co", "twimg.com", "vimeo.com", "vimeocdn.com", "dailymotion.com", "dai.ly", "twitch.tv",
    "reddit.com", "redd.it", "snapchat.com", "linkedin.com", "pinterest.com", "pin.it", "threads.net", "bilibili.com",
    "rumble.com", "kick.com", "soundcloud.com", "spotify.com", "music.apple.com", "netflix.com", "primevideo.com",
)
PLATFORM_MSG = ("platform page URLs are not imported: NEXUS does not scrape platforms or bypass their download controls. "
                "Upload the file you own, or use the creator's own export / download feature and upload that file")
BLOCKED_NAMES = {"localhost", "metadata", "metadata.google.internal", "metadata.goog", "instance-data",
                 "instance-data.ec2.internal", "kubernetes", "kubernetes.default", "host.docker.internal"}
BLOCKED_SUFFIXES = (".localhost", ".internal", ".local", ".localdomain", ".home.arpa", ".lan", ".intranet", ".corp",
                    ".svc", ".cluster.local", ".test", ".invalid", ".example")
NAT64 = ipaddress.ip_network("64:ff9b::/96")


class Rejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- address policy
def ip_problem(ip: str | ipaddress._BaseAddress) -> str | None:
    """None when the address is a public unicast address; otherwise why it is refused."""
    a = ipaddress.ip_address(str(ip).split("%", 1)[0]) if not isinstance(ip, ipaddress._BaseAddress) else ip
    if a.version == 6:
        for embedded in (a.ipv4_mapped, a.sixtofour, a.teredo[1] if a.teredo else None,
                         ipaddress.IPv4Address(int(a) & 0xFFFFFFFF) if a in NAT64 else None):
            if embedded is not None:
                why = ip_problem(embedded)
                if why:
                    return f"embeds {embedded}: {why}"
    if str(a) in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200"):
        return "cloud metadata address"
    for attr, why in (("is_loopback", "loopback"), ("is_private", "private network"), ("is_link_local", "link-local"),
                      ("is_multicast", "multicast"), ("is_reserved", "reserved"), ("is_unspecified", "unspecified"),
                      ("is_site_local", "site-local")):
        if getattr(a, attr, False):
            return why
    if not a.is_global:
        return "not a public address"
    return None


def _numeric_trick(host: str) -> bool:
    """True for hosts that libc would parse as an IPv4 address but that are not canonical dotted-quad
    (2130706433, 0x7f.1, 0177.0.0.1, 127.1 ...)."""
    if not re.fullmatch(r"[0-9a-fx.]+", host) or not re.search(r"\d", host):
        return False
    try:
        ipaddress.IPv4Address(host)
        return False  # canonical dotted quad; checked as an IP
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return bool(re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*", host))


def platform_of(host: str) -> str | None:
    h = host.lower().rstrip(".")
    for d in PLATFORM_DOMAINS:
        if h == d or h.endswith("." + d):
            return d
    return None


Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


@dataclass
class Target:
    url: str
    scheme: str
    host: str
    port: int
    ips: list[str]


@dataclass
class Policy:
    resolver: Resolver = system_resolver
    allowed_ports: set[int] = field(default_factory=set)
    allow_ips: set[str] = field(default_factory=set)  # tests only (the fixture server is on loopback)
    host_check: Callable[[str], object] | None = costguard.check_host
    use_proxy: bool | None = None  # None = follow HTTPS_PROXY / NO_PROXY


def validate_url(url: str, policy: Policy) -> Target:
    if len(url) > 4096:
        raise Rejected("URL too long")
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https"):
        raise Rejected(f"only http and https URLs are accepted (got {u.scheme or 'none'!r})")
    if u.username or u.password:
        raise Rejected("URLs with embedded credentials are refused")
    host = (u.hostname or "").lower().rstrip(".")
    if not host:
        raise Rejected("URL has no host")
    p = platform_of(host)
    if p:
        raise Rejected(f"{p}: {PLATFORM_MSG}")
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise Rejected("invalid port") from None
    if port not in (80, 443) and port not in policy.allowed_ports:
        raise Rejected(f"port {port} is not allowed (only 80/443 unless allowlisted)")
    literal = None
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if _numeric_trick(host):
            raise Rejected(f"non-canonical numeric host {host!r} refused") from None
        if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES) or "." not in host:
            raise Rejected(f"host {host!r} names an internal service")
    if literal is not None:
        ips = [str(literal)]
    else:
        try:
            ips = policy.resolver(host, port)
        except (socket.gaierror, OSError) as e:
            raise Rejected(f"cannot resolve {host}: {e}") from None
    if not ips:
        raise Rejected(f"{host} did not resolve")
    for ip in ips:  # every answer must be safe, not just the first
        why = None if ip in policy.allow_ips else ip_problem(ip)
        if why:
            raise Rejected(f"{host} resolves to {ip} ({why})")
    if policy.host_check is not None:
        try:
            policy.host_check(f"{u.scheme}://{host}/")
        except costguard.CostGuardError as e:
            raise Rejected(f"host not allowed by the zero-spend egress list: {e}") from None
    return Target(url=url, scheme=u.scheme, host=host, port=port, ips=ips)


# ---------------------------------------------------------------- pinned-IP HTTP
class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, **kw):
        super().__init__(host, port, **kw)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, context, **kw):
        super().__init__(host, port, context=context, **kw)
        self._ip, self._ctx = ip, context

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)  # cert checked against the HOSTNAME


def _proxy_for(t: Target, policy: Policy) -> tuple[str, int] | None:
    if policy.use_proxy is False:
        return None
    proxies = urllib.request.getproxies()
    p = proxies.get(t.scheme)
    if not p or (policy.use_proxy is None and urllib.request.proxy_bypass(t.host)):
        return None
    pu = urlparse(p)
    return pu.hostname, pu.port or 8080


@dataclass
class FetchResult:
    final_url: str
    redirects: list[str]
    content_type: str
    size: int
    ip: str
    connect_mode: str
    status: int


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for env in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(env) and Path(os.environ[env]).exists():
            ctx.load_verify_locations(os.environ[env])
            break
    return ctx


def fetch(url: str, dest: Path, max_bytes: int, policy: Policy | None = None, timeout: float = 60) -> FetchResult:
    policy = policy or Policy()
    hops: list[str] = []
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        t = validate_url(cur, policy)  # re-validated on EVERY hop
        u = urlparse(cur)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        proxy = _proxy_for(t, policy)
        headers = {"User-Agent": config.USER_AGENT, "Accept": "video/*,audio/*,image/*;q=0.9,*/*;q=0.1",
                   "Accept-Encoding": "identity", "Connection": "close"}
        if proxy:
            mode = "proxy"
            if t.scheme == "https":
                c = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout, context=_ssl_ctx())
                c.set_tunnel(t.host, t.port)
            else:
                c = http.client.HTTPConnection(proxy[0], proxy[1], timeout=timeout)
                path = cur
        else:
            mode = "pinned_ip"
            c = (_PinnedHTTPS(t.host, t.port, t.ips[0], _ssl_ctx(), timeout=timeout) if t.scheme == "https"
                 else _PinnedHTTP(t.host, t.port, t.ips[0], timeout=timeout))
        try:
            c.request("GET", path, headers={**headers, "Host": t.host if t.port in (80, 443) else f"{t.host}:{t.port}"})
            r = c.getresponse()
            if r.status in (301, 302, 303, 307, 308):
                loc = r.getheader("Location")
                r.read(65536)
                if not loc:
                    raise Rejected(f"redirect without Location from {t.host}")
                hops.append(cur)
                cur = urljoin(cur, loc)
                continue
            if r.status != 200:
                raise Rejected(f"{t.host} answered HTTP {r.status}")
            cl = r.getheader("Content-Length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                raise Rejected(f"file is {int(cl)} bytes; the limit is {max_bytes}")
            n = 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > max_bytes:
                        raise Rejected(f"download exceeded the limit of {max_bytes} bytes")
                    f.write(chunk)
            if cl and cl.isdigit() and n != int(cl):
                raise Rejected(f"truncated download ({n} of {cl} bytes)")
            return FetchResult(final_url=cur, redirects=hops, content_type=r.getheader("Content-Type") or "", size=n,
                               ip=t.ips[0], connect_mode=mode, status=r.status)
        except (OSError, http.client.HTTPException) as e:
            raise Rejected(f"download from {t.host} failed: {type(e).__name__}: {e}") from None
        finally:
            c.close()
    raise Rejected(f"more than {MAX_REDIRECTS} redirects")


# ---------------------------------------------------------------- type detection
def sniff(path: Path) -> str:
    with open(path, "rb") as f:
        h = f.read(4096)
    if len(h) >= 12 and h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"mif1"):
            return "image/heic"
        return "video/mp4"
    if h.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm" if b"webm" in h[:64] else "video/x-matroska"
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return "audio/wav"
    if h[:4] == b"RIFF" and h[8:12] == b"AVI ":
        return "video/x-msvideo"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "image/webp"
    if h.startswith(b"OggS"):
        return "video/ogg" if b"theora" in h[:200] else "audio/ogg"
    if h.startswith(b"fLaC"):
        return "audio/flac"
    if h.startswith(b"ID3") or (len(h) > 1 and h[0] == 0xFF and h[1] & 0xE0 == 0xE0 and h[1] & 0x06):
        return "audio/mpeg"
    if len(h) > 1 and h[0] == 0xFF and h[1] & 0xF6 == 0xF0:
        return "audio/aac"
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(h) >= 377 and h[0] == 0x47 and h[188] == 0x47 and h[376] == 0x47:
        return "video/mp2t"
    if h.lstrip()[:15].lower().startswith((b"<!doctype", b"<html", b"<?xml", b"{", b"[")):
        return "text/html" if b"html" in h[:200].lower() else "text/plain"
    return "application/octet-stream"


def ffprobe(path: Path) -> dict:
    exe = shutil.which("ffprobe")
    if not exe:
        return {"available": False}
    r = subprocess.run([exe, "-v", "error", "-show_entries", "format=format_name,duration,bit_rate:stream=codec_type,codec_name,width,height",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return {"available": True, "ok": False, "error": r.stderr.strip()[:300]}
    d = json.loads(r.stdout or "{}")
    streams = d.get("streams", [])
    return {"available": True, "ok": True, "format": d.get("format", {}).get("format_name"),
            "duration": float(d.get("format", {}).get("duration") or 0) or None,
            "streams": [{k: s.get(k) for k in ("codec_type", "codec_name", "width", "height") if s.get(k) is not None}
                        for s in streams]}


EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv",
       "audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/ogg": ".ogg", "audio/flac": ".flac",
       "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "video/mp2t": ".ts",
       "video/x-msvideo": ".avi", "audio/aac": ".aac", "video/ogg": ".ogv"}


def _safe_name(name: str, mime: str, fallback: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", unquote(name or "")).strip("._")[:120] or fallback
    ext = EXT.get(mime)
    if ext and not base.lower().endswith(ext):
        base = base.rsplit(".", 1)[0] + ext if "." in base else base + ext
    return base


# ---------------------------------------------------------------- jobs
def _save(job: MediaIngestionJob) -> None:
    db.put_doc("ingest", json.loads(job.model_dump_json()), job.ingestion_id)


def get(ingestion_id: str) -> MediaIngestionJob | None:
    d = db.get_doc("ingest", ingestion_id)
    if not d:
        return None
    d.pop("id", None)
    return MediaIngestionJob.model_validate(d)


def upload_dir() -> Path:
    d = config.HOME / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _reject(job: MediaIngestionJob, reason: str) -> MediaIngestionJob:
    job.status, job.reason = "rejected", reason
    _save(job)
    emit("ingest.rejected", reason, ingestion_id=job.ingestion_id, kind=job.source.kind)
    return job


def run(job: MediaIngestionJob, policy: Policy | None = None) -> MediaIngestionJob:
    """Validate, fetch, verify and store one source. Never raises for a refused source: returns status 'rejected'."""
    from .storage import artifacts
    src = job.source
    _save(job)
    if src.rights == "unknown":
        return _reject(job, RIGHTS_MSG)
    if src.rights == "licensed" and not src.licence:
        return _reject(job, "licensed media needs the licence name (MediaSource.licence)")
    stage = config.CACHE / "ingest" / job.ingestion_id
    stage.mkdir(parents=True, exist_ok=True)
    staged = stage / "original"
    prov: dict = {"source_kind": src.kind, "source_ref": src.ref if src.kind != "upload" else Path(src.ref).name,
                  "rights": src.rights, "licence": src.licence, "attribution": src.attribution,
                  "declared_by": src.declared_by, "ingestion_id": job.ingestion_id, "ingested_at": time.time()}
    name = ""
    try:
        job.status = "validating"
        _save(job)
        if src.kind == "artifact":
            m = artifacts.get(src.ref)
            if m is None:
                return _reject(job, f"unknown artifact {src.ref}")
            job.status, job.artifact, job.reason = "stored", m, "existing artifact referenced"
            _save(job)
            return job
        if src.kind == "provider_api":
            return _reject(job, "provider_api sources are not supported by this intake; upload the file")
        if src.kind == "http_url":
            job.status = "downloading"
            _save(job)
            r = fetch(src.ref, staged, job.max_bytes, policy)
            prov.update(url=src.ref, final_url=r.final_url, redirects=r.redirects, http_content_type=r.content_type,
                        connect_mode=r.connect_mode, fetched_at=time.time())
            name = urlparse(r.final_url).path.rsplit("/", 1)[-1]
        elif src.kind == "drive_file":
            from .storage import connections
            from .storage.gdrive import GoogleDriveProvider
            if src.connection_id not in connections.DRIVE_IDS:
                return _reject(job, "drive_file needs connection_id drive_a, drive_b or drive_c")
            p = GoogleDriveProvider(src.connection_id)
            f = p.stat(src.ref)
            if f is None:
                return _reject(job, f"Drive file {src.ref} not visible to {src.connection_id} (drive.file scope: only "
                                    "files NEXUS created or the owner opened with NEXUS)")
            if f.size is not None and f.size > job.max_bytes:
                return _reject(job, f"file is {f.size} bytes; the limit is {job.max_bytes}")
            job.status = "downloading"
            _save(job)
            p.download(src.ref, staged)
            name = f.name
            prov.update(drive_connection=src.connection_id, drive_file_id=src.ref)
        elif src.kind == "upload":
            up = Path(src.ref).resolve()
            if upload_dir().resolve() not in up.parents or not up.is_file():
                return _reject(job, "upload must be a file received through POST /api/v1/storage/ingest (uploads dir)")
            if up.stat().st_size > job.max_bytes:
                return _reject(job, f"file is {up.stat().st_size} bytes; the limit is {job.max_bytes}")
            shutil.move(str(up), staged)
            name = up.name.split("__", 1)[-1]
        job.status = "verifying"
        _save(job)
        mime = sniff(staged)
        if not any(mime.startswith(a) for a in job.allowed_mime):
            return _reject(job, f"content is {mime} by its magic bytes; allowed: {job.allowed_mime}")
        if mime.startswith(("video/", "audio/")):
            probe = ffprobe(staged)
            prov["ffprobe"] = probe
            if probe.get("available") and not probe.get("ok"):
                return _reject(job, f"ffprobe cannot read the file: {probe.get('error')}")
            kinds = {s.get("codec_type") for s in probe.get("streams", [])}
            if probe.get("ok") and not kinds & {"video", "audio"}:
                return _reject(job, "no audio or video stream found")
        from .storage.base import sha256_file
        sha = sha256_file(staged)
        prov.update(sha256=sha, sniffed_mime=mime)
        dup = artifacts.find_by_sha(sha)
        if dup is not None:
            job.status, job.artifact, job.reason = "stored", dup, f"duplicate of {dup.artifact_id} (same sha256); not stored twice"
            _save(job)
            emit("ingest.stored", job.reason, ingestion_id=job.ingestion_id, artifact_id=dup.artifact_id, duplicate=True)
            return job
        fname = _safe_name(name, mime, job.ingestion_id)
        m = artifacts.store_file(staged, kind="source", name=fname, connection_id=job.destination, mime=mime,
                                 project_id=job.project_id, provenance=prov)
        job.status, job.artifact, job.reason = "stored", m, None
        _save(job)
        emit("ingest.stored", f"{fname} stored on {m.locations[-1].connection_id} ({m.size} bytes)",
             ingestion_id=job.ingestion_id, artifact_id=m.artifact_id)
        return job
    except Rejected as e:
        return _reject(job, e.reason)
    except Exception as e:  # technical failure (storage, network): failed, not rejected
        from ..vault import redact
        job.status, job.reason = "failed", redact(f"{type(e).__name__}: {e}")[:500]
        _save(job)
        emit("storage.failed", job.reason, ingestion_id=job.ingestion_id)
        return job
    finally:
        shutil.rmtree(stage, ignore_errors=True)

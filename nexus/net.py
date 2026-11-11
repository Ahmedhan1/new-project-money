"""The only place NEXUS makes outbound HTTP calls. Every call passes the Cost Guard."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, costguard


class RemoteUnavailable(RuntimeError):
    pass


def _open(url: str, timeout: int = 30, retries: int = 3, max_wait: float = 10.0):
    costguard.check_host(url)
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT, "Accept": "*/*"})
    delay = 2.0
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            # 429 = the provider's rate limit. We back off; we never try to evade it.
            if e.code in (429, 503) and attempt < retries - 1:
                wait = float(e.headers.get("Retry-After") or delay)
                if wait > max_wait:  # the provider asks for a long pause: give up now and let the caller fall back
                    raise RemoteUnavailable(f"{url.split('?')[0]} -> rate limited, retry after {wait:.0f}s") from e
                time.sleep(wait)
                delay *= 2
                continue
            raise RemoteUnavailable(f"{url.split('?')[0]} -> HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise RemoteUnavailable(f"{url.split('?')[0]} -> {e}") from e
    raise RemoteUnavailable(url)


def get_json(url: str, params: dict | None = None, **kw) -> dict:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    with _open(url, **kw) as r:
        return json.loads(r.read().decode("utf-8"))


def download(url: str, dest: Path, **kw) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _open(url, **kw) as r:
        dest.write_bytes(r.read())
    return dest

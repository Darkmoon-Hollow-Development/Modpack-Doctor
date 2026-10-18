"""Tiny, defensive HTTP layer (standard library only).

Downloads are restricted to an allow-list of hosts (including across redirects), verified
against hashes where available and size-capped, because modpack files come from untrusted sources.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

USER_AGENT = "ModpackDoctor/0.1 (open-source desktop tool for checking modpack compatibility)"
DEFAULT_MAX_DOWNLOAD = 512 * 1024 * 1024
TIMEOUT = 30

MODRINTH_HOSTS = {"cdn.modrinth.com", "api.modrinth.com"}
MRPACK_SPEC_HOSTS = {"cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com"}
REDIRECT_EXTRA_HOSTS = {"objects.githubusercontent.com", "release-assets.githubusercontent.com"}
CURSEFORGE_HOSTS = {"edge.forgecdn.net", "mediafilez.forgecdn.net", "media.forgecdn.net"}

# Tests flip this to talk to a local server; never enabled in normal use.
ALLOW_LOCAL_HTTP = False


class NetError(Exception):
    pass


def _check_url(url: str, allowed_hosts: Optional[Iterable[str]]) -> None:
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    local_ok = ALLOW_LOCAL_HTTP and host in ("127.0.0.1", "localhost")
    if p.scheme != "https" and not (local_ok and p.scheme == "http"):
        raise NetError(f"Refusing non-HTTPS URL: {url}")
    if allowed_hosts is not None and host not in {h.lower() for h in allowed_hosts}:
        raise NetError(f"Host not allowed: {host}")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts):
        self.allowed = set(h.lower() for h in allowed_hosts) | REDIRECT_EXTRA_HOSTS if allowed_hosts is not None else None

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl, self.allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(allowed_hosts):
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx), _GuardedRedirect(allowed_hosts))


def http_json(url: str, method: str = "GET", body=None, headers: Optional[dict] = None,
              allowed_hosts=MODRINTH_HOSTS, retries: int = 2):
    _check_url(url, allowed_hosts)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    for attempt in range(retries + 1):
        try:
            with _opener(allowed_hosts).open(req, timeout=TIMEOUT) as r:
                return json.loads(r.read(64 * 1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(min(float(e.headers.get("Retry-After", "2") or 2), 10))
                continue
            if e.code == 404:
                return None
            raise NetError(f"HTTP {e.code} from {urllib.parse.urlparse(url).hostname}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            raise NetError(f"Network error: {getattr(e, 'reason', e)}") from e
        except json.JSONDecodeError as e:
            raise NetError("Server returned invalid JSON") from e
    raise NetError("Network request failed")  # pragma: no cover


def download(url: str, dest: Path, *, sha1: Optional[str] = None, sha512: Optional[str] = None,
             sha256: Optional[str] = None,
             allowed_hosts=MODRINTH_HOSTS, max_bytes: int = DEFAULT_MAX_DOWNLOAD,
             headers: Optional[dict] = None) -> None:
    """Download to ``dest`` (written atomically). Raises NetError on any problem, including hash mismatch."""
    _check_url(url, allowed_hosts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    h1, h512, h256 = hashlib.sha1(), hashlib.sha512(), hashlib.sha256()
    total = 0
    try:
        with _opener(allowed_hosts).open(req, timeout=TIMEOUT) as r, open(tmp, "wb") as out:
            declared = r.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise NetError("File is larger than the allowed maximum")
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise NetError("File is larger than the allowed maximum")
                h1.update(chunk)
                h512.update(chunk)
                h256.update(chunk)
                out.write(chunk)
        if sha1 and h1.hexdigest().lower() != sha1.lower():
            raise NetError("SHA-1 mismatch: the downloaded file does not match the expected hash")
        if sha512 and h512.hexdigest().lower() != sha512.lower():
            raise NetError("SHA-512 mismatch: the downloaded file does not match the expected hash")
        if sha256 and h256.hexdigest().lower() != sha256.lower():
            raise NetError("SHA-256 mismatch: the downloaded file does not match the expected hash")
        os.replace(tmp, dest)
    except NetError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NetError(f"Download failed: {getattr(e, 'reason', e)}") from e
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def cache_dir() -> Path:
    d = Path(os.environ.get("MODPACK_DOCTOR_HOME", Path.home() / ".modpack_doctor")) / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d

"""Check GitHub Releases for a newer version of Modpack Doctor and install it.

Tk-free. Updating means downloading and running code, so it is deliberately strict:
  * it only talks to api.github.com and to github.com/<repo>/releases/download/... (redirects stay on GitHub's hosts);
  * a download is checked against the SHA-256 digest GitHub publishes for the asset whenever there is one;
  * a downloaded .pyzw must really be Modpack Doctor and must contain exactly the version the release says it is;
  * nothing is installed without the user's confirmation (the GUI asks first).

Three ways the app can be running, and what an update does in each:
  installer  the PyInstaller build installed by ModpackDoctor-Setup.exe: run the new installer silently, which
             replaces the files and starts the app again;
  zipapp     the single ModpackDoctor.pyzw: swap the file for the new one once this process has exited;
  source     run from a folder or pip: cannot replace itself, so the release page is opened instead.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import __version__, net

# "owner/name" of the GitHub repository whose Releases hold the app. Set it here once (or with the
# MODPACK_DOCTOR_REPO environment variable). Empty means updates are switched off.
REPO = "Darkmoon-Hollow-Development/Modpack-Doctor"

API_HOSTS = {"api.github.com"}
DOWNLOAD_HOSTS = {"github.com"}
INSTALLER_ASSET = "ModpackDoctor-Setup.exe"
ZIPAPP_ASSET = "ModpackDoctor.pyzw"
MAX_ASSET = 300 * 1024 * 1024
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class UpdateError(Exception):
    pass


@dataclass
class Asset:
    name: str
    url: str
    size: int = 0
    sha256: str = ""


@dataclass
class Release:
    tag: str
    version: str
    name: str = ""
    notes: str = ""
    page_url: str = ""
    assets: list = field(default_factory=list)

    def asset(self, name: str) -> Optional[Asset]:
        return next((a for a in self.assets if a.name == name), None)


def repo() -> str:
    return (os.environ.get("MODPACK_DOCTOR_REPO") or REPO).strip()


def configured() -> bool:
    return bool(_REPO_RE.match(repo()))


def parse_version(text: str) -> Optional[tuple]:
    """'v1.2.3' / 'Release-4.0' / '1.2.3-beta.1' -> (1, 2, 3) / (4,) / (1, 2, 3): the first number in the tag.
    None when there is no number to compare."""
    m = re.match(r"^\D*?(\d+(?:\.\d+)*)", text or "")
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest: str, current: str = __version__) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def _release_from_json(data: dict, repository: str) -> Release:
    tag = str(data.get("tag_name") or "")
    ver = parse_version(tag)
    if not ver:
        raise UpdateError(f"The latest release has an unreadable version: {tag!r}")
    assets = []
    prefix = f"/{repository}/releases/download/".lower()
    for a in data.get("assets") or []:
        url = str(a.get("browser_download_url") or "")
        host = urllib.parse.urlparse(url)
        if host.scheme != "https" or (host.hostname or "").lower() not in DOWNLOAD_HOSTS or \
                not host.path.lower().startswith(prefix):
            continue  # an asset that is not hosted in this repository's releases is ignored
        digest = str(a.get("digest") or "")
        assets.append(Asset(name=str(a.get("name") or ""), url=url, size=int(a.get("size") or 0),
                            sha256=digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""))
    return Release(tag=tag, version=".".join(str(x) for x in ver), name=str(data.get("name") or tag),
                   notes=str(data.get("body") or ""), page_url=str(data.get("html_url") or ""), assets=assets)


def latest_release(repository: Optional[str] = None) -> Optional[Release]:
    """The newest published (non-draft, non-prerelease) release, or None when the repository has none."""
    repository = repository or repo()
    if not _REPO_RE.match(repository or ""):
        raise UpdateError("No GitHub repository is configured for updates.")
    try:
        data = net.http_json(f"https://api.github.com/repos/{repository}/releases/latest",
                             allowed_hosts=API_HOSTS, headers={"Accept": "application/vnd.github+json"}, retries=1)
    except net.NetError as e:
        raise UpdateError(str(e)) from e
    if not isinstance(data, dict):
        return None
    return _release_from_json(data, repository)


def check(current: str = __version__, repository: Optional[str] = None) -> Optional[Release]:
    """The newer Release if there is one, else None."""
    rel = latest_release(repository)
    return rel if rel and is_newer(rel.version, current) else None


# ----------------------------------------------------------------------- how are we running?

def zipapp_path() -> Optional[Path]:
    """The .pyz/.pyzw file this app is running from, or None."""
    m = re.match(r"^(.*?\.pyzw?)(?:[\\/]|$)", str(Path(__file__).resolve()), re.I)
    if m and Path(m.group(1)).is_file():
        return Path(m.group(1))
    return None


def install_kind() -> str:
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        return "installer"
    if zipapp_path() is not None:
        return "zipapp"
    return "source"


def asset_for(kind: str, release: Release) -> Optional[Asset]:
    return release.asset({"installer": INSTALLER_ASSET, "zipapp": ZIPAPP_ASSET}.get(kind, ""))


# ----------------------------------------------------------------------- download + verify

def verify_zipapp(path: Path, expected_version: str) -> None:
    """A downloaded .pyzw must be a Modpack Doctor build of exactly the version the release names."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "__main__.py" not in names or "modpack_doctor/__init__.py" not in names:
                raise UpdateError("The downloaded file is not a Modpack Doctor build.")
            init = z.read("modpack_doctor/__init__.py").decode("utf-8", "replace")
    except zipfile.BadZipFile as e:
        raise UpdateError("The downloaded file is not a valid archive.") from e
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    if not m or parse_version(m.group(1)) != parse_version(expected_version):
        raise UpdateError(f"The downloaded build says it is version {m.group(1) if m else '?'}, "
                          f"not {expected_version}. (Was __version__ bumped before the release was built?)")


def download_update(release: Release, kind: str, dest_dir: Path,
                    allowed_hosts=DOWNLOAD_HOSTS) -> Path:
    asset = asset_for(kind, release)
    if asset is None:
        raise UpdateError(f"Release {release.tag} has no {INSTALLER_ASSET if kind == 'installer' else ZIPAPP_ASSET} file.")
    dest = Path(dest_dir) / asset.name
    try:
        net.download(asset.url, dest, sha256=asset.sha256 or None, allowed_hosts=allowed_hosts, max_bytes=MAX_ASSET)
    except net.NetError as e:
        raise UpdateError(str(e)) from e
    if kind == "zipapp":
        try:
            verify_zipapp(dest, release.version)
        except UpdateError:
            dest.unlink(missing_ok=True)
            raise
    return dest


# ----------------------------------------------------------------------- install

# Runs in a separate Python process after this one exits: waits until the old file can be replaced, swaps it, restarts.
_HELPER = r"""
import os, subprocess, sys, time
pid, new, old = int(sys.argv[1]), sys.argv[2], sys.argv[3]
deadline = time.time() + 90
while time.time() < deadline:
    alive = False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass
    if not alive:
        try:
            os.replace(new, old)
            break
        except OSError:
            pass
    time.sleep(0.4)
else:
    sys.exit(1)
flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0
subprocess.Popen([sys.executable, old], creationflags=flags, close_fds=True)
"""


def _detached() -> dict:
    if sys.platform == "win32":
        return {"creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return {"start_new_session": True}


def apply_update(kind: str, new_file: Path, popen: Callable = subprocess.Popen) -> None:
    """Start the replacement. The caller must exit the app right afterwards."""
    new_file = Path(new_file)
    if kind == "zipapp":
        target = zipapp_path()
        if target is None:
            raise UpdateError("Cannot tell which file the app is running from.")
        popen([sys.executable, "-c", _HELPER, str(os.getpid()), str(new_file), str(target)], **_detached())
    elif kind == "installer":
        exe = Path(sys.executable).resolve()
        pf = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)")]
        machine_wide = any(p and str(exe).lower().startswith(p.lower()) for p in pf)
        popen([str(new_file), "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS",
               "/ALLUSERS" if machine_wide else "/CURRENTUSER"], **_detached())
    else:
        raise UpdateError("This copy of the app cannot update itself; download the new version from the release page.")


def update_dir() -> Path:
    d = net.cache_dir() / "update"
    d.mkdir(parents=True, exist_ok=True)
    return d

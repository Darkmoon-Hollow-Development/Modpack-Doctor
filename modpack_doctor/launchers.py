"""Which launcher a pack lives in: names, where each keeps its instances, and where to look for a mod by hand.

Tk-free and side-effect free, so the engine, the command line and the window can all use it. The folder
locations are the launchers' usual defaults; people can move them, so they are only used to *start* a file
dialog in a sensible place, never to decide anything about a pack.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

AUTO = "auto"

# key -> label shown in the window
LAUNCHERS = {
    AUTO: "Auto-detect",
    "curseforge": "CurseForge app",
    "modrinth": "Modrinth App",
    "prism": "Prism / MultiMC",
    "atlauncher": "ATLauncher",
    "gdlauncher": "GDLauncher",
    "official": "Minecraft Launcher",
    "server": "Server / other",
}
KEYS = tuple(LAUNCHERS)

# The launcher that a metadata file we can read tells us about (see sources.read_launcher_meta).
DETECTED_BY_SOURCE = {"Prism/MultiMC instance": "prism", "CurseForge app instance": "curseforge",
                      "Modrinth pack": "modrinth", "CurseForge pack": "curseforge"}

# Which site to point people at first when a mod is missing.
_SITE_ORDER = {"curseforge": ("curseforge", "modrinth"), "modrinth": ("modrinth", "curseforge")}


def label(key: Optional[str]) -> str:
    return LAUNCHERS.get(key or AUTO, LAUNCHERS[AUTO])


def key_for_label(text: str) -> str:
    """The key for a label from the drop-down (anything unknown is Auto-detect)."""
    t = (text or "").strip().lower()
    for k, v in LAUNCHERS.items():
        if t in (k, v.lower()):
            return k
    return AUTO


def side_for(key: Optional[str]) -> Optional[str]:
    """What a launcher choice implies about the pack: 'server' for server folders, 'client' for game launchers."""
    if not key or key == AUTO:
        return None
    return "server" if key == "server" else "client"


def _candidates(key: str, platform: str, env: dict, home: Path) -> list:
    appdata = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    support = home / "Library" / "Application Support"
    share = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    win, mac = platform.startswith("win"), platform == "darwin"
    if key == "curseforge":
        return [home / "curseforge" / "minecraft" / "Instances", home / "Documents" / "curseforge" / "minecraft" / "Instances"]
    if key == "modrinth":
        return [appdata / "ModrinthApp" / "profiles"] if win else [support / "ModrinthApp" / "profiles"] if mac \
            else [share / "ModrinthApp" / "profiles"]
    if key == "prism":
        base = appdata if win else support if mac else share
        return [base / "PrismLauncher" / "instances", base / "MultiMC" / "instances", home / "MultiMC" / "instances"]
    if key == "atlauncher":
        return [appdata / "ATLauncher" / "instances"] if win else [support / "ATLauncher" / "instances"] if mac \
            else [share / "ATLauncher" / "instances", home / "ATLauncher" / "instances"]
    if key == "gdlauncher":
        base = appdata if win else support if mac else share
        return [base / "gdlauncher_carbon" / "data" / "instances", base / "gdlauncher_next" / "instances"]
    if key == "official":
        return [appdata / ".minecraft"] if win else [support / "minecraft"] if mac else [home / ".minecraft"]
    return []


def instances_folder(key: Optional[str], *, platform: Optional[str] = None, env: Optional[dict] = None,
                     home: Optional[Path] = None) -> Optional[Path]:
    """The first of the launcher's usual instance folders that exists on this computer, else None."""
    if not key or key in (AUTO, "server"):
        return None
    for c in _candidates(key, platform or sys.platform, os.environ if env is None else env,
                         Path(home) if home else Path.home()):
        try:
            if c.is_dir():
                return c
        except OSError:
            pass
    return None


def pack_file_type(key: Optional[str]) -> Optional[tuple]:
    """('CurseForge pack', '*.zip') etc. for the launchers whose pack files have a known type."""
    return {"curseforge": ("CurseForge pack", "*.zip"), "modrinth": ("Modrinth pack", "*.mrpack")}.get(key or "")


def search_links(query: str, launcher: Optional[str] = None) -> list:
    """[(button label, url)] for looking a mod up by hand. The launcher's own site comes first."""
    q = urllib.parse.quote_plus((query or "").strip())
    if not q:
        return []
    sites = {
        "modrinth": ("Search Modrinth", f"https://modrinth.com/mods?q={q}"),
        "curseforge": ("Search CurseForge", f"https://www.curseforge.com/minecraft/search?search={q}"),
    }
    order = _SITE_ORDER.get(launcher or "", ("modrinth", "curseforge"))
    return [sites[k] for k in order]

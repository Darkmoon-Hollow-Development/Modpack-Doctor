"""CurseForge API access for resolving ``manifest.json`` file ids into downloads.

An API key is required (free from https://console.curseforge.com). Mods whose authors disabled
third-party distribution come back without a download URL; those are reported as "manual download"
and never fetched by other means.
"""
from __future__ import annotations

from .net import NetError, http_json

API = "https://api.curseforge.com/v1"


def resolve_files(file_ids: list, api_key: str) -> list:
    """Return the CurseForge file objects for ``file_ids`` (order not guaranteed)."""
    out: list = []
    headers = {"x-api-key": api_key}
    for i in range(0, len(file_ids), 200):
        chunk = file_ids[i:i + 200]
        res = http_json(f"{API}/mods/files", "POST", {"fileIds": chunk}, headers=headers,
                        allowed_hosts={"api.curseforge.com"})
        if res is None:
            raise NetError("CurseForge returned nothing; check your API key")
        out.extend(res.get("data", []))
    return out


def sha1_of(file_obj: dict):
    for h in file_obj.get("hashes", []) or []:
        if h.get("algo") == 1:
            return h.get("value")
    return None

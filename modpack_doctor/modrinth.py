"""Minimal Modrinth API v2 client (no API key required)."""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Iterable, Optional

from .net import NetError, http_json

API = "https://api.modrinth.com/v2"
_CHUNK = 400


def loader_filter(loader: Optional[str], mc: Optional[str] = None) -> list:
    """Loaders whose Modrinth builds are expected to work on the pack's loader."""
    if loader == "quilt":
        return ["quilt", "fabric"]
    if loader == "neoforge":
        return ["neoforge", "forge"] if mc == "1.20.1" else ["neoforge"]
    return [loader] if loader else []


def _chunks(seq: list, n: int = _CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def versions_by_hash(hashes: list, algorithm: str = "sha1") -> dict:
    out: dict = {}
    for chunk in _chunks(list(hashes)):
        res = http_json(f"{API}/version_files", "POST", {"hashes": chunk, "algorithm": algorithm})
        out.update(res or {})
    return out


def latest_by_hash(hashes: list, loaders: list, game_versions: Optional[list], algorithm: str = "sha1") -> dict:
    out: dict = {}
    body_extra: dict = {"loaders": loaders}
    if game_versions:
        body_extra["game_versions"] = game_versions
    for chunk in _chunks(list(hashes)):
        res = http_json(f"{API}/version_files/update", "POST",
                        {"hashes": chunk, "algorithm": algorithm, **body_extra})
        out.update(res or {})
    return out


def project_titles(project_ids: Iterable[str]) -> dict:
    ids = sorted(set(project_ids))
    out: dict = {}
    for chunk in _chunks(ids, 100):
        q = urllib.parse.quote(json.dumps(chunk))
        for p in http_json(f"{API}/projects?ids={q}") or []:
            out[p["id"]] = p
    return out


def get_project(slug_or_id: str) -> Optional[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{2,64}", slug_or_id or ""):
        return None
    return http_json(f"{API}/project/{urllib.parse.quote(slug_or_id)}")


def search(query: str, loaders: list, mc: Optional[str], limit: int = 5) -> list:
    facets = [["project_type:mod"]]
    if loaders:
        facets.append([f"categories:{l}" for l in loaders])
    if mc:
        facets.append([f"versions:{mc}"])
    url = (f"{API}/search?query={urllib.parse.quote(query)}&limit={limit}"
           f"&facets={urllib.parse.quote(json.dumps(facets))}")
    res = http_json(url) or {}
    return res.get("hits", [])


def best_version(project: str, loaders: list, mc: Optional[str]) -> Optional[dict]:
    params = []
    if loaders:
        params.append("loaders=" + urllib.parse.quote(json.dumps(loaders)))
    if mc:
        params.append("game_versions=" + urllib.parse.quote(json.dumps([mc])))
    q = ("?" + "&".join(params)) if params else ""
    versions = http_json(f"{API}/project/{urllib.parse.quote(project)}/version{q}") or []
    if not versions:
        return None
    # Prefer a stable release, then the newest.
    for kind in ("release", "beta", "alpha"):
        pool = [v for v in versions if v.get("version_type") == kind]
        if pool:
            return sorted(pool, key=lambda v: v.get("date_published", ""), reverse=True)[0]
    return versions[0]


def primary_file(version: dict) -> Optional[dict]:
    files = version.get("files") or []
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def resolve_project(query: str, slug: Optional[str], loaders: list, mc: Optional[str]) -> Optional[dict]:
    """Find the Modrinth project for a mod id. Returns None unless the match is confident:
    an exact slug hit, or a search hit whose slug/title equals the query once punctuation is removed."""
    for cand in [slug, query, query.replace("_", "-")]:
        if cand:
            p = get_project(cand)
            if p and p.get("project_type") == "mod":
                return p
    target = _norm(query)
    for hit in search(query, loaders, mc):
        if _norm(hit.get("slug", "")) == target or _norm(hit.get("title", "")) == target:
            return get_project(hit["project_id"]) if hit.get("project_id") else None
    return None

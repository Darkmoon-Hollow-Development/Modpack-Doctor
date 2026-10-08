"""Online step: ask Modrinth which installed mods have newer / correct-for-this-pack builds."""
from __future__ import annotations

from typing import Callable, Optional

from . import modrinth
from .models import Action, Finding, Fix, PackContext
from .net import NetError


def add_update_findings(pack: PackContext, jars: list, findings: list,
                        progress: Optional[Callable] = None) -> None:
    """Mutates ``findings``: attaches replacement fixes to mismatch findings and adds update suggestions."""
    if not pack.loader:
        pack.notes.append("Online update check skipped: mod loader unknown.")
        return
    scanned = [j for j in jars if j.mods]
    if not scanned:
        return
    if progress:
        progress("Hashing mods for the Modrinth lookup", None)
    by_hash = {}
    for j in scanned:
        try:
            by_hash[j.sha1] = j
        except OSError:
            continue
    try:
        if progress:
            progress("Asking Modrinth about your mods", None)
        known = modrinth.versions_by_hash(list(by_hash))
        if not known:
            pack.notes.append("None of the installed jars were recognised by Modrinth (CurseForge-only mods are not covered).")
            return
        loaders = modrinth.loader_filter(pack.loader, pack.mc_version)
        gv = [pack.mc_version] if pack.mc_version else None
        latest = modrinth.latest_by_hash(list(known), loaders, gv)
        titles = modrinth.project_titles(v["project_id"] for v in known.values())
    except NetError as e:
        pack.notes.append(f"Online update check failed: {e}")
        return

    problem_by_file: dict = {}
    for f in findings:
        if f.code in ("mc_mismatch", "loader_mismatch") and f.severity == "error":
            for rel in f.files:
                problem_by_file[rel] = f

    updates = 0
    for h, current in known.items():
        jar = by_hash.get(h)
        new = latest.get(h)
        if jar is None:
            continue
        if not new:
            if jar.rel in problem_by_file:
                problem_by_file[jar.rel].detail += (
                    f"\nModrinth has no {pack.loader} build of this mod for Minecraft {pack.mc_version}.")
            continue
        if new.get("id") == current.get("id"):
            continue
        pf = modrinth.primary_file(new)
        if not pf:
            continue
        hashes = pf.get("hashes", {})
        title = (titles.get(current["project_id"]) or {}).get("title") or jar.rel
        action = Action("replace_mod", {"file": jar.rel, "url": pf["url"], "filename": pf["filename"],
                                        "sha1": hashes.get("sha1"), "sha512": hashes.get("sha512")})
        label = f"Replace with {title} {new.get('version_number')}"
        if jar.rel in problem_by_file:
            problem_by_file[jar.rel].fixes.insert(0, Fix(label, [action], auto=True, needs_network=True))
            continue
        updates += 1
        findings.append(Finding(
            "info", "update", "Updates",
            f"{title}: {current.get('version_number')} -> {new.get('version_number')}",
            f"Modrinth lists a newer build of {title} for {pack.loader} {pack.mc_version or ''}. "
            "Updating is optional; check the changelog for breaking changes first.".replace("  ", " "),
            mods=[title], files=[jar.rel],
            fixes=[Fix(label, [action], auto=False, needs_network=True)]))
    if updates == 0:
        pack.notes.append("Online check: every recognised mod is already on its latest compatible build.")

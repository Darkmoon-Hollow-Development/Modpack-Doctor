"""Compatibility analysis. Pure functions of (pack, scanned jars, rules): no network, no disk writes."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

from .models import Action, Dependency, Finding, Fix, JarInfo, ModInfo, PackContext
from .version import Version, format_range, is_release_like, matches, sort_key

LOADER_BUILTIN_IDS = {
    "forge": ("forge",),
    "neoforge": ("neoforge",),
    "fabric": ("fabricloader",),
    "quilt": ("quilt_loader", "fabricloader"),
}
ALL_LOADER_IDS = {"forge", "neoforge", "fabricloader", "quilt_loader"}


class _Prov:
    __slots__ = ("version", "jar", "bundled", "builtin")

    def __init__(self, version, jar=None, bundled=False, builtin=False):
        self.version, self.jar, self.bundled, self.builtin = version, jar, bundled, builtin


def loader_accepts(pack: PackContext, mod_loader: str) -> bool:
    """Can a mod written for ``mod_loader`` load on the pack's loader?"""
    pl = pack.loader
    if pl is None or mod_loader == pl:
        return True
    if pl == "quilt" and mod_loader == "fabric":
        return True
    if pl == "neoforge" and mod_loader == "forge" and pack.mc_version == "1.20.1":
        return True
    return False


def _norm_id(s: str) -> str:
    return (s or "").lower()


_NOT_FABRIC_API = {"fabric-permissions-api-v0", "fabric-language-kotlin"}


def _is_fabric_api_module(did: str) -> bool:
    if did in ("fabric", "fabric-api", "fabric-api-base") or did in _NOT_FABRIC_API:
        return did in ("fabric", "fabric-api", "fabric-api-base")
    return did.startswith("fabric-") and re.search(r"-v\d+$", did) is not None


class Analyzer:
    def __init__(self, pack: PackContext, jars: list, rules: dict):
        self.pack, self.jars, self.rules = pack, jars, rules
        self.findings: list = []
        self.active: list = []  # (JarInfo, ModInfo) that can load on this pack
        self.inert: list = []  # (JarInfo, ModInfo) written for another loader
        self.avail: dict = defaultdict(list)  # mod id -> [_Prov]
        self.has_connector = False

    # ------------------------------------------------------------------ driver
    def run(self) -> list:
        self._classify()
        self._build_availability()
        self._check_dependencies()
        self._check_duplicates()
        self._check_rule_conflicts()
        self._check_redundant()
        self._check_side()
        self._recommend()
        self.findings.sort(key=lambda f: ({"error": 0, "warning": 1, "info": 2}[f.severity], f.category, f.title))
        for f in self.findings:
            f.trace.insert(0, f"Check: {f.code} ({CHECKS.get(f.code, 'analyzer')})")
        return self.findings

    def _add(self, *a, **kw) -> Finding:
        f = Finding(*a, **kw)
        self.findings.append(f)
        return f

    # ---- developer trace helpers: say exactly where in which file a finding comes from
    @staticmethod
    def _src(jar: JarInfo, m: ModInfo) -> str:
        return f'{jar.rel} > {m.source or "(metadata file unknown)"} > mod "{m.mod_id}"' + \
               (f" version {m.version}" if m.version else "")

    def _dep_trace(self, jar: JarInfo, m: ModInfo, d) -> str:
        rng = format_range(d.version_range)
        return f'{self._src(jar, m)} > {d.path or d.mod_id} [{d.kind}, wants {rng}]'

    # ------------------------------------------------------------------ classification
    def _classify(self) -> None:
        pack = self.pack
        no_meta, bad = [], []
        for jar in self.jars:
            unreadable = bool(jar.errors) and jar.errors[0].startswith(("Not a valid", "Could not read"))
            if unreadable:
                bad.append(jar)
                continue
            if not jar.mods:
                if jar.errors:
                    self._add("warning", "bad_metadata", "Metadata problem",
                              f"{jar.rel}: the mod metadata could not be read", "\n".join(jar.errors),
                              files=[jar.rel], trace=self._meta_trace(jar),
                              fixes=[Fix("Disable this jar (the loader most likely cannot read it either)",
                                         [Action("disable_mod", {"file": jar.rel})], auto=False)])
                else:
                    no_meta.append(jar)
                continue
            fit = [m for m in jar.mods if loader_accepts(pack, m.loader)]
            if fit:
                self.active.extend((jar, m) for m in fit)
            else:
                self.inert.extend((jar, m) for m in jar.mods)
            if jar.errors:
                self._add("warning", "bad_metadata", "Metadata problem",
                          f"{jar.rel}: part of this jar's mod metadata could not be read",
                          "\n".join(jar.errors) + "\n\nThe mod itself was recognised from its other metadata, so this is "
                          "often harmless (for example a second loader's file that is slightly malformed).",
                          files=[jar.rel], trace=self._meta_trace(jar))
        for jar in bad:
            self._add("error", "bad_jar", "Unreadable jar", f"{jar.rel} cannot be read",
                      "\n".join(jar.errors) + "\nThe file is probably corrupt or a partial download. "
                      "Delete it and download it again.", files=[jar.rel],
                      trace=[f"{jar.rel} > zip container: {jar.errors[0] if jar.errors else 'unreadable'}"],
                      fixes=[Fix("Disable this file", [Action("disable_mod", {"file": jar.rel})], auto=False)])
        if no_meta:
            names = [j.rel for j in no_meta]
            shown = "\n".join(f"  - {n}" for n in names[:40]) + (f"\n  ... and {len(names) - 40} more" if len(names) > 40 else "")
            self._add("info", "no_metadata", "Jars without mod metadata",
                      f"{len(names)} jar(s) contain no recognizable mod metadata",
                      "These are usually libraries, coremods, or mods for an old loader version. "
                      "They cannot be checked for compatibility.\n" + shown, files=names,
                      trace=[f"{j.rel} > none of fabric.mod.json, quilt.mod.json, META-INF/(neoforge.)mods.toml, "
                             "mcmod.info found" for j in no_meta[:40]])

        self.has_connector = any(_norm_id(m.mod_id) == "connector" for _, m in self.active)
        if self.inert:
            self._report_loader_mismatch()

    @staticmethod
    def _meta_trace(jar: JarInfo) -> list:
        lines = []
        for mf in jar.meta_files:
            if mf["ok"]:
                note = f" - NOTE: {mf['note']}" if mf.get("note") else ""
                lines.append(f'{jar.rel} > {mf["file"]}: read OK, mods: {", ".join(mf["mods"]) or "none declared"}{note}')
            else:
                lines.append(f'{jar.rel} > {mf["file"]}: FAILED - {mf["error"]}')
        return lines or [f"{jar.rel}: {e}" for e in jar.errors]

    def _report_loader_mismatch(self) -> None:
        pack = self.pack
        by_jar: dict = defaultdict(list)
        for jar, m in self.inert:
            by_jar[jar.rel].append(m)
        for rel, mods in sorted(by_jar.items()):
            loaders = sorted({m.loader for m in mods})
            names = ", ".join(m.display for m in mods[:3])
            soft = self.has_connector and pack.loader in ("forge", "neoforge") and loaders == ["fabric"]
            if soft:
                self._add("warning", "loader_mismatch", "Loader",
                          f"{names} is a Fabric mod running through Sinytra Connector",
                          "Connector can load many Fabric mods on Forge/NeoForge, but compatibility varies "
                          "per mod. If the game crashes, this is a good first suspect.",
                          mods=[m.mod_id for m in mods], files=[rel],
                          trace=[f'{rel} > {m.source} > mod "{m.mod_id}" is a {m.loader} mod' for m in mods])
            else:
                self._add("error", "loader_mismatch", "Loader",
                          f"{names} is built for {'/'.join(loaders)}, but this pack uses {pack.loader}",
                          f"{rel} will not load. Replace it with the {pack.loader} version of the mod, "
                          "or remove it.", mods=[m.mod_id for m in mods], files=[rel],
                          trace=[f'{rel} > {m.source} > mod "{m.mod_id}" is a {m.loader} mod; pack loader is {pack.loader} '
                                 f'(from: {"; ".join(pack.notes[:2]) or "launcher files or the mods themselves"})' for m in mods],
                          fixes=[Fix("Disable this jar", [Action("disable_mod", {"file": rel})], auto=False)])

    # ------------------------------------------------------------------ availability map
    def _build_availability(self) -> None:
        pack = self.pack
        self.avail["minecraft"].append(_Prov(pack.mc_version, builtin=True))
        for lid in LOADER_BUILTIN_IDS.get(pack.loader or "", ()):
            self.avail[lid].append(_Prov(pack.loader_version if lid != "fabricloader" or pack.loader == "fabric" else None,
                                         builtin=True))
        if pack.loader == "neoforge" and pack.mc_version == "1.20.1":
            self.avail["forge"].append(_Prov(None, builtin=True))
        self.avail["java"].append(_Prov(None, builtin=True))
        for jar, m in self.active + self.inert:  # inert mods count so one root cause isn't reported repeatedly
            self.avail[_norm_id(m.mod_id)].append(_Prov(m.version, jar))
            for p in m.provides:
                self.avail[_norm_id(p)].append(_Prov(m.version, jar))
        for jar in self.jars:
            for b in jar.bundled:
                self.avail[_norm_id(b.mod_id)].append(_Prov(b.version, jar, bundled=True))
                for p in b.provides:
                    self.avail[_norm_id(p)].append(_Prov(b.version, jar, bundled=True))

    # ------------------------------------------------------------------ dependencies
    def _applies(self, d: Dependency) -> bool:
        s = self.pack.side
        return not ((d.side == "client" and s == "server") or (d.side == "server" and s == "client"))

    def _check_dependencies(self) -> None:
        pack = self.pack
        missing: dict = defaultdict(list)
        mismatch: dict = defaultdict(list)
        breaks: dict = {}
        mc_bad: list = []
        loader_ver_bad: dict = defaultdict(list)
        recommended: dict = defaultdict(list)
        soft_bad: list = []

        for jar, m in self.active:
            for d in m.deps:
                if not self._applies(d):
                    continue
                did = _norm_id(d.mod_id)
                provs = self.avail.get(did, [])
                own = {_norm_id(m.mod_id)} | {_norm_id(p) for p in m.provides}
                if d.kind in ("breaks", "conflicts"):
                    if did in own or did in ("minecraft", "java") or did in ALL_LOADER_IDS:
                        continue
                    for p in provs:
                        if p.jar is jar:
                            continue
                        if matches(d.version_range, d.syntax, p.version) is not False:
                            key = frozenset({_norm_id(m.mod_id), did})
                            breaks.setdefault(key, (jar, m, d, p))
                            break
                    continue
                if d.kind in ("recommended",):
                    if not provs:
                        recommended[did].append(m)
                    continue
                if d.kind == "suggested":
                    continue
                if d.kind == "optional" and not provs:
                    continue

                if not provs:
                    if d.kind == "required" and did not in ("java",):
                        missing[did].append((jar, m, d))
                    continue
                if did == "minecraft":
                    v = pack.mc_version
                    if v and is_release_like(v) and matches(d.version_range, d.syntax, v) is False:
                        mc_bad.append((jar, m, d))
                    continue
                if did in ALL_LOADER_IDS and provs[0].builtin:
                    if provs[0].version and matches(d.version_range, d.syntax, provs[0].version) is False:
                        loader_ver_bad[did].append((jar, m, d))
                    continue
                if did == "java":
                    continue
                if all(matches(d.version_range, d.syntax, p.version) is False for p in provs):
                    bucket = mismatch if d.kind == "required" else None
                    if bucket is not None:
                        bucket[(did, jar.rel, _norm_id(m.mod_id))].append((jar, m, d, provs))
                    else:
                        soft_bad.append((jar, m, d, provs))

        self._report_mc_mismatch(mc_bad)
        self._report_loader_versions(loader_ver_bad)
        self._report_missing(missing)
        self._report_mismatch(mismatch)
        self._report_soft_mismatch(soft_bad)
        self._report_breaks(breaks)
        self._report_recommended(recommended)

    def _mod_fix_disable(self, jar: JarInfo, label: Optional[str] = None, auto: bool = False) -> Fix:
        return Fix(label or f"Disable {jar.rel}", [Action("disable_mod", {"file": jar.rel})], auto=auto)

    def _report_mc_mismatch(self, mc_bad: list) -> None:
        pack = self.pack
        if not mc_bad:
            return
        n_checked = len({id(j) for j, _ in self.active})
        if len(mc_bad) >= 5 and len(mc_bad) >= 0.4 * max(n_checked, 1):
            self._add("warning", "mc_version", "Minecraft version",
                      f"{len(mc_bad)} of {n_checked} mods target a different Minecraft version than {pack.mc_version}",
                      "That many mismatches usually means the pack's Minecraft version was detected wrongly. "
                      "Check the 'Minecraft version' field and run the analysis again."
                      + (" (The version was inferred from the mods themselves.)" if "inferred" in " ".join(pack.notes).lower() else ""))
            return
        seen = set()
        for jar, m, d in mc_bad:
            if jar.rel in seen:
                continue
            seen.add(jar.rel)
            self._add("error", "mc_mismatch", "Minecraft version",
                      f"{m.display} is built for Minecraft {format_range(d.version_range)}, not {pack.mc_version}",
                      f"{jar.rel} requires Minecraft {format_range(d.version_range)} but this pack runs "
                      f"{pack.mc_version}. Replace it with a build for {pack.mc_version}.",
                      mods=[m.mod_id], files=[jar.rel], fixes=[self._mod_fix_disable(jar)],
                      trace=[self._dep_trace(jar, m, d), f"Pack Minecraft version is {pack.mc_version}"])

    def _report_loader_versions(self, bad: dict) -> None:
        for did, items in bad.items():
            have = self.avail[did][0].version
            reqs = "; ".join(f"{m.display} needs {format_range(d.version_range)}" for _, m, d in items[:6])
            self._add("error", "loader_version", "Loader version",
                      f"{did} {have} does not satisfy {len(items)} mod(s)", reqs +
                      "\nUpdate (or downgrade) the mod loader in your launcher, or replace the listed mods.",
                      mods=[m.mod_id for _, m, _ in items], files=[j.rel for j, _, _ in items],
                      trace=[self._dep_trace(j, m, d) for j, m, d in items[:12]] +
                            [f"Installed loader: {did} {have}"])

    def _display_for(self, did: str) -> str:
        return {"fabric-api": "Fabric API"}.get(did, did)

    def _report_missing(self, missing: dict) -> None:
        pack = self.pack
        # Fabric API is split into many "fabric-*" module ids; report them as one problem.
        if pack.loader in ("fabric", "quilt"):
            merged: list = []
            for did in list(missing):
                if _is_fabric_api_module(did):
                    merged.extend(missing.pop(did))
            if merged:
                missing["fabric-api"] = merged
        slugs = self.rules.get("slug_overrides", {})
        for did, items in sorted(missing.items()):
            seen_files, lines = set(), []
            for jar, m, d in items:
                if jar.rel in seen_files:
                    continue
                seen_files.add(jar.rel)
                lines.append(f"  - {m.display} (needs {format_range(d.version_range)})")
            who = ", ".join(sorted({m.display for _, m, _ in items})[:3])
            more = f" and {len(seen_files) - 3} more" if len(seen_files) > 3 else ""
            fixes = []
            if pack.loader and pack.mc_version:
                fixes.append(Fix(
                    f'Download "{self._display_for(did)}" from Modrinth',
                    [Action("install_project", {"query": did, "slug": slugs.get(did), "mc": pack.mc_version,
                                                "loader": pack.loader, "display": self._display_for(did)})],
                    auto=True, needs_network=True))
            self._add("error", "missing_dep", "Missing dependency",
                      f'Missing required mod "{self._display_for(did)}" (needed by {who}{more})',
                      "These mods will refuse to load, or crash the game at startup, without it:\n" + "\n".join(lines[:15]),
                      mods=sorted({m.mod_id for _, m, _ in items}), files=sorted(seen_files), fixes=fixes,
                      trace=[self._dep_trace(j, m, d) for j, m, d in items[:12]] +
                            [f'No installed mod provides id "{did}" (looked at mod ids, "provides" lists, '
                             "bundled jar-in-jar libraries and the loader's built-in ids)"])

    def _report_mismatch(self, mismatch: dict) -> None:
        for (did, rel, _), items in sorted(mismatch.items()):
            jar, m, d, provs = items[0]
            have = ", ".join(sorted({p.version or "unknown" for p in provs}))
            self._add("error", "dep_version", "Dependency version",
                      f'{m.display} needs "{did}" {format_range(d.version_range)}, but {have} is installed',
                      f"{rel} requires a different version of \"{did}\". Update or downgrade \"{did}\", "
                      f"or replace {m.name or m.mod_id}.",
                      mods=[m.mod_id, did], files=[rel] + [p.jar.rel for p in provs if p.jar],
                      trace=[self._dep_trace(jar, m, d)] +
                            [f'{p.jar.rel if p.jar else "loader built-in"} provides "{did}" version {p.version or "unknown"}'
                             for p in provs])

    def _report_soft_mismatch(self, soft: list) -> None:
        seen = set()
        for jar, m, d, provs in soft:
            key = (jar.rel, _norm_id(d.mod_id))
            if key in seen:
                continue
            seen.add(key)
            have = ", ".join(sorted({p.version or "unknown" for p in provs}))
            self._add("warning", "dep_version", "Dependency version",
                      f'{m.display} works best with "{d.mod_id}" {format_range(d.version_range)}, but {have} is installed',
                      "This is an optional dependency, so the game may still start, but the integration "
                      "can be disabled or buggy.", mods=[m.mod_id, d.mod_id], files=[jar.rel],
                      trace=[self._dep_trace(jar, m, d)] +
                            [f'{p.jar.rel if p.jar else "loader built-in"} provides "{d.mod_id}" version {p.version or "unknown"}'
                             for p in provs])

    def _report_breaks(self, breaks: dict) -> None:
        for key, (jar, m, d, p) in breaks.items():
            severity = "error" if d.kind == "breaks" else "warning"
            other_name = d.mod_id
            fixes = [self._mod_fix_disable(jar, f"Disable {m.name or m.mod_id} ({jar.rel})")]
            if p.jar is not None and p.jar is not jar:
                fixes.append(self._mod_fix_disable(p.jar, f"Disable {other_name} ({p.jar.rel})"))
            verb = "declares it is incompatible with" if d.kind == "breaks" else "declares conflicts with"
            self._add(severity, "incompatible", "Declared incompatibility",
                      f'{m.display} {verb} "{other_name}"{" " + format_range(d.version_range) if d.version_range not in (None, "*") else ""}',
                      "The mod's own metadata says these two cannot be used together. Remove one of them.",
                      mods=[m.mod_id, other_name], files=[jar.rel] + ([p.jar.rel] if p.jar else []), fixes=fixes,
                      trace=[self._dep_trace(jar, m, d),
                             f'{p.jar.rel if p.jar else "loader built-in"} provides "{other_name}" version {p.version or "unknown"}'])

    def _report_recommended(self, rec: dict) -> None:
        if not rec:
            return
        rows = sorted(rec.items(), key=lambda kv: (-len({m.mod_id for m in kv[1]}), kv[0]))
        lines = [f"  - {did}  (recommended by {', '.join(sorted({m.name or m.mod_id for m in ms})[:3])})"
                 for did, ms in rows[:20]]
        self._add("info", "recommended_deps", "Recommended companions",
                  f"{len(rows)} recommended companion mod(s) are not installed",
                  "Mod authors suggest these for full functionality or compatibility; none is required:\n" + "\n".join(lines))

    # ------------------------------------------------------------------ duplicates
    def _check_duplicates(self) -> None:
        by_id: dict = defaultdict(dict)
        for jar, m in self.active:
            by_id[_norm_id(m.mod_id)].setdefault(jar.rel, (jar, m))
        for did, per_jar in sorted(by_id.items()):
            if len(per_jar) < 2:
                continue
            entries = sorted(per_jar.values(), key=lambda jm: (sort_key(jm[1].version), jm[0].path.stat().st_mtime
                                                                if jm[0].path.exists() else 0), reverse=True)
            keep_jar, keep_mod = entries[0]
            drop = entries[1:]
            lines = [f"  - {j.rel}  (version {m.version or 'unknown'})" for j, m in entries]
            fixes = [Fix(f"Keep {keep_jar.rel}, disable the other {len(drop)}",
                         [Action("disable_mod", {"file": j.rel}) for j, _ in drop], auto=True)]
            self._add("error", "duplicate", "Duplicate mod",
                      f'Mod "{keep_mod.name or did}" is installed {len(entries)} times',
                      "Forge, NeoForge, Fabric and Quilt all refuse to start with the same mod id loaded twice. "
                      "The newest version is recommended:\n" + "\n".join(lines),
                      mods=[keep_mod.mod_id], files=[j.rel for j, _ in entries], fixes=fixes,
                      trace=[self._src(j, m) for j, m in entries])

        top_ids = set(by_id)
        for jar, m in self.active:
            for p in m.provides:
                pid = _norm_id(p)
                if pid in top_ids and any(j is not jar for j, _ in by_id[pid].values()):
                    other = next(j for j, _ in by_id[pid].values() if j is not jar)
                    self._add("warning", "provides_clash", "Duplicate mod",
                              f'{m.display} provides "{p}", which is also installed as a separate mod',
                              f"{jar.rel} and {other.rel} cover the same mod id. Fabric/Quilt typically refuse "
                              "to load this combination.", mods=[m.mod_id, p], files=[jar.rel, other.rel],
                              fixes=[self._mod_fix_disable(jar), self._mod_fix_disable(other)],
                              trace=[f'{self._src(jar, m)} > provides "{p}"', f'{other.rel} is installed as mod id "{pid}"'])

    # ------------------------------------------------------------------ rule database
    def _present(self) -> dict:
        present: dict = defaultdict(list)
        for jar, m in self.active + self.inert:
            present[_norm_id(m.mod_id)].append((jar, m))
        return present

    def _check_rule_conflicts(self) -> None:
        present = self._present()
        for ri, rule in enumerate(self.rules.get("conflicts", [])):
            a = [x for x in map(_norm_id, rule["a"]) if x in present]
            b = [x for x in map(_norm_id, rule["b"]) if x in present]
            if not a or not b:
                continue
            side_a = {j.rel: j for x in a for j, _ in present[x]}
            side_b = {j.rel: j for x in b for j, _ in present[x]}
            fix_a = Fix(f"Disable {', '.join(a)} (keep {', '.join(b)})",
                        [Action("disable_mod", {"file": r}) for r in side_a])
            fix_b = Fix(f"Disable {', '.join(b)} (keep {', '.join(a)})",
                        [Action("disable_mod", {"file": r}) for r in side_b])
            fixes = [fix_a, fix_b] if rule.get("prefer", "b") == "b" else [fix_b, fix_a]
            self._add(rule.get("severity", "warning"), "rule_conflict", "Known conflict",
                      f"{', '.join(a)} conflicts with {', '.join(b)}", rule["message"],
                      mods=a + b, files=sorted(set(side_a) | set(side_b)), fixes=fixes,
                      trace=[f"rules.json > conflicts[{ri}]: a={rule['a']} b={rule['b']} severity={rule.get('severity', 'warning')}"] +
                            [f'{j.rel} matches "{x}"' for x in a + b for j, _ in present[x]][:12])

    def _check_redundant(self) -> None:
        present = self._present()
        for ri, rule in enumerate(self.rules.get("redundant", [])):
            group = [x for x in map(_norm_id, rule["group"]) if x in present]
            if len(group) < 2:
                continue
            order = rule.get("prefer_by_loader", {}).get(self.pack.loader or "", rule.get("prefer", rule["group"]))
            order = list(map(_norm_id, order)) + [g for g in group if g not in set(map(_norm_id, order))]
            ranked = [x for x in order if x in group]
            fixes = []
            for keep in ranked:
                drop_files = sorted({j.rel for x in group if x != keep for j, _ in present[x]})
                fixes.append(Fix(f"Keep {keep}, disable {', '.join(x for x in group if x != keep)}",
                                 [Action("disable_mod", {"file": r}) for r in drop_files]))
            self._add(rule.get("severity", "info"), "rule_redundant", "Redundant mods",
                      f"Overlapping mods installed: {', '.join(group)}", rule["message"],
                      mods=group, files=sorted({j.rel for x in group for j, _ in present[x]}), fixes=fixes,
                      trace=[f"rules.json > redundant[{ri}]: group={rule['group']}"] +
                            [f'{j.rel} matches "{x}"' for x in group for j, _ in present[x]][:12])

    # ------------------------------------------------------------------ client/server side
    def _check_side(self) -> None:
        if self.pack.side != "server":
            return
        for jar, m in self.active:
            if m.environment == "client" or m.client_only_hint:
                self._add("warning", "client_only", "Client-only mod",
                          f"{m.display} is client-only but this is a server pack",
                          f"{jar.rel} is declared as client-side. On a dedicated server it is useless at best and "
                          "a frequent source of startup crashes at worst.",
                          mods=[m.mod_id], files=[jar.rel],
                          trace=[f'{self._src(jar, m)} > environment = "{m.environment}"'
                                 + (" (client-only dependency on the loader)" if m.client_only_hint else "")],
                          fixes=[self._mod_fix_disable(jar, f"Disable {jar.rel}", auto=True)])

    # ------------------------------------------------------------------ recommendations
    def _recommend(self) -> None:
        pack = self.pack
        if not pack.loader or len(self.active) < 10:
            return
        present = self._present()
        for ri, rule in enumerate(self.rules.get("recommended", [])):
            if pack.loader not in rule.get("loaders", []):
                continue
            if rule.get("client_only") and pack.side == "server":
                continue
            if any(_norm_id(x) in present for x in rule.get("absent_if_any", [])):
                continue
            slug = rule.get("slug") or rule.get("slug_by_loader", {}).get(pack.loader)
            display = rule.get("display") or rule.get("display_by_loader", {}).get(pack.loader) or slug
            fixes = []
            if slug and pack.mc_version:
                fixes.append(Fix(f"Download {display} from Modrinth",
                                 [Action("install_project", {"query": slug, "slug": slug, "mc": pack.mc_version,
                                                             "loader": pack.loader, "display": display})],
                                 auto=False, needs_network=True))
            self._add("info", "recommend", "Suggestion", rule["title"], rule["message"],
                      mods=[slug] if slug else [], fixes=fixes,
                      trace=[f"rules.json > recommended[{ri}]: loaders={rule.get('loaders')}, "
                             f"absent_if_any={rule.get('absent_if_any', [])}",
                             f"none of those mod ids is installed; pack loader is {pack.loader}"])


CHECKS = {
    "bad_metadata": "Analyzer._classify", "bad_jar": "Analyzer._classify", "no_metadata": "Analyzer._classify",
    "loader_mismatch": "Analyzer._report_loader_mismatch", "mc_version": "Analyzer._report_mc_mismatch",
    "mc_mismatch": "Analyzer._report_mc_mismatch", "loader_version": "Analyzer._report_loader_versions",
    "missing_dep": "Analyzer._report_missing", "dep_version": "Analyzer._report_mismatch / _report_soft_mismatch",
    "incompatible": "Analyzer._report_breaks", "recommended_deps": "Analyzer._report_recommended",
    "duplicate": "Analyzer._check_duplicates", "provides_clash": "Analyzer._check_duplicates",
    "rule_conflict": "Analyzer._check_rule_conflicts", "rule_redundant": "Analyzer._check_redundant",
    "client_only": "Analyzer._check_side", "recommend": "Analyzer._recommend",
}


def analyze(pack: PackContext, jars: list, rules: dict) -> list:
    return Analyzer(pack, jars, rules).run()


# ---------------------------------------------------------------------- context inference

def infer_loader(jars: list) -> Optional[str]:
    counts = {"forge": 0, "neoforge": 0, "fabric": 0, "quilt": 0}
    ids = set()
    for jar in jars:
        for l in jar.loaders:
            counts[l] += 1
        ids.update(_norm_id(m.mod_id) for m in jar.mods)
    if not any(counts.values()):
        return None
    if counts["quilt"] and (counts["quilt"] >= 3 or ids & {"quilted_fabric_api", "qsl"}) and not (counts["forge"] or counts["neoforge"]):
        return "quilt"
    best = max(counts, key=lambda k: (counts[k], {"neoforge": 3, "forge": 2, "fabric": 1, "quilt": 0}[k]))
    return best


def infer_mc_version(jars: list) -> tuple:
    """Return (version, coverage) where coverage is the share of mods whose range accepts it."""
    import re
    ranges = []
    for jar in jars:
        for m in jar.mods:
            for d in m.deps:
                if d.mod_id == "minecraft" and d.kind == "required":
                    ranges.append(d)
    if not ranges:
        return None, 0.0
    cands = set()
    for d in ranges:
        cands.update(re.findall(r"\d+\.\d+(?:\.\d+)?", format_range(d.version_range)))
    best, best_score = None, -1
    for c in sorted(cands, key=lambda s: Version(s), reverse=True):
        score = sum(1 for d in ranges if matches(d.version_range, d.syntax, c))
        if score > best_score:
            best, best_score = c, score
    return best, best_score / len(ranges)

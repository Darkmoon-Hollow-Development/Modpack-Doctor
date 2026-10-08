"""Read mod metadata out of .jar files (Forge, NeoForge, Fabric, Quilt, legacy Forge)."""
from __future__ import annotations

import io
import os
import re
import tomllib
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from .models import Dependency, JarInfo, ModInfo
from .util import lenient_json_loads

MAX_META_BYTES = 8 * 1024 * 1024
MAX_NESTED_JAR_BYTES = 48 * 1024 * 1024
MAX_NESTED_TOTAL_BYTES = 256 * 1024 * 1024
MAX_DEPTH = 3
_ENV = {"*": "both", "client": "client", "server": "server"}


# ----------------------------------------------------------------------- helpers

def _read(zf: zipfile.ZipFile, name: str, limit: int = MAX_META_BYTES) -> Optional[bytes]:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        raise ValueError(f"{name} is unreasonably large ({info.file_size} bytes)")
    with zf.open(info) as f:
        return f.read(limit + 1)[:limit]


def _text(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def _clean_version(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or "${" in s:
        return None
    return s


def _manifest_version(zf: zipfile.ZipFile) -> Optional[str]:
    try:
        data = _read(zf, "META-INF/MANIFEST.MF", 256 * 1024)
    except ValueError:
        return None
    if not data:
        return None
    for line in _text(data).splitlines():
        if line.lower().startswith("implementation-version:"):
            return _clean_version(line.split(":", 1)[1])
    return None


def _strip_group(mod_id: str) -> str:
    return mod_id.split(":", 1)[1] if ":" in mod_id else mod_id


# ----------------------------------------------------------------------- parsers

def parse_fabric(doc: dict) -> list:
    mod_id = doc.get("id")
    if not mod_id:
        return []
    deps = []
    for key, kind in (("depends", "required"), ("recommends", "recommended"), ("suggests", "suggested"),
                      ("conflicts", "conflicts"), ("breaks", "breaks")):
        table = doc.get(key) or {}
        if isinstance(table, dict):
            for dep_id, rng in table.items():
                deps.append(Dependency(str(dep_id), kind, rng, "fabric"))
    provides = []
    for p in doc.get("provides") or []:
        pid = p if isinstance(p, str) else (p or {}).get("id")
        if pid:
            provides.append(str(pid))
    return [ModInfo(
        mod_id=str(mod_id),
        version=_clean_version(doc.get("version")),
        loader="fabric",
        name=str(doc.get("name") or mod_id),
        provides=provides,
        deps=deps,
        environment=_ENV.get(str(doc.get("environment", "*")), "both"),
        description=str(doc.get("description") or "")[:300],
    )]


def parse_quilt(doc: dict) -> list:
    ql = doc.get("quilt_loader") or {}
    mod_id = ql.get("id")
    if not mod_id:
        return []
    deps = []
    for item in ql.get("depends") or []:
        if isinstance(item, str):
            deps.append(Dependency(_strip_group(item), "required", None, "fabric"))
        elif isinstance(item, dict) and item.get("id"):
            kind = "optional" if item.get("optional") else "required"
            deps.append(Dependency(_strip_group(str(item["id"])), kind, item.get("versions"), "fabric"))
    for item in ql.get("breaks") or []:
        if isinstance(item, str):
            deps.append(Dependency(_strip_group(item), "breaks", None, "fabric"))
        elif isinstance(item, dict) and item.get("id"):
            deps.append(Dependency(_strip_group(str(item["id"])), "breaks", item.get("versions"), "fabric"))
    provides = []
    for p in ql.get("provides") or []:
        pid = p if isinstance(p, str) else (p or {}).get("id")
        if pid:
            provides.append(_strip_group(str(pid)))
    meta = ql.get("metadata") or {}
    env = (doc.get("minecraft") or {}).get("environment", "*")
    return [ModInfo(
        mod_id=str(mod_id),
        version=_clean_version(ql.get("version")),
        loader="quilt",
        name=str(meta.get("name") or mod_id),
        provides=provides,
        deps=deps,
        environment=_ENV.get(str(env), "both"),
        description=str(meta.get("description") or "")[:300],
    )]


_FORGE_KIND = {"required": "required", "optional": "optional", "incompatible": "breaks", "discouraged": "conflicts"}
_LOADER_DEP_IDS = {"minecraft", "forge", "neoforge"}


def parse_forge_toml(text: str, loader: str, manifest_version: Optional[str]) -> list:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        # Forge's own parser is more forgiving than tomllib; fall back to a regex so we still learn the id.
        ids = re.findall(r'modId\s*=\s*"([^"]+)"', text)
        vers = re.findall(r'^\s*version\s*=\s*"([^"]*)"', text, re.M)
        if not ids:
            raise
        mi = ModInfo(ids[0], _clean_version(vers[0]) if vers else manifest_version, loader)
        mi.note = f"TOML is not strictly valid ({e}); only the mod id and version were recovered, dependencies were not read"
        return [mi]
    mods_tbl = doc.get("mods") or []
    deps_tbl = doc.get("dependencies") or {}
    if not isinstance(deps_tbl, dict):
        deps_tbl = {}
    out = []
    for m in mods_tbl:
        mid = m.get("modId")
        if not mid:
            continue
        version = _clean_version(m.get("version")) or manifest_version
        deps = []
        loader_sides = []
        for d in deps_tbl.get(mid) or []:
            dep_id = d.get("modId")
            if not dep_id:
                continue
            typ = d.get("type")
            if typ:
                kind = _FORGE_KIND.get(str(typ).lower(), "optional")
            else:
                kind = "required" if d.get("mandatory", True) else "optional"
            side = str(d.get("side", "BOTH")).lower()
            side = side if side in ("client", "server") else "both"
            if dep_id in _LOADER_DEP_IDS:
                loader_sides.append(side)
            deps.append(Dependency(str(dep_id), kind, d.get("versionRange"), "maven", side))
        out.append(ModInfo(
            mod_id=str(mid),
            version=version,
            loader=loader,
            name=str(m.get("displayName") or mid),
            deps=deps,
            client_only_hint=bool(loader_sides) and all(s == "client" for s in loader_sides),
            description=str(m.get("description") or "").strip()[:300],
        ))
    return out


def parse_mcmod_info(text: str) -> list:
    data = lenient_json_loads(text)
    if isinstance(data, dict):
        data = data.get("modList") or data.get("modlist") or []
    out = []
    for e in data if isinstance(data, list) else []:
        mid = e.get("modid")
        if not mid:
            continue
        deps = []
        for r in e.get("requiredMods") or []:
            deps.append(Dependency(str(r).split("@")[0].strip(), "required", None, "maven"))
        out.append(ModInfo(str(mid), _clean_version(e.get("version")), "forge", str(e.get("name") or mid), deps=deps))
    return out


# ----------------------------------------------------------------------- jar scanning

_FABRIC_KEYS = {"required": "depends", "recommended": "recommends", "suggested": "suggests",
                "conflicts": "conflicts", "breaks": "breaks"}


def _annotate(mods: list, source: str) -> None:
    """Remember which metadata file (and which key inside it) every mod and dependency came from."""
    for m in mods:
        m.source = source
        for d in m.deps:
            if d.syntax == "maven":
                d.path = f'[[dependencies.{m.mod_id}]] modId = "{d.mod_id}"'
            elif source == "quilt.mod.json":
                d.path = f'quilt_loader.{"breaks" if d.kind == "breaks" else "depends"} "{d.mod_id}"'
            else:
                d.path = f'{_FABRIC_KEYS.get(d.kind, d.kind)}."{d.mod_id}"'


def _scan_zip(zf: zipfile.ZipFile, depth: int, budget: list, meta: Optional[list] = None) -> tuple:
    """Return (mods, bundled_mods, errors) for one zip file. ``meta`` collects a record per metadata file."""
    mods, bundled, errors = [], [], []
    names = set(zf.namelist())
    manifest_version = _manifest_version(zf)

    def attempt(label: str, fn, file: str = ""):
        try:
            found = fn()
            _annotate(found, file or label)
            mods.extend(found)
            if meta is not None:
                meta.append({"file": file or label, "ok": True, "error": "", "mods": [m.mod_id for m in found],
                             "note": next((m.note for m in found if m.note), "")})
        except Exception as e:  # malformed metadata must never abort the scan
            errors.append(f"{label}: {type(e).__name__}: {e}")
            if meta is not None:
                meta.append({"file": file or label, "ok": False, "error": f"{type(e).__name__}: {e}", "mods": []})

    if "fabric.mod.json" in names:
        attempt("fabric.mod.json", lambda: parse_fabric(lenient_json_loads(_text(_read(zf, "fabric.mod.json")))), "fabric.mod.json")
    if "quilt.mod.json" in names:
        attempt("quilt.mod.json", lambda: parse_quilt(lenient_json_loads(_text(_read(zf, "quilt.mod.json")))), "quilt.mod.json")
    if "META-INF/neoforge.mods.toml" in names:
        attempt("neoforge.mods.toml", lambda: parse_forge_toml(
            _text(_read(zf, "META-INF/neoforge.mods.toml")), "neoforge", manifest_version), "META-INF/neoforge.mods.toml")
    if "META-INF/mods.toml" in names:
        attempt("mods.toml", lambda: parse_forge_toml(
            _text(_read(zf, "META-INF/mods.toml")), "forge", manifest_version), "META-INF/mods.toml")
    if "mcmod.info" in names and not any(m.loader in ("forge", "neoforge") for m in mods):
        attempt("mcmod.info", lambda: parse_mcmod_info(_text(_read(zf, "mcmod.info"))), "mcmod.info")

    if depth < MAX_DEPTH:
        nested = sorted(n for n in names
                        if n.lower().endswith(".jar") and (n.startswith("META-INF/jars/") or n.startswith("META-INF/jarjar/")))
        for n in nested:
            try:
                info = zf.getinfo(n)
                if info.file_size > MAX_NESTED_JAR_BYTES or budget[0] - info.file_size < 0:
                    continue
                with zf.open(info) as f:
                    data = f.read(MAX_NESTED_JAR_BYTES + 1)[:MAX_NESTED_JAR_BYTES]
                budget[0] -= len(data)
                with zipfile.ZipFile(io.BytesIO(data)) as inner:
                    n_mods, n_bundled, _ = _scan_zip(inner, depth + 1, budget)
                bundled.extend(n_mods)
                bundled.extend(n_bundled)
            except (zipfile.BadZipFile, ValueError, OSError):
                continue
    return mods, bundled, errors


def scan_jar(path: Path, root: Path) -> JarInfo:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    jar = JarInfo(path=path, rel=rel, size=size)
    try:
        with zipfile.ZipFile(path) as zf:
            jar.mods, jar.bundled, jar.errors = _scan_zip(zf, 0, [MAX_NESTED_TOTAL_BYTES], jar.meta_files)
    except zipfile.BadZipFile:
        jar.errors.append("Not a valid jar/zip file (corrupt or incomplete download?)")
    except OSError as e:
        jar.errors.append(f"Could not read file: {e}")
    return jar


def scan_mods_dir(mods_dir: Path, root: Path, progress: Optional[Callable] = None) -> tuple:
    """Scan every .jar below ``mods_dir``. Returns (jars, disabled_relpaths)."""
    jars_paths, disabled = [], []
    for dirpath, dirnames, filenames in os.walk(mods_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            low = fn.lower()
            if low.endswith(".jar"):
                jars_paths.append(full)
            elif low.endswith(".jar.disabled") or low.endswith(".disabled"):
                disabled.append(full.relative_to(root).as_posix() if root in full.parents else fn)
    results = []
    total = len(jars_paths)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, jar in enumerate(ex.map(lambda p: scan_jar(p, root), jars_paths), 1):
            results.append(jar)
            if progress and (i % 10 == 0 or i == total):
                progress(f"Reading mod metadata ({i}/{total})", i / max(total, 1))
    return results, disabled

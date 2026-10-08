"""Turn "something the user points at" into (PackContext, scanned jars).

Supported inputs: an instance folder (Prism/MultiMC, CurseForge app, Modrinth app, server folders,
or any folder with a ``mods`` directory), a Modrinth ``.mrpack``, and a CurseForge modpack ``.zip``.
Archives are materialised into a workspace folder first, because pack archives list mods rather than
containing them.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from . import curseforge, net
from .analyzer import infer_loader, infer_mc_version
from .jarscan import scan_mods_dir
from .models import LOADERS, PackContext
from .util import lenient_json_loads, safe_filename

Progress = Optional[Callable[[str, Optional[float]], None]]

MAX_EXTRACT_FILE = 256 * 1024 * 1024
MAX_EXTRACT_TOTAL = 2 * 1024 * 1024 * 1024
EXTRACT_PREFIXES = ("mods/", "config/", "defaultconfigs/")
EXTRACT_TOPLEVEL = {"server.properties"}


class SourceError(Exception):
    pass


@dataclass
class Options:
    mc: Optional[str] = None
    loader: Optional[str] = None
    side: Optional[str] = None  # client | server | both
    cf_key: Optional[str] = None
    workspace_dir: Optional[Path] = None
    mrpack_hosts: Optional[set] = None  # override download allow-list (tests)
    # Context carried over when re-checking an unpacked workspace after fixes (see pipeline.reanalyze).
    loader_version: Optional[str] = None
    name: Optional[str] = None
    source_label: Optional[str] = None
    unresolved: Optional[list] = None
    max_mem_mb: Optional[int] = None


def _p(progress: Progress, msg: str, frac: Optional[float] = None) -> None:
    if progress:
        progress(msg, frac)


# ----------------------------------------------------------------------- instance folders

def find_instance_root(path: Path) -> tuple:
    p = path.resolve()
    if not p.is_dir():
        raise SourceError(f"Not a folder: {path}")
    for c in (p, p / ".minecraft", p / "minecraft", p / "overrides"):
        if (c / "mods").is_dir():
            return c, c / "mods"
    if p.name.lower() == "mods":
        return p.parent, p
    if any(p.glob("*.jar")):
        return p, p
    raise SourceError(f"No 'mods' folder found in {path}. Pick the instance folder (the one that contains 'mods').")


def parse_loader_id(s: str) -> tuple:
    """'forge-47.2.0' -> ('forge', '47.2.0')"""
    m = re.match(r"(neoforge|forge|fabric|quilt)[-_ ]?(.*)$", (s or "").strip(), re.I)
    return (m.group(1).lower(), m.group(2) or None) if m else (None, None)


def read_launcher_meta(root: Path) -> dict:
    """Best-effort read of launcher files that record the Minecraft version and loader."""
    meta: dict = {}
    for d in (root, root.parent):
        mmc = d / "mmc-pack.json"
        if mmc.is_file():
            try:
                comps = json.loads(mmc.read_text(encoding="utf-8")).get("components", [])
                uid_loader = {"net.minecraftforge": "forge", "net.neoforged": "neoforge",
                              "net.fabricmc.fabric-loader": "fabric", "org.quiltmc.quilt-loader": "quilt"}
                for c in comps:
                    if c.get("uid") == "net.minecraft":
                        meta["mc"] = c.get("version")
                    elif c.get("uid") in uid_loader:
                        meta["loader"], meta["loader_version"] = uid_loader[c["uid"]], c.get("version")
                meta["source"] = "Prism/MultiMC instance"
            except (OSError, ValueError):
                pass
        cfg = d / "instance.cfg"
        if cfg.is_file():
            try:
                cp = configparser.ConfigParser(strict=False)
                cp.read_string("[g]\n" + cfg.read_text(encoding="utf-8", errors="replace"))
                g = cp["g"]
                if g.get("name"):
                    meta.setdefault("name", g.get("name"))
                if g.get("MaxMemAlloc", "").isdigit():
                    meta["max_mem_mb"] = int(g["MaxMemAlloc"])
            except (configparser.Error, OSError):
                pass
        cf = d / "minecraftinstance.json"
        if cf.is_file():
            try:
                data = json.loads(cf.read_text(encoding="utf-8"))
                bml = data.get("baseModLoader") or {}
                loader, lver = parse_loader_id(bml.get("name", ""))
                meta.setdefault("mc", bml.get("minecraftVersion") or data.get("gameVersion"))
                if loader:
                    meta.setdefault("loader", loader)
                    meta.setdefault("loader_version", lver)
                meta.setdefault("name", data.get("name"))
                if isinstance(data.get("allocatedMemory"), int):
                    meta.setdefault("max_mem_mb", data["allocatedMemory"])
                meta.setdefault("source", "CurseForge app instance")
            except (OSError, ValueError):
                pass
        if meta.get("mc") and meta.get("loader"):
            break
    return meta


def open_folder(path: Path, opts: Options, progress: Progress = None) -> tuple:
    root, mods_dir = find_instance_root(path)
    _p(progress, f"Scanning {mods_dir}")
    jars, disabled = scan_mods_dir(mods_dir, root, lambda m, f: _p(progress, m, f))
    pack = PackContext(name=path.name, root=root, mods_dir=mods_dir, source=f"Folder: {path}")
    meta = read_launcher_meta(root)
    pack.name = meta.get("name") or pack.name
    pack.max_mem_mb = meta.get("max_mem_mb")
    pack.disabled_count = len(disabled)
    _apply_context(pack, jars, opts, meta)
    if opts.name:
        pack.name = opts.name
    if opts.source_label:
        pack.source = opts.source_label
    if opts.unresolved:
        pack.unresolved = list(opts.unresolved)
    if opts.max_mem_mb and not pack.max_mem_mb:
        pack.max_mem_mb = opts.max_mem_mb
    if opts.loader_version and pack.loader == (opts.loader or pack.loader):
        pack.loader_version = opts.loader_version
    return pack, jars


def _apply_context(pack: PackContext, jars: list, opts: Options, meta: dict) -> None:
    pack.mod_count = sum(1 for j in jars if j.mods)
    if opts.side:
        pack.side = opts.side
    elif pack.root and (pack.root / "server.properties").is_file():
        pack.side = "server"
        pack.notes.append("Side set to 'server' because server.properties exists (override with --side).")
    elif meta.get("source"):
        pack.side = "client"
    else:
        pack.side = "both"

    pack.loader = opts.loader or meta.get("loader")
    pack.loader_version = meta.get("loader_version") if (not opts.loader or opts.loader == meta.get("loader")) else None
    if not pack.loader:
        pack.loader = infer_loader(jars)
        if pack.loader:
            pack.notes.append(f"Loader '{pack.loader}' was inferred from the mods (set it manually if wrong).")
    elif meta.get("loader") and not opts.loader:
        pack.notes.append(f"Loader {pack.loader} {pack.loader_version or ''} read from launcher data.".replace("  ", " "))

    pack.mc_version = opts.mc or meta.get("mc")
    if not pack.mc_version:
        v, cov = infer_mc_version([j for j in jars if not pack.loader or pack.loader in j.loaders or not j.loaders])
        if v:
            pack.mc_version = v
            pack.notes.append(f"Minecraft {v} was inferred from the mods ({cov:.0%} of mods accept it). "
                              "Set it manually if wrong.")
    if not pack.mc_version:
        pack.notes.append("Minecraft version could not be determined; version checks against Minecraft are skipped.")
    if not pack.loader:
        pack.notes.append("Mod loader could not be determined; loader checks are skipped.")


# ----------------------------------------------------------------------- workspaces & extraction

def make_workspace(opts: Options, name: str) -> Path:
    base = opts.workspace_dir or (Path.home() / "ModpackDoctor" / "workspaces")
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ws = base / f"{safe_filename(name, 'pack')}-{stamp}"
    n = 1
    while ws.exists():
        n += 1
        ws = base / f"{safe_filename(name, 'pack')}-{stamp}-{n}"
    ws.mkdir(parents=True)
    return ws


def safe_relpath(rel: str) -> Optional[PurePosixPath]:
    """Normalise an archive path; return None if it is absolute or escapes the workspace."""
    rel = (rel or "").replace("\\", "/")
    pp = PurePosixPath(rel)
    raw_parts = rel.split("/")
    if (pp.is_absolute() or not pp.parts or any(part in ("..", "") for part in raw_parts)
            or re.match(r"^[A-Za-z]:", rel)):
        return None
    return pp


def extract_overrides(zf: zipfile.ZipFile, prefixes: list, ws: Path) -> int:
    total, count = 0, 0
    ws_resolved = ws.resolve()
    for info in zf.infolist():
        if info.is_dir():
            continue
        for prefix in prefixes:
            if info.filename.startswith(prefix):
                rel = safe_relpath(info.filename[len(prefix):])
                break
        else:
            continue
        if rel is None:
            continue
        rel_s = rel.as_posix()
        if not (rel_s.startswith(EXTRACT_PREFIXES) or rel_s in EXTRACT_TOPLEVEL):
            continue
        if info.file_size > MAX_EXTRACT_FILE or total + info.file_size > MAX_EXTRACT_TOTAL:
            continue
        dest = (ws / rel_s).resolve()
        if ws_resolved not in dest.parents:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        total += info.file_size
        count += 1
    return count


def _fetch_cached(url: str, sha1: Optional[str], sha512: Optional[str], dest: Path, hosts, key: str) -> None:
    cached = net.cache_dir() / f"{key}.jar"
    if cached.is_file():
        shutil.copyfile(cached, dest)
        return
    net.download(url, dest, sha1=sha1, sha512=sha512, allowed_hosts=hosts)
    try:
        shutil.copyfile(dest, cached)
    except OSError:
        pass


# ----------------------------------------------------------------------- Modrinth .mrpack

def open_mrpack(path: Path, opts: Options, progress: Progress = None) -> tuple:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise SourceError(f"{path.name} is not a valid .mrpack/zip file") from e
    with zf:
        try:
            index = json.loads(zf.read("modrinth.index.json").decode("utf-8-sig"))
        except (KeyError, ValueError) as e:
            raise SourceError("modrinth.index.json is missing or invalid; this is not a Modrinth modpack") from e
        name = index.get("name") or path.stem
        deps = index.get("dependencies") or {}
        ws = make_workspace(opts, name)
        side = opts.side or "both"
        prefixes = ["overrides/"]
        if side != "server":
            prefixes.append("client-overrides/")
        if side != "client":
            prefixes.append("server-overrides/")
        _p(progress, "Extracting pack overrides")
        extract_overrides(zf, prefixes, ws)

    hosts = opts.mrpack_hosts if opts.mrpack_hosts is not None else net.MRPACK_SPEC_HOSTS
    todo, unresolved, skipped = [], [], 0
    for f in index.get("files", []):
        rel = safe_relpath(f.get("path", ""))
        if rel is None:
            unresolved.append(f"{f.get('path')!r}: unsafe path in pack, skipped")
            continue
        rel_s = rel.as_posix()
        if not rel_s.startswith("mods/"):
            skipped += 1
            continue
        env = f.get("env") or {}
        if (side == "client" and env.get("client") == "unsupported") or (side == "server" and env.get("server") == "unsupported"):
            skipped += 1
            continue
        todo.append((rel_s, f))

    def one(item):
        rel_s, f = item
        h = f.get("hashes") or {}
        urls = f.get("downloads") or []
        dest = ws / rel_s
        dest.parent.mkdir(parents=True, exist_ok=True)
        last = "no download URL"
        for url in urls:
            try:
                _fetch_cached(url, h.get("sha1"), h.get("sha512"), dest, hosts, h.get("sha1") or hashlib.sha1(url.encode()).hexdigest())
                return None
            except (net.NetError, OSError) as e:
                last = str(e)
        return f"{rel_s}: {last}"

    _p(progress, f"Downloading {len(todo)} mods", 0.0)
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for err in ex.map(one, todo):
            done += 1
            if err:
                unresolved.append(err)
            if done % 5 == 0 or done == len(todo):
                _p(progress, f"Downloaded {done}/{len(todo)} mods", done / max(len(todo), 1))

    loader, loader_version = None, None
    for key, lname in (("neoforge", "neoforge"), ("forge", "forge"), ("fabric-loader", "fabric"), ("quilt-loader", "quilt")):
        if deps.get(key):
            loader, loader_version = lname, str(deps[key])
            break
    meta = {"mc": deps.get("minecraft"), "loader": loader, "loader_version": loader_version, "source": "Modrinth pack"}
    jars, disabled = scan_mods_dir(ws / "mods", ws, lambda m, f: _p(progress, m, f)) if (ws / "mods").is_dir() else ([], [])
    pack = PackContext(name=name, root=ws, mods_dir=ws / "mods", source=f"Modrinth pack: {path.name}",
                       unresolved=unresolved, disabled_count=len(disabled))
    pack.notes.append(f"Pack downloaded into a workspace: {ws}")
    if skipped:
        pack.notes.append(f"{skipped} non-mod or other-side file(s) in the pack were not downloaded.")
    _apply_context(pack, jars, opts, meta)
    if opts.side is None:
        pack.side = "both"
    if unresolved:
        pack.notes.append(f"{len(unresolved)} pack file(s) could not be fetched, so analysis is incomplete.")
    return pack, jars


# ----------------------------------------------------------------------- CurseForge .zip

def open_curseforge(path: Path, opts: Options, progress: Progress = None) -> tuple:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise SourceError(f"{path.name} is not a valid zip file") from e
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8-sig"))
        except (KeyError, ValueError) as e:
            raise SourceError("manifest.json is missing or invalid; this is not a CurseForge modpack") from e
        name = manifest.get("name") or path.stem
        ws = make_workspace(opts, name)
        overrides = str(manifest.get("overrides") or "overrides").strip("/") + "/"
        _p(progress, "Extracting pack overrides")
        extract_overrides(zf, [overrides], ws)

    mcinfo = manifest.get("minecraft") or {}
    loaders = mcinfo.get("modLoaders") or []
    primary = next((l for l in loaders if l.get("primary")), loaders[0] if loaders else {})
    loader, lver = parse_loader_id(primary.get("id", ""))
    meta = {"mc": mcinfo.get("version"), "loader": loader, "loader_version": lver, "source": "CurseForge pack"}

    entries = [f for f in manifest.get("files", []) if f.get("fileID")]
    unresolved: list = []
    if entries and not opts.cf_key:
        unresolved.append(f"{len(entries)} mod(s) listed in the pack were not downloaded: CurseForge requires a free API key "
                          "to resolve them (console.curseforge.com). Only mods bundled inside the pack were analysed.")
    elif entries:
        _p(progress, "Resolving mod files with CurseForge", 0.0)
        try:
            objs = curseforge.resolve_files([int(f["fileID"]) for f in entries], opts.cf_key)
        except (net.NetError, ValueError) as e:
            raise SourceError(f"CurseForge lookup failed: {e}") from e
        by_id = {o["id"]: o for o in objs}
        jobs = []
        for f in entries:
            o = by_id.get(int(f["fileID"]))
            if not o:
                unresolved.append(f"project {f.get('projectID')} file {f['fileID']}: not found on CurseForge")
            elif not o.get("downloadUrl"):
                unresolved.append(f"{o.get('fileName')}: the author does not allow third-party downloads; "
                                  f"download it manually from CurseForge (project {f.get('projectID')})")
            else:
                jobs.append(o)

        def one(o):
            fname = safe_filename(o.get("fileName", ""), "mod.jar")
            if not fname.lower().endswith(".jar"):
                return f"{fname}: not a .jar file, skipped"
            dest = ws / "mods" / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            sha1 = curseforge.sha1_of(o)
            try:
                _fetch_cached(o["downloadUrl"], sha1, None, dest, opts.mrpack_hosts or net.CURSEFORGE_HOSTS,
                              sha1 or hashlib.sha1(o["downloadUrl"].encode()).hexdigest())
                return None
            except (net.NetError, OSError) as e:
                return f"{fname}: {e}"

        done = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            for err in ex.map(one, jobs):
                done += 1
                if err:
                    unresolved.append(err)
                if done % 5 == 0 or done == len(jobs):
                    _p(progress, f"Downloaded {done}/{len(jobs)} mods", done / max(len(jobs), 1))

    jars, disabled = scan_mods_dir(ws / "mods", ws, lambda m, f: _p(progress, m, f)) if (ws / "mods").is_dir() else ([], [])
    pack = PackContext(name=name, root=ws, mods_dir=ws / "mods", source=f"CurseForge pack: {path.name}",
                       unresolved=unresolved, disabled_count=len(disabled))
    pack.notes.append(f"Pack downloaded into a workspace: {ws}")
    _apply_context(pack, jars, opts, meta)
    if opts.side is None:
        pack.side = "both"
    if unresolved:
        pack.notes.append(f"{len(unresolved)} pack file(s) could not be fetched, so analysis is incomplete.")
    return pack, jars


# ----------------------------------------------------------------------- dispatcher

def open_source(path_str: str, opts: Options, progress: Progress = None) -> tuple:
    path = Path(path_str).expanduser()
    if not path.exists():
        raise SourceError(f"Path does not exist: {path}")
    if path.is_dir():
        return open_folder(path, opts, progress)
    low = path.name.lower()
    if low.endswith(".mrpack"):
        return open_mrpack(path, opts, progress)
    if low.endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile as e:
            raise SourceError(f"{path.name} is not a valid zip file") from e
        if "modrinth.index.json" in names:
            return open_mrpack(path, opts, progress)
        if "manifest.json" in names:
            return open_curseforge(path, opts, progress)
        raise SourceError("This zip is neither a Modrinth nor a CurseForge modpack "
                          "(no modrinth.index.json or manifest.json). Extract it and pick the folder instead.")
    raise SourceError("Unsupported input. Choose an instance folder, a .mrpack file, or a CurseForge modpack .zip.")

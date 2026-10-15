"""Apply fixes to an instance folder, with a journal so every change set can be undone.

Safety rules:
* Mods are never deleted. "Disable" renames ``x.jar`` to ``x.jar.disabled`` (what launchers do).
* Replaced jars and edited configs are copied into ``.modpack_doctor/backups/<id>/`` first.
* Every path is checked to stay inside the instance folder.
* Downloads come only from cdn.modrinth.com and are verified against Modrinth's hashes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import tomllib
from pathlib import Path
from typing import Callable, Optional

from . import modrinth, net
from .models import Action
from .util import lenient_json_loads, safe_filename

STATE_DIR = ".modpack_doctor"


class FixError(Exception):
    pass


def _state(root: Path) -> Path:
    d = root / STATE_DIR
    d.mkdir(exist_ok=True)
    return d


def _safe(root: Path, rel: str) -> Path:
    base = root.resolve()
    p = (base / rel).resolve()
    if p != base and base not in p.parents:
        raise FixError(f"Refusing to touch a path outside the instance folder: {rel}")
    return p


def _rel(root: Path, p: Path) -> str:
    return p.resolve().relative_to(root.resolve()).as_posix()


def _backup(root: Path, cs_id: str, p: Path) -> str:
    dest = _state(root) / "backups" / cs_id / _rel(root, p)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dest)
    return _rel(root, dest)


# ----------------------------------------------------------------------- config editors

def _fmt_toml_value(v: str) -> str:
    return v


def edit_properties(text: str, key: str, value: str) -> str:
    pat = re.compile(rf"^(\s*{re.escape(key)}\s*[=:]\s*)(.*?)(\s*)$", re.M)
    if pat.search(text):
        return pat.sub(lambda m: m.group(1) + value + m.group(3), text, count=1)
    return text + ("" if text.endswith("\n") or not text else "\n") + f"{key}={value}\n"


def edit_toml(text: str, key: str, value: str, table: str = "") -> str:
    """Line-based edit that keeps comments and layout. ``value`` must be a valid TOML literal."""
    lines = text.splitlines(keepends=True)
    current = ""
    pat = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)([^#\r\n]*?)(\s*(?:#.*)?\r?\n?)$")
    for i, line in enumerate(lines):
        h = re.match(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$", line)
        if h:
            current = h.group(1)
            continue
        if current == table:
            m = pat.match(line)
            if m:
                lines[i] = m.group(1) + value + m.group(3)
                return "".join(lines)
    raise FixError(f"Key '{key}' not found in table '{table or '(root)'}'; edit this file manually")


def edit_json(text: str, key: str, value: str) -> str:
    data = json.loads(text.lstrip("﻿"))  # strict on purpose: rewriting would drop comments
    node = data
    parts = key.split(".")
    for part in parts[:-1]:
        node = node[part]
    try:
        new = json.loads(value)
    except ValueError:
        new = value
    node[parts[-1]] = new
    return json.dumps(data, indent=2) + "\n"


def _apply_edit(root: Path, cs_id: str, params: dict) -> dict:
    p = _safe(root, params["file"])
    if not p.is_file():
        raise FixError(f"Config file not found: {params['file']}")
    fmt = params.get("format") or {".toml": "toml", ".json": "json"}.get(p.suffix.lower(), "properties")
    original = p.read_text(encoding="utf-8")
    try:
        if fmt == "properties":
            new = edit_properties(original, params["key"], str(params["value"]))
        elif fmt == "toml":
            new = edit_toml(original, params["key"], str(params["value"]), params.get("table", ""))
            tomllib.loads(new)
        elif fmt == "json":
            new = edit_json(original, params["key"], str(params["value"]))
        else:
            raise FixError(f"Unsupported config format: {fmt}")
    except (ValueError, KeyError, TypeError) as e:
        raise FixError(f"Could not edit {params['file']}: {e}") from e
    backup = _backup(root, cs_id, p)
    p.write_text(new, encoding="utf-8", newline="")
    return {"op": "config", "path": _rel(root, p), "backup": backup}


# ----------------------------------------------------------------------- jar operations

def _disable(root: Path, params: dict) -> dict:
    src = _safe(root, params["file"])
    if not src.is_file():
        raise FixError(f"File not found (already changed?): {params['file']}")
    if not src.name.lower().endswith(".jar"):
        raise FixError(f"Only .jar files can be disabled: {params['file']}")
    dst = src.with_name(src.name + ".disabled")
    n = 1
    while dst.exists():
        dst = src.with_name(f"{src.name}.disabled{n}")
        n += 1
    src.rename(dst)
    return {"op": "disable", "from": _rel(root, src), "to": _rel(root, dst)}


_DISABLED_RE = re.compile(r"^(?P<base>.+?\.jar)\.disabled\d*$", re.I)


def list_disabled(root: Path) -> list:
    """Jars that were disabled by renaming (``x.jar.disabled``), as relative paths."""
    root = Path(root)
    out = []
    mods = root / "mods"
    if not mods.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(mods):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if _DISABLED_RE.match(fn):
                out.append((Path(dirpath) / fn).relative_to(root).as_posix())
    return out


def _enable(root: Path, params: dict) -> dict:
    src = _safe(root, params["file"])
    if not src.is_file():
        raise FixError(f"File not found (already changed?): {params['file']}")
    m = _DISABLED_RE.match(src.name)
    if not m:
        raise FixError(f"Not a disabled jar: {params['file']}")
    dst = src.with_name(m.group("base"))
    if dst.exists():
        raise FixError(f"Cannot enable {src.name}: {dst.name} already exists")
    src.rename(dst)
    return {"op": "enable", "from": _rel(root, src), "to": _rel(root, dst)}


def _download_to(root: Path, cs_id: str, params: dict, dest_dir: Path) -> Path:
    url = params["url"]
    fname = safe_filename(params["filename"], "mod.jar")
    if not fname.lower().endswith(".jar"):
        raise FixError(f"Refusing to install a non-jar file: {fname}")
    if not params.get("sha1") and not params.get("sha512"):
        raise FixError("No hash available to verify the download; refusing to install")
    dest = dest_dir / fname
    if dest.exists():
        raise FixError(f"{fname} already exists")
    try:
        net.download(url, dest, sha1=params.get("sha1"), sha512=params.get("sha512"),
                     allowed_hosts=net.MODRINTH_HOSTS)
    except net.NetError as e:
        raise FixError(f"Download of {fname} failed: {e}") from e
    return dest


def _resolve_install(params: dict) -> dict:
    loaders = modrinth.loader_filter(params.get("loader"), params.get("mc"))
    try:
        project = modrinth.resolve_project(params["query"], params.get("slug"), loaders, params.get("mc"))
        if not project:
            raise FixError(f"No confident Modrinth match for \"{params.get('display') or params['query']}\". "
                           "Install it manually.")
        version = modrinth.best_version(project["id"], loaders, params.get("mc"))
    except net.NetError as e:
        raise FixError(f"Modrinth lookup failed: {e}") from e
    if not version:
        raise FixError(f"Modrinth has no {params.get('loader')} build of \"{project['title']}\" "
                       f"for Minecraft {params.get('mc')}")
    f = modrinth.primary_file(version)
    if not f:
        raise FixError(f"Modrinth version of \"{project['title']}\" has no downloadable file")
    h = f.get("hashes", {})
    return {"url": f["url"], "filename": f["filename"], "sha1": h.get("sha1"), "sha512": h.get("sha512"),
            "label": f"{project['title']} {version.get('version_number', '')}".strip()}


def _replace(root: Path, cs_id: str, params: dict) -> list:
    old = _safe(root, params["file"])
    if not old.is_file():
        raise FixError(f"File not found: {params['file']}")
    new = _download_to(root, cs_id, params, old.parent)
    backup = _backup(root, cs_id, old)
    old.unlink()
    return [{"op": "replace", "old": _rel(root, old), "old_backup": backup, "new": _rel(root, new)}]


# ----------------------------------------------------------------------- ignored findings
IGNORE_FILE = "ignored.json"


def _ignore_path(root: Path) -> Path:
    return Path(root) / STATE_DIR / IGNORE_FILE


def ignore_key(code: str, files) -> tuple:
    return (str(code), tuple(sorted(str(f) for f in files or [])))


def load_ignored(root: Path) -> set:
    """Findings (code + affected files) the user chose to hide for this pack."""
    try:
        data = json.loads(_ignore_path(root).read_text(encoding="utf-8"))
        return {ignore_key(e["code"], e.get("files")) for e in data.get("ignored", []) if isinstance(e, dict) and "code" in e}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return set()


def clear_ignored(root: Path) -> int:
    n = len(load_ignored(root))
    try:
        _ignore_path(root).unlink()
    except OSError:
        pass
    return n


def _ignore(root: Path, params: dict) -> dict:
    path = _ignore_path(root)
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    try:
        data = json.loads(previous) if previous else {}
    except ValueError:
        data = {}
    items = [e for e in data.get("ignored", []) if isinstance(e, dict)]
    entry = {"code": str(params["code"]), "files": sorted(str(f) for f in params.get("files", [])),
             "title": str(params.get("title", ""))[:200]}
    if all(ignore_key(e.get("code"), e.get("files")) != ignore_key(entry["code"], entry["files"]) for e in items):
        items.append(entry)
    _state(root)
    path.write_text(json.dumps({"ignored": items}, indent=2), encoding="utf-8")
    return {"op": "ignore", "previous": previous, "title": entry["title"] or entry["code"]}


# ----------------------------------------------------------------------- public API

def apply_actions(root: Path, actions: list, progress: Optional[Callable] = None) -> dict:
    """Apply actions in order. Returns the change set (also saved to disk)."""
    root = Path(root).resolve()
    cs_id = time.strftime("%Y%m%d-%H%M%S")
    sdir = _state(root) / "changesets"
    sdir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (sdir / f"{cs_id}.json").exists():
        n += 1
        cs_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
    entries, errors, seen = [], [], set()
    for i, a in enumerate(actions, 1):
        key = json.dumps(a.to_dict(), sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        if progress:
            progress(f"Applying change {i}/{len(actions)}", i / max(len(actions), 1))
        try:
            k, p = a.kind, a.params
            if k == "disable_mod":
                entries.append(_disable(root, p))
            elif k == "enable_mod":
                entries.append(_enable(root, p))
            elif k == "ignore_finding":
                entries.append(_ignore(root, p))
            elif k == "edit_config":
                entries.append(_apply_edit(root, cs_id, p))
            elif k == "replace_mod":
                entries.extend(_replace(root, cs_id, p))
            elif k in ("install_mod", "install_project"):
                spec = _resolve_install(p) if k == "install_project" else p
                dest = _download_to(root, cs_id, spec, (root / "mods"))
                entries.append({"op": "install", "path": _rel(root, dest), "label": spec.get("label", "")})
            else:
                raise FixError(f"Unknown action: {k}")
        except FixError as e:
            errors.append(str(e))
        except OSError as e:
            errors.append(f"{a.kind}: {e}")
    cs = {"id": cs_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "entries": entries,
          "errors": errors, "undone": False}
    (sdir / f"{cs_id}.json").write_text(json.dumps(cs, indent=2), encoding="utf-8")
    return cs


def list_changesets(root: Path) -> list:
    sdir = Path(root) / STATE_DIR / "changesets"
    out = []
    for f in sdir.glob("*.json") if sdir.is_dir() else []:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue

    def order(cs: dict) -> tuple:  # "…-120000" then "…-120000-2": plain string sorting would put -2 first
        stamp, _, extra = str(cs.get("id", "")).rpartition("-")
        if extra.isdigit() and len(extra) < 6 and stamp.count("-") >= 1:
            return (stamp, int(extra))
        return (str(cs.get("id", "")), 1)

    return sorted(out, key=order)


def undo(root: Path, cs_id: Optional[str] = None) -> dict:
    """Undo a change set (default: the most recent one that has not been undone)."""
    root = Path(root).resolve()
    sets = [c for c in list_changesets(root) if not c.get("undone") and c["entries"]]
    if cs_id:
        sets = [c for c in sets if c["id"] == cs_id]
    if not sets:
        raise FixError("There is nothing to undo.")
    cs = sets[-1]
    problems = []
    for e in reversed(cs["entries"]):
        try:
            if e["op"] == "disable":
                src, dst = _safe(root, e["from"]), _safe(root, e["to"])
                if src.exists():
                    raise FixError(f"Cannot restore {e['from']}: a file with that name exists")
                dst.rename(src)
            elif e["op"] == "enable":
                src, dst = _safe(root, e["from"]), _safe(root, e["to"])
                if src.exists():
                    raise FixError(f"Cannot disable {e['to']} again: {e['from']} exists")
                dst.rename(src)
            elif e["op"] == "install":
                p = _safe(root, e["path"])
                if p.exists():
                    p.unlink()
            elif e["op"] == "replace":
                new, old, bak = _safe(root, e["new"]), _safe(root, e["old"]), _safe(root, e["old_backup"])
                if new.exists():
                    new.unlink()
                shutil.copy2(bak, old)
            elif e["op"] == "ignore":
                path = _ignore_path(root)
                if e.get("previous") is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(e["previous"], encoding="utf-8")
            elif e["op"] == "config":
                shutil.copy2(_safe(root, e["backup"]), _safe(root, e["path"]))
        except (FixError, OSError) as ex:
            problems.append(str(ex))
    cs["undone"] = True
    cs["undo_problems"] = problems
    (root / STATE_DIR / "changesets" / f"{cs['id']}.json").write_text(json.dumps(cs, indent=2), encoding="utf-8")
    return cs


# Folders worth keeping when a user wants a quick "before I tinker" copy. The mods themselves are big and are never
# changed destructively by this tool, so only their names go into the backup.
BACKUP_DIRS = ("config", "defaultconfigs", "kubejs", "scripts", "shaderpacks")
BACKUP_FILES = ("options.txt", "servers.dat", "server.properties")


def backup_configs(root: Path, mod_list: str = "", dest_dir: Optional[Path] = None) -> tuple:
    """Zip the pack's config folders (plus settings files and a text list of the mods) into
    ``.modpack_doctor/backups/``. Returns (zip path, number of files stored)."""
    import zipfile
    root = Path(root)
    dest_dir = Path(dest_dir) if dest_dir else _state(root) / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = dest_dir / f"configs-{stamp}.zip"
    n = 1
    while out.exists():
        n += 1
        out = dest_dir / f"configs-{stamp}-{n}.zip"
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for d in BACKUP_DIRS:
            base = root / d
            if not base.is_dir():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    z.write(p, p.relative_to(root).as_posix())
                    count += 1
        for fname in BACKUP_FILES:
            p = root / fname
            if p.is_file():
                z.write(p, fname)
                count += 1
        if mod_list:
            z.writestr("mod-list.txt", mod_list)
            count += 1
    return out, count

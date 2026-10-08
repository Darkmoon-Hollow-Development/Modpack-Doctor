"""Config-folder checks: syntax validation, rule-based value checks, and memory guidance."""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Optional

from .models import Action, Finding, Fix, PackContext
from .util import lenient_json_loads

MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_CONFIG_FILES = 5000


def validate_config(path: Path) -> Optional[str]:
    """Return an error message if the file is syntactically invalid, else None."""
    ext = path.suffix.lower()
    if ext not in (".toml", ".json"):
        return None
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            return None
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if ext == ".toml":
            tomllib.loads(text)
        elif text.strip():
            lenient_json_loads(text)
    except tomllib.TOMLDecodeError as e:
        return f"Invalid TOML: {e}"
    except ValueError as e:
        return f"Invalid JSON: {e}"
    except OSError:
        return None
    return None


_PROP_RE = re.compile(r"^\s*([^=:\s#!][^=:]*?)\s*[=:]\s*(.*?)\s*$")


def read_properties(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _PROP_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _compare(op: str, current: float, target: float) -> bool:
    return {">": current > target, ">=": current >= target, "<": current < target,
            "<=": current <= target, "==": current == target}.get(op, False)


def check(pack: PackContext, rules: dict) -> list:
    findings: list = []
    root = pack.root
    if root is None:
        return findings

    # 1) syntax of config files
    cfg = root / "config"
    if cfg.is_dir():
        n = 0
        for dirpath, dirnames, filenames in os.walk(cfg):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in sorted(filenames):
                n += 1
                if n > MAX_CONFIG_FILES:
                    break
                full = Path(dirpath) / fn
                err = validate_config(full)
                if err:
                    rel = full.relative_to(root).as_posix()
                    findings.append(Finding(
                        "warning", "config_invalid", "Config",
                        f"{rel} is not valid",
                        f"{err}\nMost mods reset or crash on a config they cannot parse. Fix the syntax or "
                        "delete the file so the mod regenerates it (a backup is recommended).",
                        files=[rel]))

    # 2) rule-based value checks
    for rule in rules.get("config_rules", []):
        fp = root / rule["file"]
        if not fp.is_file() or pack.mod_count < int(rule.get("min_mods", 0)):
            continue
        if rule.get("format", "properties") != "properties":
            continue
        raw = read_properties(fp).get(rule["key"])
        if raw is None:
            continue
        try:
            current = float(raw)
        except ValueError:
            continue
        if _compare(rule.get("op", ">"), current, float(rule["value"])):
            fixes = []
            if "set" in rule:
                fixes.append(Fix(f"Set {rule['key']}={rule['set']} in {rule['file']}",
                                 [Action("edit_config", {"file": rule["file"], "format": "properties",
                                                         "key": rule["key"], "value": str(rule["set"])})]))
            findings.append(Finding(
                rule.get("severity", "info"), "config_value", "Config",
                f"{rule['file']}: {rule['key']} = {raw}",
                rule["message"].format(current=raw), files=[rule["file"]], fixes=fixes))

    # 3) memory guidance (launcher setting, advice only)
    if pack.max_mem_mb:
        need = 0
        for tier in sorted(rules.get("ram_guidance", []), key=lambda t: -t["min_mods"]):
            if pack.mod_count >= tier["min_mods"]:
                need = tier["mb"]
                break
        if need and pack.max_mem_mb < need:
            findings.append(Finding(
                "warning", "ram", "Memory",
                f"Only {pack.max_mem_mb} MB of RAM is allocated for {pack.mod_count} mods",
                f"Packs of this size usually want at least {need} MB. Raise the maximum memory in your launcher's "
                "instance settings (rule of thumb, not a guarantee; avoid giving the game more than about half "
                "of your physical RAM).", ))
    return findings

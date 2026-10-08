"""GUI-independent logic for the desktop app (kept free of tkinter so it can be unit-tested)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import report
from .models import AnalysisResult, Finding, LOADERS, SEVERITY_ORDER

SEVERITY_LABEL = {"error": "Error", "warning": "Warning", "info": "Tip"}
ALL_CATEGORIES = "All categories"


# ----------------------------------------------------------------------- findings

def categories(findings: Iterable[Finding]) -> list:
    return [ALL_CATEGORIES] + sorted({f.category for f in findings})


def filter_findings(findings: Iterable[Finding], severities: Iterable[str], category: Optional[str] = None,
                    text: str = "") -> list:
    """Keep findings whose severity is selected, whose category matches, and that contain every search word."""
    sev = set(severities)
    words = [w for w in (text or "").lower().split() if w]
    out = []
    for f in findings:
        if f.severity not in sev:
            continue
        if category and category != ALL_CATEGORIES and f.category != category:
            continue
        if words:
            hay = " ".join([f.title, f.detail, f.category, *f.mods, *f.files]).lower()
            if not all(w in hay for w in words):
                continue
        out.append(f)
    return out


def recommended_fixes(result: AnalysisResult) -> list:
    """[(finding, fix)] for the first recommended ('auto') fix of every finding that has one."""
    out = []
    for f in result.findings:
        fx = next((x for x in f.fixes if x.auto), None)
        if fx:
            out.append((f, fx))
    return out


def fix_button_label(fix) -> str:
    tags = []
    if fix.auto:
        tags.append("recommended")
    if fix.needs_network:
        tags.append("downloads")
    return fix.label + (f"   ({', '.join(tags)})" if tags else "")


# ----------------------------------------------------------------------- mods table

@dataclass
class ModRow:
    name: str
    mod_id: str
    version: str
    loader: str
    side: str
    file: str
    issue: str  # "", "error", "warning" or "info"
    bundled: int = 0


def file_severity(result: AnalysisResult) -> dict:
    """Worst severity of any finding that mentions each jar (relative path)."""
    worst: dict = {}
    for f in result.findings:
        for rel in f.files:
            if rel not in worst or SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[worst[rel]]:
                worst[rel] = f.severity
    return worst


def mod_rows(result: AnalysisResult) -> list:
    worst = file_severity(result)
    rows = []
    for jar in result.jars:
        if not jar.mods:
            continue
        for m in jar.mods:
            rows.append(ModRow(
                name=m.name or m.mod_id, mod_id=m.mod_id, version=m.version or "?", loader=m.loader,
                side=m.environment if m.environment != "both" else ("client" if m.client_only_hint else "both"),
                file=jar.rel, issue=worst.get(jar.rel, ""), bundled=len(jar.bundled)))
    return rows


def filter_mod_rows(rows: Iterable[ModRow], text: str = "", only_issues: bool = False) -> list:
    words = [w for w in (text or "").lower().split() if w]
    out = []
    for r in rows:
        if only_issues and not r.issue:
            continue
        hay = f"{r.name} {r.mod_id} {r.version} {r.loader} {r.file}".lower()
        if all(w in hay for w in words):
            out.append(r)
    return out


def sort_mod_rows(rows: list, column: str, descending: bool = False) -> list:
    if column == "issue":
        key = lambda r: SEVERITY_ORDER.get(r.issue, 9)  # noqa: E731
    else:
        key = lambda r: str(getattr(r, column)).lower()  # noqa: E731
    return sorted(rows, key=key, reverse=descending)


# ----------------------------------------------------------------------- summary helpers

def suggested_ram_mb(rules: dict, mod_count: int) -> Optional[int]:
    """Memory guidance from the rule database (rule of thumb, for the client)."""
    for tier in sorted(rules.get("ram_guidance", []), key=lambda t: -t["min_mods"]):
        if mod_count >= tier["min_mods"]:
            return int(tier["mb"])
    return None


def format_mb(mb: Optional[int]) -> str:
    if not mb:
        return "n/a"
    return f"{mb / 1024:g} GB"


def score_color(score: int) -> str:
    if score >= 90:
        return "#2e9d4f"
    if score >= 70:
        return "#c99a00"
    if score >= 40:
        return "#e07b00"
    return "#d1342f"


def pack_facts(result: AnalysisResult, rules: dict) -> list:
    """[(label, value)] rows for the summary panel."""
    p = result.pack
    loader = (p.loader or "unknown") + (f" {p.loader_version}" if p.loader_version else "")
    facts = [
        ("Pack", p.name or "?"),
        ("Minecraft", p.mc_version or "unknown"),
        ("Loader", loader),
        ("Side", p.side),
        ("Mods", f"{p.mod_count}" + (f" (+{p.disabled_count} disabled)" if p.disabled_count else "")),
    ]
    ram = suggested_ram_mb(rules, p.mod_count)
    if ram:
        facts.append(("Suggested RAM", f"{format_mb(ram)}+"))
    if p.max_mem_mb:
        facts.append(("Allocated RAM", format_mb(p.max_mem_mb)))
    return facts


def details_text(result: AnalysisResult) -> str:
    """Body of the 'Pack details' tab."""
    p = result.pack
    out = [f"Source: {p.source}"]
    if p.root:
        out.append(f"Folder analysed: {p.root}")
    out.append("")
    if p.notes:
        out.append("Notes")
        out += [f"  - {n}" for n in p.notes]
        out.append("")
    if p.unresolved:
        out.append(f"Files that could not be fetched ({len(p.unresolved)})")
        out += [f"  - {u}" for u in p.unresolved]
        out.append("")
    bundled = sum(len(j.bundled) for j in result.jars)
    out.append(f"{len(result.jars)} jar file(s) scanned, {p.mod_count} with mod metadata, "
               f"{bundled} bundled library mod(s) found inside them.")
    return "\n".join(out)


# ----------------------------------------------------------------------- export

def render_report(result: AnalysisResult, fmt: str) -> str:
    if fmt == "json":
        return report.to_json(result)
    if fmt in ("md", "markdown"):
        return report.to_markdown(result)
    return report.to_text(result, verbose=True)


def format_for_path(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".json": "json", ".md": "md", ".markdown": "md"}.get(ext, "text")


def export_report(result: AnalysisResult, path: str) -> None:
    Path(path).write_text(render_report(result, format_for_path(path)), encoding="utf-8")


# ----------------------------------------------------------------------- settings

def settings_path() -> Path:
    base = Path(os.environ.get("MODPACK_DOCTOR_HOME", Path.home() / ".modpack_doctor"))
    return base / "gui.json"


PERSISTED = ("source", "mc", "loader", "side", "online", "rules", "last_dir", "theme", "restore_tabs")


def load_settings(path: Optional[Path] = None) -> dict:
    try:
        data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k in PERSISTED} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(values: dict, path: Optional[Path] = None) -> None:
    """Best-effort. The CurseForge API key is deliberately never written to disk."""
    p = path or settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({k: v for k, v in values.items() if k in PERSISTED}, indent=2), encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------------------- themes

THEME_CHOICES = ("System", "Light", "Dark")

PALETTES = {
    "light": {
        "bg": "#f3f4f6", "surface": "#ffffff", "field": "#ffffff", "fg": "#1f2328", "muted": "#5b6472",
        "border": "#c9ccd2", "trough": "#e3e5e8", "button": "#e5e7eb", "button_active": "#d5d8dd",
        "accent": "#1d4ed8", "accent_active": "#1e40af", "accent_fg": "#ffffff",
        "select_bg": "#2563eb", "select_fg": "#ffffff",
        "error": "#c62828", "warning": "#8a5100", "info": "#1565c0",
    },
    "dark": {
        "bg": "#1e2227", "surface": "#262b32", "field": "#2b3139", "fg": "#e6e8eb", "muted": "#9aa3af",
        "border": "#3b424b", "trough": "#3a414a", "button": "#323944", "button_active": "#3d4550",
        "accent": "#2563eb", "accent_active": "#1d4ed8", "accent_fg": "#ffffff",
        "select_bg": "#2f5fb3", "select_fg": "#ffffff",
        "error": "#ff7b7b", "warning": "#f0b429", "info": "#6cb2ff",
    },
}


def system_prefers_dark() -> bool:
    """Best-effort check of the operating system's light/dark setting (False when unknown)."""
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=2)
            return r.stdout.strip().lower() == "dark"
        return "dark" in os.environ.get("GTK_THEME", "").lower()
    except Exception:  # noqa: BLE001 - detection must never break the app
        return False


def resolve_theme(preference: Optional[str], dark_probe=None) -> str:
    """'System' / 'Light' / 'Dark' (any case, or junk) -> 'light' or 'dark'."""
    p = (preference or "system").strip().lower()
    if p in ("light", "dark"):
        return p
    return "dark" if (dark_probe or system_prefers_dark)() else "light"


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    chans = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        chans.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two #rrggbb colours (4.5 or more is comfortable for body text)."""
    a, b = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


# ----------------------------------------------------------------------- tabs, saved packs, recents

SIDES = ("auto", "client", "server", "both")
MAX_TABS = 20
MAX_RECENT = 10
MAX_CLOSED = 10
MAX_SAVED = 200


@dataclass
class TabSpec:
    """Everything needed to bring a tab back: what to open and the options it was opened with."""
    source: str = ""
    title: str = ""  # a name the user chose; empty means "derive it from the pack"
    mc: str = ""
    loader: str = "auto"
    side: str = "auto"
    online: bool = False

    def to_dict(self) -> dict:
        return {"source": self.source, "title": self.title, "mc": self.mc, "loader": self.loader,
                "side": self.side, "online": self.online}

    @staticmethod
    def from_dict(d) -> Optional["TabSpec"]:
        """Defensive parse: files on disk may be hand-edited or corrupt, so every field is validated."""
        if not isinstance(d, dict):
            return None
        src = d.get("source", "")
        if not isinstance(src, str):
            return None
        title = d.get("title", "")
        mc = d.get("mc", "")
        loader = d.get("loader", "auto")
        side = d.get("side", "auto")
        return TabSpec(
            source=src.strip()[:4096],
            title=title.strip()[:60] if isinstance(title, str) else "",
            mc=mc.strip() if isinstance(mc, str) and re.fullmatch(r"[0-9A-Za-z.\-_+ ]{0,24}", mc.strip()) else "",
            loader=loader if loader in ("auto",) + LOADERS else "auto",
            side=side if side in SIDES else "auto",
            online=d.get("online") is True)


def norm_source(source: str) -> str:
    """Canonical form of a path for 'is this the same pack?' comparisons."""
    s = (source or "").strip().strip('"')
    return os.path.normcase(os.path.normpath(os.path.expanduser(s))) if s else ""


def default_title(source: str) -> str:
    """'C:\\Games\\Prism\\instances\\My Pack' -> 'My Pack'; 'x/pack.mrpack' -> 'pack'; '' -> 'New tab'."""
    s = (source or "").strip().strip('"').rstrip("\\/")
    if not s:
        return "New tab"
    name = re.split(r"[\\/]", s)[-1] or s
    for ext in (".mrpack", ".zip"):
        if name.lower().endswith(ext) and len(name) > len(ext):
            name = name[: -len(ext)]
    return name


def shorten(text: str, limit: int = 24) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _state_dir() -> Path:
    return Path(os.environ.get("MODPACK_DOCTOR_HOME", Path.home() / ".modpack_doctor"))


def session_path() -> Path:
    return _state_dir() / "tabs.json"


def library_path() -> Path:
    return _state_dir() / "library.json"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data) -> None:
    """Best-effort atomic write: a crash mid-write must not destroy the previous file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def load_session(path: Optional[Path] = None) -> tuple:
    """-> ([TabSpec], active_index). Never raises; returns ([], 0) for a missing or damaged file."""
    data = _read_json(path or session_path())
    if not isinstance(data, dict) or not isinstance(data.get("tabs"), list):
        return [], 0
    specs = [sp for sp in (TabSpec.from_dict(t) for t in data["tabs"]) if sp is not None][:MAX_TABS]
    active = data.get("active", 0)
    active = active if isinstance(active, int) and not isinstance(active, bool) and 0 <= active < len(specs) else 0
    return specs, active


def save_session(specs: list, active: int, path: Optional[Path] = None) -> None:
    _write_json(path or session_path(), {"version": 1, "active": active, "tabs": [s.to_dict() for s in specs[:MAX_TABS]]})


@dataclass
class SavedPack:
    name: str
    spec: TabSpec


@dataclass
class Library:
    saved: list = field(default_factory=list)  # [SavedPack]
    recent: list = field(default_factory=list)  # [source str], newest first


def load_library(path: Optional[Path] = None) -> Library:
    data = _read_json(path or library_path())
    lib = Library()
    if not isinstance(data, dict):
        return lib
    for item in data.get("saved", []) if isinstance(data.get("saved"), list) else []:
        spec = TabSpec.from_dict(item.get("spec") if isinstance(item, dict) else None)
        name = item.get("name") if isinstance(item, dict) else None
        if spec and spec.source and isinstance(name, str) and name.strip():
            lib.saved.append(SavedPack(name.strip()[:60], spec))
    seen = set()
    for src in data.get("recent", []) if isinstance(data.get("recent"), list) else []:
        if isinstance(src, str) and src.strip() and norm_source(src) not in seen:
            seen.add(norm_source(src))
            lib.recent.append(src.strip())
    lib.saved, lib.recent = lib.saved[:MAX_SAVED], lib.recent[:MAX_RECENT]
    return lib


def save_library(lib: Library, path: Optional[Path] = None) -> None:
    _write_json(path or library_path(), {
        "version": 1,
        "saved": [{"name": p.name, "spec": p.spec.to_dict()} for p in lib.saved],
        "recent": lib.recent})


def unique_name(lib: Library, wanted: str, ignore: Optional[SavedPack] = None) -> str:
    base = (wanted or "").strip()[:60] or "Saved pack"
    taken = {p.name.lower() for p in lib.saved if p is not ignore}
    name, n = base, 1
    while name.lower() in taken:
        n += 1
        name = f"{base} ({n})"
    return name


def add_saved(lib: Library, name: str, spec: TabSpec) -> SavedPack:
    """Save a pack. Saving the same folder/file again updates its entry instead of adding a duplicate."""
    spec = TabSpec.from_dict(spec.to_dict()) or spec
    spec.title = ""  # the saved name is what identifies it
    for p in lib.saved:
        if norm_source(p.spec.source) == norm_source(spec.source):
            p.name, p.spec = unique_name(lib, name, ignore=p), spec
            return p
    p = SavedPack(unique_name(lib, name), spec)
    lib.saved.append(p)
    del lib.saved[:-MAX_SAVED]
    return p


def remove_saved(lib: Library, pack: SavedPack) -> None:
    if pack in lib.saved:
        lib.saved.remove(pack)


def rename_saved(lib: Library, pack: SavedPack, new_name: str) -> bool:
    new_name = (new_name or "").strip()[:60]
    if not new_name or any(p is not pack and p.name.lower() == new_name.lower() for p in lib.saved):
        return False
    pack.name = new_name
    return True


def add_recent(lib: Library, source: str, limit: int = MAX_RECENT) -> None:
    source = (source or "").strip().strip('"')
    if not source:
        return
    key = norm_source(source)
    lib.recent = [source] + [r for r in lib.recent if norm_source(r) != key]
    del lib.recent[limit:]

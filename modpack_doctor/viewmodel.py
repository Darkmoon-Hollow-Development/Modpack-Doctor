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

from . import launchers, report
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
        *([("Launcher", launchers.label(p.launcher))] if p.launcher else []),
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


def support_summary(result: AnalysisResult, limit: int = 12) -> str:
    """A short plain-text summary that is easy to paste into a Discord/forum support request."""
    pack = result.pack
    lines = [f"Modpack Doctor report for {pack.name or 'modpack'}",
             f"Minecraft {pack.mc_version or '?'} | {pack.loader or '?'} | {len(mod_rows(result))} mods | "
             f"health {result.score}/100"]
    order = {"error": 0, "warning": 1, "info": 2}
    fs = sorted(result.findings, key=lambda f: order.get(f.severity, 3))
    if not fs:
        lines.append("No problems found.")
    else:
        lines.append(f"{sum(1 for f in fs if f.severity == 'error')} errors, "
                     f"{sum(1 for f in fs if f.severity == 'warning')} warnings, "
                     f"{sum(1 for f in fs if f.severity == 'info')} notes")
        lines.append("")
        for f in fs[:limit]:
            lines.append(f"- [{f.severity.upper()}] {f.title}")
        if len(fs) > limit:
            lines.append(f"- ... and {len(fs) - limit} more")
    return "\n".join(lines) + "\n"


def render_mod_list(result: AnalysisResult, fmt: str = "csv") -> str:
    """The mod table as CSV (opens in Excel) or a Markdown table (for forum posts / GitHub)."""
    import csv
    import io
    rows = sort_mod_rows(mod_rows(result), "name")
    head = ["Name", "Mod id", "Version", "Loader", "Side", "File", "Issue"]
    data = [[r.name, r.mod_id, r.version, r.loader, r.side, r.file, r.issue] for r in rows]
    if fmt in ("txt", "text"):
        title = result.pack.name or "Modpack"
        lines = [f"{title} - {len(rows)} mods", ""]
        for r in rows:
            ver = f" {r.version}" if r.version and r.version != "?" else ""
            lines.append(f"{r.name}{ver}  [{r.mod_id}]  ({r.file})")
        return "\n".join(lines) + "\n"
    if fmt in ("md", "markdown"):
        esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        out += ["| " + " | ".join(esc(c) for c in row) + " |" for row in data]
        return "\n".join(out) + "\n"
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    w.writerows(data)
    return buf.getvalue()


def export_mod_list(result: AnalysisResult, path: str) -> int:
    ext = Path(path).suffix.lower()
    fmt = "md" if ext in (".md", ".markdown") else "txt" if ext == ".txt" else "csv"
    # utf-8-sig so Excel on Windows reads accented mod names correctly
    Path(path).write_text(render_mod_list(result, fmt), encoding="utf-8-sig" if fmt == "csv" else "utf-8")
    return len(mod_rows(result))


SHORTCUTS = (
    ("F5", "Analyze"),
    ("Ctrl+O", "Open instance folder"),
    ("Ctrl+Shift+O", "Open pack file (.mrpack / .zip)"),
    ("Ctrl+S", "Export report"),
    ("Ctrl+M", "Open mods folder"),
    ("Ctrl+Shift+C", "Open config folder"),
    ("Ctrl+D", "Save this pack"),
    ("Ctrl+T", "New tab"),
    ("Ctrl+W", "Close tab"),
    ("Ctrl+Shift+T", "Reopen closed tab"),
    ("Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous tab"),
    ("Ctrl+L", "Analyze the latest crash report"),
)


def shortcuts_text() -> str:
    width = max(len(k) for k, _ in SHORTCUTS)
    return "\n".join(f"{k.ljust(width)}   {v}" for k, v in SHORTCUTS)


# ----------------------------------------------------------------------- settings

def settings_path() -> Path:
    base = Path(os.environ.get("MODPACK_DOCTOR_HOME", Path.home() / ".modpack_doctor"))
    return base / "gui.json"


PERSISTED = ("source", "mc", "loader", "side", "online", "rules", "last_dir", "theme", "restore_tabs", "show_options", "ui_version")


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

THEME_CHOICES = ("Minecraft", "System", "Light", "Dark")

PALETTES = {
    "light": {
        "page": "#e7eaf0", "bg": "#ffffff", "surface": "#ffffff", "field": "#f1f3f8", "fg": "#1b1f27",
        "muted": "#586274", "border": "#e1e5ec", "trough": "#e6e8ec", "button": "#eef1f6", "button_active": "#e0e5ee",
        "accent": "#2563eb", "accent_active": "#1d4ed8", "accent_fg": "#ffffff",
        "select_bg": "#dbe7ff", "select_fg": "#10203f",
        "error": "#c62828", "warning": "#8a5100", "info": "#1565c0",
        "blocky": False, "hi": "#ffffff", "lo": "#c9cfdb", "edge": "#e1e5ec",
    },
    "dark": {
        "page": "#0f1217", "bg": "#1b1f26", "surface": "#1b1f26", "field": "#252a33", "fg": "#e8eaed",
        "muted": "#9aa3af", "border": "#2a303a", "trough": "#2e3540", "button": "#262c36", "button_active": "#323946",
        "accent": "#2f68ee", "accent_active": "#2757d6", "accent_fg": "#ffffff",
        "select_bg": "#27406f", "select_fg": "#ffffff",
        "error": "#ff7b7b", "warning": "#f0b429", "info": "#6cb2ff",
        "blocky": False, "hi": "#2a303a", "lo": "#0f1217", "edge": "#2a303a",
    },
    # Original Minecraft-inspired look: dirt background, dark stone panels, grass-green primary buttons.
    # (No Mojang artwork, fonts or logos are used; "hi"/"lo" are the bevel colours of the blocky widgets.)
    "minecraft": {
        "page": "#4a3422", "bg": "#313131", "surface": "#262626", "field": "#1c1c1c", "fg": "#f2f2f2",
        "muted": "#bdbdbd", "border": "#0b0b0b", "trough": "#1c1c1c", "button": "#6b6b6b", "button_active": "#585b94",
        "accent": "#3b7d27", "accent_active": "#2f6a1e", "accent_fg": "#ffffff",
        "select_bg": "#2d5f1e", "select_fg": "#ffffff",
        "error": "#ff6b6b", "warning": "#ffd24a", "info": "#6ec6ff",
        "blocky": True, "hi": "#a8a8a8", "lo": "#3c3c3c", "edge": "#0b0b0b",
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
    """'System' / 'Light' / 'Dark' / 'Minecraft' (any case, or junk) -> 'light', 'dark' or 'minecraft'."""
    p = (preference or "system").strip().lower()
    if p in ("light", "dark", "minecraft"):
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
    launcher: str = "auto"

    def to_dict(self) -> dict:
        return {"source": self.source, "title": self.title, "mc": self.mc, "loader": self.loader,
                "side": self.side, "online": self.online, "launcher": self.launcher}

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
            online=d.get("online") is True,
            launcher=d.get("launcher") if d.get("launcher") in launchers.KEYS else "auto")


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


# ---------------------------------------------------------------------------- pack folders
def pack_folders(result: Optional[AnalysisResult] = None, source: str = "") -> dict:
    """The folders worth jumping to for a pack: ``{"pack": Path, "mods": Path, "config": Path}``.

    Taken from the scan result when there is one, otherwise worked out from a local source folder.
    Only folders that exist are included (a pack with no ``config`` yet simply has no "config" key).
    """
    root = mods = None
    if result is not None and result.pack.root is not None:
        root, mods = result.pack.root, result.pack.mods_dir
    elif source.strip():
        from .sources import SourceError, find_instance_root
        try:
            root, mods = find_instance_root(Path(source.strip().strip('"')).expanduser())
        except (SourceError, OSError):
            return {}
    out: dict = {}
    for key, path in (("pack", root), ("mods", mods), ("config", root / "config" if root else None)):
        try:
            if path is not None and Path(path).is_dir():
                out[key] = Path(path)
        except OSError:
            pass
    return out


# ---------------------------------------------------------------------------- developer view
METADATA_NAMES = ("fabric.mod.json", "quilt.mod.json", "META-INF/neoforge.mods.toml", "META-INF/mods.toml",
                  "mcmod.info", "META-INF/MANIFEST.MF", "pack.mcmeta")
MAX_VIEW_BYTES = 512 * 1024


def _jar_on_disk(root: Path, rel: str) -> Optional[Path]:
    """The jar for a finding's relative path (also when it has since been disabled), kept inside ``root``."""
    base = Path(root).resolve()
    for name in (rel, rel + ".disabled"):
        p = (base / name).resolve()
        if (p == base or base in p.parents) and p.is_file():
            return p
    return None


def jar_metadata_files(root: Path, rel: str) -> list:
    """[(name, size)] of the metadata files inside a jar, plus any jar-in-jar libraries (listed, not opened)."""
    import zipfile
    p = _jar_on_disk(root, rel)
    if p is None:
        return []
    try:
        with zipfile.ZipFile(p) as zf:
            info = {i.filename: i.file_size for i in zf.infolist()}
    except (zipfile.BadZipFile, OSError):
        return []
    out = [(n, info[n]) for n in METADATA_NAMES if n in info]
    out += [(n, info[n]) for n in sorted(info) if n.lower().endswith(".jar") and
            n.startswith(("META-INF/jars/", "META-INF/jarjar/"))]
    return out


def read_jar_text(root: Path, rel: str, name: str) -> str:
    """Text of one file inside a jar (size-limited, decoded leniently). Raises OSError/KeyError on problems."""
    import zipfile
    p = _jar_on_disk(root, rel)
    if p is None:
        raise OSError(f"{rel} was not found (moved or deleted since the scan?)")
    with zipfile.ZipFile(p) as zf:
        info = zf.getinfo(name)
        if name.lower().endswith(".jar"):
            return f"(This is a library jar bundled inside the mod: {info.file_size} bytes. It is scanned for mods too, but not opened here.)"
        with zf.open(info) as f:
            data = f.read(MAX_VIEW_BYTES + 1)
    text = data[:MAX_VIEW_BYTES].decode("utf-8-sig", errors="replace")
    return text + ("\n\n[... file cut off at 512 KB ...]" if len(data) > MAX_VIEW_BYTES else "")


def error_line(message: str) -> Optional[int]:
    """Line number mentioned by a TOML/JSON parse error ('at line 12, column 3' / 'line 3 column 9'), if any."""
    m = re.search(r"line\s+(\d+)", message or "", re.I)
    return int(m.group(1)) if m else None


def _redact(text: str) -> str:
    home = str(Path.home())
    return text.replace(home, "~") if home and home != "/" else text


def debug_info(result: AnalysisResult, finding) -> str:
    """Everything a developer needs to reproduce or explain one finding, as JSON (home folder redacted)."""
    from . import __version__
    p = result.pack
    jars = {j.rel: j for j in result.jars}
    files = []
    for rel in finding.files[:20]:
        j = jars.get(rel)
        if j is None:
            files.append({"file": rel, "scanned": False})
            continue
        try:
            sha1 = j.sha1
        except OSError:
            sha1 = None
        files.append({"file": rel, "size": j.size, "sha1": sha1, "errors": j.errors, "metadata_files": j.meta_files,
                      "mods": [{"id": m.mod_id, "version": m.version, "loader": m.loader, "source": m.source,
                                "environment": m.environment} for m in j.mods],
                      "bundled_mods": [{"id": m.mod_id, "version": m.version, "loader": m.loader}
                                       for m in j.bundled[:30]]})
    doc = {"tool": {"name": "Modpack Doctor", "version": __version__, "python": sys.version.split()[0],
                    "platform": sys.platform},
           "pack": {"name": p.name, "minecraft": p.mc_version, "loader": p.loader, "loader_version": p.loader_version,
                    "side": p.side, "mods": p.mod_count, "notes": p.notes, "source": p.source},
           "finding": finding.to_dict(), "files": files}
    return _redact(json.dumps(doc, indent=2, default=str))

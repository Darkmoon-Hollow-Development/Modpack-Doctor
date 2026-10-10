"""Read a Minecraft crash report (or latest.log) and say, in plain words, what went wrong.

Pure Python, no Tk. It understands the usual shapes of Forge/NeoForge and Fabric/Quilt crash
reports: the "Suspected Mod(s)" line, missing-dependency lists, Mixin failures, out-of-memory
errors and Java-version mismatches. It is a best-effort reader of text that varies between
loader versions, so the result is a pointer, not a verdict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MAX_BYTES = 6 * 1024 * 1024
JAVA_BY_CLASSFILE = {52: 8, 53: 9, 54: 10, 55: 11, 56: 12, 57: 13, 58: 14, 59: 15, 60: 16, 61: 17,
                     62: 18, 63: 19, 64: 20, 65: 21, 66: 22, 67: 23, 68: 24, 69: 25}
IGNORED_IDS = {"minecraft", "java", "forge", "neoforge", "fabricloader", "fabric", "quilt_loader", "none", ""}


@dataclass
class Suspect:
    mod_id: str
    name: str = ""
    version: str = ""
    reasons: list = field(default_factory=list)
    jar: Optional[str] = None  # path of the matching jar relative to the pack, when we found it

    @property
    def label(self) -> str:
        base = self.name or self.mod_id
        return f"{base} ({self.mod_id})" if self.name and self.name != self.mod_id else base


@dataclass
class CrashReport:
    name: str = ""
    kind: str = "crash"  # crash | log
    description: str = ""
    exception: str = ""
    minecraft: str = ""
    suspects: list = field(default_factory=list)
    missing: list = field(default_factory=list)  # [{"mod": requester, "needs": id, "detail": text}]
    advice: list = field(default_factory=list)  # plain-language lines, most important first
    problems: list = field(default_factory=list)  # short tags: oom, java_version, mixin, missing_dep, ...

    @property
    def understood(self) -> bool:
        return bool(self.suspects or self.missing or self.problems)

    def to_text(self) -> str:
        out = [f"File: {self.name}"]
        if self.description:
            out.append(f"What the game was doing: {self.description}")
        if self.exception:
            out.append(f"Error: {self.exception}")
        out.append("")
        out.extend(self.advice or ["Nothing here points at a specific mod."])
        return "\n".join(out)


# ---------------------------------------------------------------------------- reading

def find_latest(root: Path) -> Optional[Path]:
    """Newest crash report in ``crash-reports/``, else ``logs/latest.log``, else None."""
    root = Path(root)
    d = root / "crash-reports"
    try:
        reports = sorted((p for p in d.glob("crash-*.txt") if p.is_file()), key=lambda p: p.stat().st_mtime)
    except OSError:
        reports = []
    if reports:
        return reports[-1]
    log = root / "logs" / "latest.log"
    return log if log.is_file() else None


def read_text(path: Path) -> str:
    """Read the file (the *end* of it if it is huge, since that is where the crash is)."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > MAX_BYTES:
            f.seek(size - MAX_BYTES)
        data = f.read()
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------------------- parsing

_SUSPECT_HEAD = re.compile(r"^\s*Suspected Mods?(?:\(s\))?:\s*(.*)$", re.I | re.M)
_SUSPECT_ITEM = re.compile(r"^\s*(?P<name>[^()\n]*?)\s*\((?P<id>[A-Za-z0-9_.\-]+)\)(?:\s*,?\s*Version:\s*(?P<ver>\S+))?")
_FORGE_DEP = re.compile(r"Mod ID:\s*'(?P<need>[^']+)',\s*Requested by:\s*'(?P<by>[^']+)'"
                        r"(?:,\s*Expected range:\s*'(?P<rng>[^']*)')?(?:,\s*Actual version:\s*'(?P<act>[^']*)')?")
_FABRIC_REQ = re.compile(r"Mod '(?P<name>[^']+)' \((?P<id>[^)]+)\) (?P<ver>\S+) (?P<verb>requires|recommends|conflicts with|"
                         r"is incompatible with|breaks) (?P<rest>.*)$", re.M)
_FABRIC_NEED = re.compile(r"(?:any version|version[^']*?)\s*of mod '(?P<name>[^']+)' \((?P<id>[^)]+)\)")
_FABRIC_FIX = re.compile(r"^\s*-\s*((?:Install|Replace|Remove|Upgrade|Update)\b.*)$", re.M)
_MIXIN_MOD = re.compile(r"(?:Mixin (?:apply|transformation)[^\n]*?(?:for mod|from mod)\s+|from mod\s+)(?P<id>[A-Za-z0-9_.\-]+)")
_MIXIN_CFG = re.compile(r"(?P<cfg>[A-Za-z0-9_.\-]+\.mixins?\.json)")
_MOD_FILE = re.compile(r"^\s*Mod File:\s*(?P<f>.+)$", re.M)
_MOD_SECTION = re.compile(r"^-- MOD (?P<id>[A-Za-z0-9_.\-]+) --\s*$", re.M)
_CLASSFILE = re.compile(r"class file version (\d+)")
_UP_TO = re.compile(r"up to (?:class file versions? )?(\d+)")


def _first_exception(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^[\w.$]+(?:Exception|Error|Throwable)(?::.*)?$", s):
            return s[:300]
    return ""


def _suspects_from_header(text: str) -> list:
    out = []
    m = _SUSPECT_HEAD.search(text)
    if not m:
        return out
    chunk = [m.group(1)]
    rest = text[m.end():].splitlines()
    for line in rest:
        if not line.strip() or not line.startswith((" ", "\t")):
            break
        if re.match(r"^\s*(Mod List|Stacktrace|Loaded Modules|Launched|Backend|Virtual)", line):
            break
        chunk.append(line)
    for line in chunk:
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        # Forge may put several on one line separated by blank lines or '; ' - take them one by one.
        for piece in re.split(r"\s{2,}|;", line):
            mm = _SUSPECT_ITEM.match(piece.strip())
            if mm and mm.group("id").lower() not in IGNORED_IDS:
                out.append(Suspect(mm.group("id"), mm.group("name").strip(), mm.group("ver") or ""))
    return out


def parse(text: str, name: str = "") -> CrashReport:
    # Minified or binary junk can have enormous "lines"; cut them so no pattern below can run away.
    text = "\n".join(l[:2000] for l in text.splitlines())
    kind = "crash" if "Minecraft Crash Report" in text[:2000] or name.startswith("crash-") else "log"
    rep = CrashReport(name=name, kind=kind)
    m = re.search(r"^Description:\s*(.+)$", text, re.M)
    if m:
        rep.description = m.group(1).strip()
    rep.exception = _first_exception(text)
    m = re.search(r"Minecraft Version:\s*(\S+)", text)
    if m:
        rep.minecraft = m.group(1)

    by_id: dict = {}

    def suspect(mod_id: str, name_: str = "", version: str = "") -> Suspect:
        key = mod_id.lower()
        s = by_id.get(key)
        if s is None:
            s = by_id[key] = Suspect(mod_id, name_, version)
            rep.suspects.append(s)
        else:
            s.name = s.name or name_
            s.version = s.version or version
        return s

    def reason(s: Suspect, why: str) -> None:
        if why not in s.reasons:
            s.reasons.append(why)

    for s in _suspects_from_header(text):
        reason(suspect(s.mod_id, s.name, s.version), "The crash report lists it as the suspected mod.")

    # Forge/NeoForge: "-- MOD id --" sections with a failure message, and Mod File lines.
    for sec in _MOD_SECTION.finditer(text):
        if sec.group("id").lower() in IGNORED_IDS:
            continue
        chunk = text[sec.end(): sec.end() + 800]
        fm = re.search(r"Failure message:\s*(.+)", chunk)
        mf = _MOD_FILE.search(chunk)
        s = suspect(sec.group("id"))
        reason(s, "Failed to load: " + fm.group(1).strip()[:200] if fm else "The crash report has a section for this mod.")
        if mf:
            s.jar = Path(mf.group("f").strip().replace("\\", "/")).name  # filename for now; match() resolves it

    # Forge missing / wrong-version dependencies.
    for dm in _FORGE_DEP.finditer(text):
        need, by = dm.group("need"), dm.group("by")
        act = dm.group("act") or ""
        missing = "MISSING" in act.upper() or not act
        detail = (f"needs {need}, which is not installed" if missing else
                  f"needs {need} {dm.group('rng') or ''}, but version {act} is installed").replace("  ", " ")
        rep.missing.append({"mod": by, "needs": need, "detail": detail})
        reason(suspect(by), detail.capitalize() + ".")

    # Fabric / Quilt dependency lines.
    for fm in _FABRIC_REQ.finditer(text):
        verb, rest = fm.group("verb"), fm.group("rest")
        mid = fm.group("id")
        if verb in ("requires", "recommends"):
            if verb == "recommends":
                continue
            nm = _FABRIC_NEED.search(rest)
            need = nm.group("id") if nm else rest.strip(" .")[:60]
            miss = "missing" in rest
            detail = f"needs {need}" + (", which is not installed" if miss else f" ({rest.strip(' .')[:120]})")
            item = {"mod": mid, "needs": need, "detail": detail}
            if item not in rep.missing:
                rep.missing.append(item)
            reason(suspect(mid, fm.group("name"), fm.group("ver")), detail.capitalize() + ".")
        else:
            reason(suspect(mid, fm.group("name"), fm.group("ver")), f"{verb.capitalize()} another mod: {rest.strip(' .')[:140]}.")
    fixes = []
    for fx in _FABRIC_FIX.finditer(text):
        t = fx.group(1).strip()
        if t not in fixes:
            fixes.append(t)

    # Mixin failures.
    for line in text.splitlines():
        if "Mixin" not in line or not re.search(r"failed|FAILED|error", line):
            continue
        who = _MIXIN_MOD.search(line)
        cfg = _MIXIN_CFG.search(line)
        if who and who.group("id").lower() not in IGNORED_IDS:
            reason(suspect(who.group("id")), "A Mixin from this mod failed to apply, which usually means it does not "
                   "match this version of Minecraft or another mod.")
            if "mixin" not in rep.problems:
                rep.problems.append("mixin")
        elif cfg:
            stem = cfg.group("cfg").split(".")[0]
            if "mixin" not in rep.problems:
                rep.problems.append("mixin")
            rep.advice.append(f"A Mixin config ({cfg.group('cfg')}) failed to apply. Its name usually starts with the mod "
                              f"that owns it ('{stem}').")

    low = text
    if "OutOfMemoryError" in low:
        rep.problems.append("oom")
    uc = _CLASSFILE.search(low)
    if "UnsupportedClassVersionError" in low and uc:
        need = JAVA_BY_CLASSFILE.get(int(uc.group(1)))
        have = _UP_TO.search(low[uc.start():])
        have_j = JAVA_BY_CLASSFILE.get(int(have.group(1))) if have else None
        rep.problems.append("java_version")
        msg = "A mod needs a newer Java than the one running the game"
        if need:
            msg += f" (it needs Java {need}" + (f", the game is on Java {have_j})" if have_j else ")")
        rep.advice.insert(0, msg + ". Switch the launcher's Java to the version the mod requires.")
    if "oom" in rep.problems:
        rep.advice.insert(0, "The game ran out of memory. Give it more RAM in your launcher "
                          "(for a big pack, 6-8 GB), or remove heavy mods. This is not a bad mod.")
    if rep.missing:
        rep.problems.append("missing_dep")
    if "NoSuchMethodError" in low or "NoSuchFieldError" in low or "NoClassDefFoundError" in low:
        rep.problems.append("version_mismatch")
        if not rep.missing:
            rep.advice.append("A mod called code that does not exist in the version it found "
                              "(NoSuchMethod/NoClassDefFound). That almost always means two mods, or a mod and its library, "
                              "are from different versions.")

    # Plain-language lines, most actionable first.
    lines = []
    for item in rep.missing:
        lines.append(f"{item['mod']} {item['detail']}.")
    for t in fixes[:6]:
        lines.append(f"Fabric suggests: {t}")
    for s in rep.suspects:
        if s.reasons and not s.reasons[0].lower().startswith("needs"):
            lines.append(f"{s.label}: {s.reasons[0]}")
    rep.advice = rep.advice[:1] + [l for l in lines if l not in rep.advice] + rep.advice[1:]
    seen, uniq = set(), []
    for l in rep.advice:
        if l not in seen:
            seen.add(l)
            uniq.append(l)
    rep.advice = uniq
    if not rep.advice:
        rep.advice = ["Nothing here points at a specific mod. Look at the 'Error' line above, or send the whole file to "
                      "the modpack's author."]
    return rep


def analyze_file(path: Path) -> CrashReport:
    path = Path(path)
    return parse(read_text(path), path.name)


# ---------------------------------------------------------------------------- matching to jars

def match_jars(report: CrashReport, jars: list) -> CrashReport:
    """Fill ``Suspect.jar`` from a scan result's jars (list of JarInfo). Mutates and returns the report."""
    by_id: dict = {}
    by_name: dict = {}
    for j in jars:
        by_name[Path(j.rel).name.lower()] = j.rel
        for mi in j.mods:
            by_id.setdefault(mi.mod_id.lower(), j.rel)
            for p in mi.provides:
                by_id.setdefault(str(p).lower(), j.rel)
    for s in report.suspects:
        hint = (s.jar or "").lower()
        s.jar = by_name.get(hint) or by_id.get(s.mod_id.lower())
    return report


# ---------------------------------------------------------------------------- pasted text and shared links

SHARE_HOSTS = {"api.mclo.gs"}
_SHARE_RE = re.compile(r"^\s*(?:https?://)?(?:www\.)?(?:mclo\.gs|api\.mclo\.gs/1/raw)/(?P<id>[A-Za-z0-9]{4,32})\s*$")


def share_link_id(text: str) -> Optional[str]:
    """If ``text`` is just an mclo.gs link (what crash-assistant style tools and Discord helpers share), its id."""
    m = _SHARE_RE.match(text or "")
    return m.group("id") if m else None


def fetch_shared(link_id: str) -> str:
    """Download the raw text of an mclo.gs log. Only api.mclo.gs is contacted; raises net.NetError on any problem."""
    import tempfile
    from . import net
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "log.txt"
        net.download(f"https://api.mclo.gs/1/raw/{link_id}", dest, allowed_hosts=SHARE_HOSTS, max_bytes=MAX_BYTES)
        return read_text(dest)


def analyze_pasted(text: str) -> CrashReport:
    """Analyze text that was pasted in (a whole crash report, part of a log, or an mclo.gs link)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("There is nothing to analyze.")
    link = share_link_id(text)
    if link:
        return parse(fetch_shared(link), f"mclo.gs/{link}")
    return parse(text, "pasted text")

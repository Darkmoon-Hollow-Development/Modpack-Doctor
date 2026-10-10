"""Data classes shared by every module."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

LOADERS = ("forge", "neoforge", "fabric", "quilt")
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Dependency:
    mod_id: str
    kind: str  # required | optional | recommended | suggested | breaks | conflicts
    version_range: Any = None
    syntax: str = "fabric"  # "maven" (Forge/NeoForge) or "fabric" (Fabric/Quilt)
    side: str = "both"  # both | client | server
    path: str = ""  # where it was declared, e.g. 'depends."fabric-api"' (for the developer trace)


@dataclass
class ModInfo:
    mod_id: str
    version: Optional[str]
    loader: str
    name: str = ""
    provides: list = field(default_factory=list)
    deps: list = field(default_factory=list)
    environment: str = "both"  # both | client | server
    client_only_hint: bool = False
    description: str = ""
    source: str = ""  # the metadata file inside the jar this came from, e.g. META-INF/mods.toml
    note: str = ""  # something odd about how it was read (e.g. recovered from invalid TOML)

    @property
    def display(self) -> str:
        label = self.name or self.mod_id
        return f"{label} {self.version}" if self.version else label


@dataclass
class JarInfo:
    path: Path
    rel: str
    size: int
    mods: list = field(default_factory=list)  # top-level ModInfo entries
    bundled: list = field(default_factory=list)  # jar-in-jar ModInfo entries
    errors: list = field(default_factory=list)
    meta_files: list = field(default_factory=list)  # [{file, ok, error, mods}] for every metadata file we tried
    _sha1: Optional[str] = None

    @property
    def sha1(self) -> str:
        if self._sha1 is None:
            h = hashlib.sha1()
            with open(self.path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            self._sha1 = h.hexdigest()
        return self._sha1

    @property
    def loaders(self) -> set:
        return {m.loader for m in self.mods}


@dataclass
class Action:
    """One concrete change. ``kind`` is one of:
    disable_mod, enable_mod, install_project, install_mod, replace_mod, edit_config, ignore_finding."""

    kind: str
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "params": self.params}

    @staticmethod
    def from_dict(d: dict) -> "Action":
        return Action(d["kind"], dict(d.get("params", {})))


@dataclass
class Fix:
    label: str
    actions: list = field(default_factory=list)
    auto: bool = False  # included in "apply recommended fixes"
    needs_network: bool = False

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "auto": self.auto,
            "needs_network": self.needs_network,
            "actions": [a.to_dict() for a in self.actions],
        }


@dataclass
class Finding:
    severity: str  # error | warning | info
    code: str
    category: str
    title: str
    detail: str = ""
    mods: list = field(default_factory=list)
    files: list = field(default_factory=list)  # jar paths relative to the instance root
    fixes: list = field(default_factory=list)
    trace: list = field(default_factory=list)  # developer view: which check, jar, metadata file and key produced this

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "category": self.category,
            "title": self.title,
            "detail": self.detail,
            "mods": self.mods,
            "files": self.files,
            "fixes": [f.to_dict() for f in self.fixes],
            "trace": self.trace,
        }


@dataclass
class PackContext:
    name: str = ""
    root: Optional[Path] = None
    mods_dir: Optional[Path] = None
    mc_version: Optional[str] = None
    loader: Optional[str] = None
    loader_version: Optional[str] = None
    side: str = "both"  # client | server | both
    source: str = ""
    max_mem_mb: Optional[int] = None
    notes: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)  # pack entries that could not be fetched
    disabled_count: int = 0
    mod_count: int = 0


@dataclass
class AnalysisResult:
    pack: PackContext
    jars: list
    findings: list
    score: int = 100
    verdict: str = ""

    def counts(self) -> dict:
        c = {"error": 0, "warning": 0, "info": 0}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    def to_dict(self) -> dict:
        p = self.pack
        return {
            "pack": {
                "name": p.name,
                "root": str(p.root) if p.root else None,
                "minecraft": p.mc_version,
                "loader": p.loader,
                "loader_version": p.loader_version,
                "side": p.side,
                "source": p.source,
                "mods": p.mod_count,
                "disabled_files": p.disabled_count,
                "notes": p.notes,
                "unresolved": p.unresolved,
            },
            "score": self.score,
            "verdict": self.verdict,
            "counts": self.counts(),
            "findings": [f.to_dict() for f in self.findings],
        }


def score_findings(findings: list) -> tuple:
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    score = max(0, 100 - 8 * errors - 2 * warnings)
    if errors == 0 and warnings == 0:
        verdict = "Healthy: no compatibility problems found"
    elif score >= 90:
        verdict = "Mostly healthy: minor issues"
    elif score >= 70:
        verdict = "Needs attention"
    elif score >= 40:
        verdict = "Likely to have problems at launch"
    else:
        verdict = "Very unlikely to launch as is"
    return score, verdict

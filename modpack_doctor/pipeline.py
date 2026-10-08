"""High-level entry points used by both the CLI and the GUI."""
from __future__ import annotations

import json
from typing import Callable, Optional

from . import analyzer, configcheck, enrich, fixer
from .models import Action, AnalysisResult, score_findings
from .rules import load_rules
from .sources import Options, open_source


def run_analysis(source: str, opts: Options, *, online: bool = False, rules_path: Optional[str] = None,
                 progress: Optional[Callable] = None) -> AnalysisResult:
    rules = load_rules(rules_path)
    pack, jars = open_source(source, opts, progress)
    if progress:
        progress("Analysing compatibility", None)
    findings = analyzer.analyze(pack, jars, rules)
    findings.extend(configcheck.check(pack, rules))
    if pack.unresolved:
        from .models import Finding
        lines = "\n".join(f"  - {u}" for u in pack.unresolved[:40])
        findings.append(Finding("warning", "unresolved", "Pack contents",
                                f"{len(pack.unresolved)} file(s) from the pack could not be fetched",
                                "The analysis does not cover these:\n" + lines))
    if online:
        enrich.add_update_findings(pack, jars, findings, progress)
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f.severity], f.category, f.title))
    score, verdict = score_findings(findings)
    return AnalysisResult(pack, jars, findings, score, verdict)


def reanalyze(prev: AnalysisResult, *, online: bool = False, rules_path: Optional[str] = None,
              progress: Optional[Callable] = None) -> AnalysisResult:
    """Re-check the folder a previous analysis worked on (e.g. after fixes), keeping its context.

    For .mrpack/.zip sources this re-reads the unpacked workspace instead of downloading again.
    """
    p = prev.pack
    if p.root is None:
        raise ValueError("The previous analysis has no folder to re-check")
    opts = Options(mc=p.mc_version, loader=p.loader, side=p.side, loader_version=p.loader_version,
                   name=p.name, source_label=p.source, unresolved=list(p.unresolved), max_mem_mb=p.max_mem_mb)
    res = run_analysis(str(p.root), opts, online=online, rules_path=rules_path, progress=progress)
    res.pack.notes[:0] = [n for n in p.notes if n not in res.pack.notes]
    return res


def recommended_actions(result: AnalysisResult, allow_network: bool = True) -> list:
    """The first 'auto' fix of every finding that has one (optionally skipping fixes that download files)."""
    actions: list = []
    for f in result.findings:
        fix = next((x for x in f.fixes if x.auto), None)
        if fix and (allow_network or not fix.needs_network):
            actions.extend(fix.actions)
    return actions


def autofix(prev: AnalysisResult, *, online: bool = False, rules_path: Optional[str] = None,
            progress: Optional[Callable] = None, max_passes: int = 3, allow_network: bool = True) -> dict:
    """Apply the recommended fixes, re-check, and repeat while that keeps finding more to fix.

    Fixing one problem can expose or resolve others (for example a downloaded library whose own
    dependency is missing), so several short rounds are better than one. Stops when nothing recommended
    is left, when a round changes nothing, or when the same set of actions would be repeated.
    Every round is its own change set; ``changesets`` lists their ids so the whole run can be undone.
    """
    root = prev.pack.root
    if root is None:
        raise fixer.FixError("No instance folder to modify")
    cur, applied, errors, changesets, seen, passes = prev, 0, [], [], set(), 0
    while passes < max_passes:
        actions = recommended_actions(cur, allow_network)
        if not actions:
            break
        key = json.dumps([a.to_dict() for a in actions], sort_keys=True)
        if key in seen:
            break
        seen.add(key)
        passes += 1
        cs = fixer.apply_actions(root, actions, progress)
        errors.extend(cs["errors"])
        if not cs["entries"]:
            break
        changesets.append(cs["id"])
        applied += len(cs["entries"])
        cur = reanalyze(cur, online=online, rules_path=rules_path, progress=progress)
    return {"result": cur, "applied": applied, "passes": passes, "errors": errors, "changesets": changesets,
            "before": prev.counts(), "before_score": prev.score}


def apply(result: AnalysisResult, actions: list, progress: Optional[Callable] = None) -> dict:
    if result.pack.root is None:
        raise fixer.FixError("No instance folder to modify")
    return fixer.apply_actions(result.pack.root, actions, progress)

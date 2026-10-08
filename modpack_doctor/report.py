"""Human-readable and machine-readable reports."""
from __future__ import annotations

import json

from .models import AnalysisResult

_ICON = {"error": "[ERROR]", "warning": "[WARN] ", "info": "[info] "}


def pack_summary_lines(r: AnalysisResult) -> list:
    p = r.pack
    lines = [
        f"Pack:        {p.name}",
        f"Source:      {p.source}",
        f"Minecraft:   {p.mc_version or 'unknown'}",
        f"Loader:      {(p.loader or 'unknown')}{(' ' + p.loader_version) if p.loader_version else ''}",
        f"Side:        {p.side}",
        f"Mods:        {p.mod_count} jar(s) with mod metadata"
        + (f", {p.disabled_count} disabled file(s)" if p.disabled_count else ""),
    ]
    if p.root:
        lines.append(f"Folder:      {p.root}")
    return lines


def to_text(r: AnalysisResult, verbose: bool = False) -> str:
    c = r.counts()
    out = pack_summary_lines(r)
    for n in r.pack.notes:
        out.append(f"Note:        {n}")
    out += ["", f"Health score: {r.score}/100 - {r.verdict}",
            f"{c['error']} error(s), {c['warning']} warning(s), {c['info']} suggestion(s)", ""]
    for f in r.findings:
        if f.severity == "info" and not verbose and f.code in ("update", "recommended_deps", "no_metadata"):
            continue
        out.append(f"{_ICON[f.severity]} {f.title}")
        if f.detail:
            out += ["         " + line for line in f.detail.splitlines()]
        for i, fx in enumerate(f.fixes, 1):
            tag = " (recommended)" if fx.auto else ""
            out.append(f"         fix {i}: {fx.label}{tag}")
        out.append("")
    hidden = sum(1 for f in r.findings if f.severity == "info" and not verbose
                 and f.code in ("update", "recommended_deps", "no_metadata"))
    if hidden:
        out.append(f"({hidden} low-priority item(s) hidden; use --verbose to show them)")
    return "\n".join(out).rstrip() + "\n"


def to_markdown(r: AnalysisResult) -> str:
    c = r.counts()
    p = r.pack
    md = [f"# Modpack report: {p.name}", "",
          f"- **Minecraft:** {p.mc_version or 'unknown'}",
          f"- **Loader:** {p.loader or 'unknown'} {p.loader_version or ''}".rstrip(),
          f"- **Mods analysed:** {p.mod_count}",
          f"- **Health score:** {r.score}/100 ({r.verdict})",
          f"- **Findings:** {c['error']} errors, {c['warning']} warnings, {c['info']} suggestions", ""]
    for n in p.notes:
        md.append(f"> {n}")
    md.append("")
    for sev, title in (("error", "Errors"), ("warning", "Warnings"), ("info", "Suggestions")):
        items = [f for f in r.findings if f.severity == sev]
        if not items:
            continue
        md += [f"## {title}", ""]
        for f in items:
            md.append(f"### {f.title}")
            if f.detail:
                md += ["", f.detail.replace("\n", "  \n")]
            if f.fixes:
                md += ["", "Possible fixes:"] + [f"- {fx.label}{' *(recommended)*' if fx.auto else ''}" for fx in f.fixes]
            if f.trace:
                md += ["", "<details><summary>Where this comes from</summary>", "", "```"] + f.trace + ["```", "", "</details>"]
            md.append("")
    return "\n".join(md)


def to_json(r: AnalysisResult) -> str:
    return json.dumps(r.to_dict(), indent=2)

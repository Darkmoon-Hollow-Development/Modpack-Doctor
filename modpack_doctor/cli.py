"""Command-line interface: analyze / fix / undo / gui."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import fixer, pipeline, report
from .models import LOADERS
from .sources import Options, SourceError


def _progress(msg, frac=None):
    print(f"  ... {msg}", file=sys.stderr, flush=True)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", help="Instance folder, Modrinth .mrpack, or CurseForge modpack .zip")
    p.add_argument("--mc", help="Minecraft version (default: read from launcher data or inferred)")
    p.add_argument("--loader", choices=LOADERS, help="Mod loader (default: read from launcher data or inferred)")
    p.add_argument("--side", choices=("client", "server", "both"), help="Target side (default: auto)")
    p.add_argument("--online", action="store_true", help="Also ask Modrinth about updates and replacements")
    p.add_argument("--cf-key", default=os.environ.get("CURSEFORGE_API_KEY"),
                   help="CurseForge API key for resolving CurseForge pack downloads (or set CURSEFORGE_API_KEY)")
    p.add_argument("--workspace", type=Path, help="Where to unpack .mrpack/.zip packs")
    p.add_argument("--rules", help="Extra rules JSON file")
    p.add_argument("-q", "--quiet", action="store_true", help="No progress messages")


def _run(args):
    opts = Options(mc=args.mc, loader=args.loader, side=args.side, cf_key=args.cf_key, workspace_dir=args.workspace)
    return pipeline.run_analysis(args.source, opts, online=args.online, rules_path=args.rules,
                                 progress=None if args.quiet else _progress)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="modpack-doctor", description="Check a Minecraft modpack for mod and config problems.")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("analyze", help="Scan a modpack and print a report")
    _common(a)
    a.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    a.add_argument("--out", type=Path, help="Write the report to a file instead of printing it")
    a.add_argument("-v", "--verbose", action="store_true", help="Show low-priority suggestions too")

    f = sub.add_parser("fix", help="Apply the recommended fixes (mods are disabled, never deleted; undo is available)")
    _common(f)
    f.add_argument("--yes", action="store_true", help="Do not ask for confirmation")

    u = sub.add_parser("undo", help="Undo the most recent fix run on an instance folder")
    u.add_argument("folder", type=Path)

    g = sub.add_parser("gui", help="Open the desktop app")
    g.add_argument("source", nargs="*", help="Optional instance folders or pack files to open, one tab each")

    args = ap.parse_args(argv)
    if args.cmd in (None, "gui"):
        try:
            from .gui import main as gui_main
        except ImportError as e:  # tkinter is optional on some Linux distributions
            print(f"The desktop app needs tkinter ({e}). Install it (Debian/Ubuntu: sudo apt install python3-tk) "
                  "or use the command line: modpack-doctor analyze <folder-or-pack>", file=sys.stderr)
            return 2
        return gui_main(getattr(args, "source", None) or None)

    try:
        if args.cmd == "analyze":
            res = _run(args)
            text = {"text": lambda: report.to_text(res, args.verbose), "markdown": lambda: report.to_markdown(res),
                    "json": lambda: report.to_json(res)}[args.format]()
            if args.out:
                args.out.write_text(text, encoding="utf-8")
                print(f"Report written to {args.out}")
            else:
                print(text)
            return 1 if res.counts()["error"] else 0

        if args.cmd == "fix":
            res = _run(args)
            actions = pipeline.recommended_actions(res)
            print(report.to_text(res))
            if not actions:
                print("No recommended automatic fixes.")
                return 0
            print(f"{len(actions)} change(s) would be applied to {res.pack.root}:")
            for f_ in res.findings:
                fx = next((x for x in f_.fixes if x.auto), None)
                if fx:
                    print(f"  - {fx.label}")
            if not args.yes and input("Apply these changes? [y/N] ").strip().lower() != "y":
                print("Nothing changed.")
                return 0
            cs = pipeline.apply(res, actions, None if args.quiet else _progress)
            print(f"Applied {len(cs['entries'])} change(s), {len(cs['errors'])} problem(s). Undo with: "
                  f"modpack-doctor undo \"{res.pack.root}\"")
            for e in cs["errors"]:
                print(f"  ! {e}")
            return 0

        if args.cmd == "undo":
            root, _ = __import__("modpack_doctor.sources", fromlist=["x"]).find_instance_root(args.folder)
            cs = fixer.undo(root)
            print(f"Undid change set {cs['id']} ({len(cs['entries'])} change(s)).")
            for p in cs.get("undo_problems", []):
                print(f"  ! {p}")
            return 0
    except (SourceError, fixer.FixError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Tests for the tkinter-free GUI logic and the pipeline.reanalyze() helper."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import fakepack  # noqa: E402
from modpack_doctor import fixer, pipeline, viewmodel as vm  # noqa: E402
from modpack_doctor.models import Finding, Fix, Action  # noqa: E402
from modpack_doctor.rules import load_rules  # noqa: E402
from modpack_doctor.sources import Options  # noqa: E402


def F(sev, cat, title, detail="", mods=(), files=()):
    return Finding(sev, "x", cat, title, detail, list(mods), list(files))


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.fs = [F("error", "Missing dependency", "Missing Fabric API", "needed by Iris", ["iris"], ["mods/iris.jar"]),
                   F("warning", "Config", "bad.toml is not valid", files=["config/bad.toml"]),
                   F("info", "Suggestion", "No rendering optimizer installed")]

    def test_severity_category_text(self):
        self.assertEqual(len(vm.filter_findings(self.fs, ["error", "warning", "info"])), 3)
        self.assertEqual([f.severity for f in vm.filter_findings(self.fs, ["error"])], ["error"])
        self.assertEqual(len(vm.filter_findings(self.fs, ["error", "warning", "info"], "Config")), 1)
        self.assertEqual(len(vm.filter_findings(self.fs, ["error", "warning", "info"], vm.ALL_CATEGORIES)), 3)

    def test_search_matches_mods_files_and_is_multi_word(self):
        allsev = ["error", "warning", "info"]
        self.assertEqual(len(vm.filter_findings(self.fs, allsev, None, "IRIS")), 1)
        self.assertEqual(len(vm.filter_findings(self.fs, allsev, None, "bad.toml")), 1)
        self.assertEqual(len(vm.filter_findings(self.fs, allsev, None, "missing fabric")), 1)
        self.assertEqual(len(vm.filter_findings(self.fs, allsev, None, "missing nothing")), 0)

    def test_categories(self):
        self.assertEqual(vm.categories(self.fs), [vm.ALL_CATEGORIES, "Config", "Missing dependency", "Suggestion"])


class SummaryTests(unittest.TestCase):
    def test_ram(self):
        rules = load_rules()
        self.assertIsNone(vm.suggested_ram_mb(rules, 10))
        self.assertEqual(vm.suggested_ram_mb(rules, 40), 3072)
        self.assertEqual(vm.suggested_ram_mb(rules, 150), 4096)
        self.assertEqual(vm.suggested_ram_mb(rules, 400), 6144)
        self.assertEqual(vm.format_mb(6144), "6 GB")
        self.assertEqual(vm.format_mb(3072), "3 GB")
        self.assertEqual(vm.format_mb(None), "n/a")

    def test_score_color_bands(self):
        self.assertEqual(len({vm.score_color(s) for s in (100, 80, 50, 10)}), 4)

    def test_fix_button_label(self):
        self.assertEqual(vm.fix_button_label(Fix("Do it")), "Do it")
        self.assertEqual(vm.fix_button_label(Fix("Do it", auto=True, needs_network=True)), "Do it   (recommended, downloads)")

    def test_format_for_path(self):
        self.assertEqual(vm.format_for_path("a.JSON"), "json")
        self.assertEqual(vm.format_for_path("a.md"), "md")
        self.assertEqual(vm.format_for_path("a.txt"), "text")
        self.assertEqual(vm.format_for_path("noext"), "text")

    def test_settings_roundtrip_never_stores_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "gui.json"
            vm.save_settings({"mc": "1.20.1", "cf_key": "SECRET", "online": True}, p)
            self.assertEqual(vm.load_settings(p), {"mc": "1.20.1", "online": True})
            self.assertNotIn("SECRET", p.read_text())
            p.write_text("not json")
            self.assertEqual(vm.load_settings(p), {})
            self.assertEqual(vm.load_settings(Path(d) / "missing.json"), {})


class ThemeTests(unittest.TestCase):
    def test_palette_is_complete_and_readable(self):
        pal = vm.PALETTE
        for key in ("fg", "muted", "error", "warning", "info"):
            for bg in ("bg", "surface"):
                self.assertGreaterEqual(vm.contrast_ratio(pal[key], pal[bg]), 4.5, (key, bg))
        for fg, bg in (("accent_fg", "accent"), ("accent_fg", "accent_active"), ("select_fg", "select_bg"),
                       ("fg", "button"), ("fg", "field"), ("fg", "button_active")):
            self.assertGreaterEqual(vm.contrast_ratio(pal[fg], pal[bg]), 4.5, (fg, bg))
        for k in ("hi", "lo", "edge"):
            self.assertRegex(pal[k], r"^#[0-9a-f]{6}$")
        self.assertNotEqual(pal["hi"], pal["lo"])  # the bevel needs a light and a dark side

    def test_only_one_theme_remains(self):
        for gone in ("PALETTES", "THEME_CHOICES", "resolve_theme", "system_prefers_dark"):
            self.assertFalse(hasattr(vm, gone), gone)

    def test_contrast_ratio_known_values(self):
        self.assertAlmostEqual(vm.contrast_ratio("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(vm.contrast_ratio("#ffffff", "#ffffff"), 1.0, places=2)

    def test_settings_are_persisted_and_old_theme_key_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "gui.json"
            vm.save_settings({"theme": "Dark", "show_options": False}, p)
            self.assertEqual(vm.load_settings(p), {"show_options": False})


class TabPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_titles(self):
        self.assertEqual(vm.default_title(""), "New tab")
        self.assertEqual(vm.default_title("C:\\Games\\Prism\\instances\\My Pack\\"), "My Pack")
        self.assertEqual(vm.default_title("/home/x/packs/Cool.mrpack"), "Cool")
        self.assertEqual(vm.default_title("/home/x/packs/Cool.ZIP"), "Cool")
        self.assertEqual(vm.default_title("/.zip"), ".zip")  # never produce an empty title
        self.assertEqual(vm.shorten("short"), "short")
        self.assertEqual(len(vm.shorten("x" * 50, 24)), 24)

    def test_norm_source_matches_equivalent_paths(self):
        self.assertEqual(vm.norm_source("/a/b/../b/c/"), vm.norm_source("/a/b/c"))
        self.assertEqual(vm.norm_source('  "/a/b"  '), vm.norm_source("/a/b"))
        self.assertEqual(vm.norm_source(""), "")

    def test_spec_validation(self):
        good = vm.TabSpec.from_dict({"source": " /p ", "title": " T ", "mc": "1.20.1", "loader": "forge", "side": "server",
                                     "online": True})
        self.assertEqual(good, vm.TabSpec("/p", "T", "1.20.1", "forge", "server", True))
        bad = vm.TabSpec.from_dict({"source": "/p", "mc": "1.20; rm -rf /", "loader": "evil", "side": 7, "online": "true",
                                    "title": ["x"]})
        self.assertEqual(bad, vm.TabSpec("/p", "", "", "auto", "auto", False))
        self.assertIsNone(vm.TabSpec.from_dict({"source": 3}))
        self.assertIsNone(vm.TabSpec.from_dict(None))
        self.assertEqual(len(vm.TabSpec.from_dict({"source": "x" * 9000}).source), 4096)

    def test_session_roundtrip_and_active_index(self):
        p = self.dir / "sub" / "tabs.json"
        specs = [vm.TabSpec("/a", loader="fabric"), vm.TabSpec("/b", "Mine", "1.20.1", "forge", "client", True), vm.TabSpec("")]
        vm.save_session(specs, 1, p)
        got, active = vm.load_session(p)
        self.assertEqual((got, active), (specs, 1))
        self.assertFalse(p.with_name("tabs.json.tmp").exists())  # atomic write leaves no temp file behind

    def test_session_tolerates_damage(self):
        p = self.dir / "tabs.json"
        self.assertEqual(vm.load_session(p), ([], 0))  # missing
        p.write_text("{not json")
        self.assertEqual(vm.load_session(p), ([], 0))
        p.write_text(json.dumps({"tabs": "nope"}))
        self.assertEqual(vm.load_session(p), ([], 0))
        p.write_text(json.dumps({"active": 99, "tabs": [{"source": "/a"}, 5, {"source": 1}, {"source": "/b"}]}))
        specs, active = vm.load_session(p)
        self.assertEqual([x.source for x in specs], ["/a", "/b"])
        self.assertEqual(active, 0)  # out of range falls back
        p.write_text(json.dumps({"active": True, "tabs": [{"source": "/a"}, {"source": "/b"}]}))
        self.assertEqual(vm.load_session(p)[1], 0)  # a bool is not an index

    def test_session_is_capped(self):
        p = self.dir / "tabs.json"
        vm.save_session([vm.TabSpec(f"/p{i}") for i in range(vm.MAX_TABS + 15)], 0, p)
        self.assertEqual(len(vm.load_session(p)[0]), vm.MAX_TABS)

    def test_saved_packs(self):
        lib = vm.Library()
        a = vm.add_saved(lib, "Family server", vm.TabSpec("/srv/a", title="ignored", loader="forge"))
        self.assertEqual((a.name, a.spec.title), ("Family server", ""))
        vm.add_saved(lib, "Family server", vm.TabSpec("/srv/other"))  # same name, other pack: made unique
        self.assertEqual([p.name for p in lib.saved], ["Family server", "Family server (2)"])
        again = vm.add_saved(lib, "Renamed", vm.TabSpec("/srv/./a", mc="1.20.1"))  # same pack: updated, not duplicated
        self.assertIs(again, a)
        self.assertEqual((len(lib.saved), a.name, a.spec.mc), (2, "Renamed", "1.20.1"))
        self.assertTrue(vm.rename_saved(lib, a, "Better name"))
        self.assertFalse(vm.rename_saved(lib, a, "family SERVER (2)"))  # clashes (case-insensitive)
        self.assertFalse(vm.rename_saved(lib, a, "   "))
        self.assertEqual(a.name, "Better name")
        vm.remove_saved(lib, a)
        vm.remove_saved(lib, a)  # removing twice is harmless
        self.assertEqual([p.name for p in lib.saved], ["Family server (2)"])

    def test_recents_order_dedupe_and_limit(self):
        lib = vm.Library()
        for src in ("/a", "/b", "/c", "/a/", "", "  "):
            vm.add_recent(lib, src)
        self.assertEqual(lib.recent, ["/a/", "/c", "/b"])
        for i in range(30):
            vm.add_recent(lib, f"/p{i}")
        self.assertEqual(len(lib.recent), vm.MAX_RECENT)
        self.assertEqual(lib.recent[0], "/p29")

    def test_library_roundtrip_and_damage(self):
        p = self.dir / "library.json"
        lib = vm.Library()
        vm.add_saved(lib, "One", vm.TabSpec("/one", loader="fabric", online=True))
        vm.add_recent(lib, "/recent")
        vm.save_library(lib, p)
        got = vm.load_library(p)
        self.assertEqual([(x.name, x.spec) for x in got.saved], [(x.name, x.spec) for x in lib.saved])
        self.assertEqual(got.recent, ["/recent"])
        p.write_text("garbage")
        self.assertEqual(vm.load_library(p), vm.Library())
        p.write_text(json.dumps({"saved": [{"name": "ok", "spec": {"source": "/x"}}, {"name": "", "spec": {"source": "/y"}},
                                           {"name": "nosrc", "spec": {"source": ""}}, "junk", {"spec": 1}],
                                 "recent": ["/r", "/r/", 5, None, "/s"]}))
        got = vm.load_library(p)
        self.assertEqual([x.name for x in got.saved], ["ok"])
        self.assertEqual(got.recent, ["/r", "/s"])

    def test_cf_key_is_not_part_of_what_gets_saved(self):
        self.assertNotIn("cf", " ".join(vm.TabSpec().to_dict()).lower())


class RealPackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inst = fakepack.make_fabric_instance(Path(self.tmp.name) / "inst")
        self.res = pipeline.run_analysis(str(self.inst), Options())

    def test_mod_rows_and_issue_marking(self):
        rows = vm.mod_rows(self.res)
        by_id = {r.mod_id: r for r in rows}
        self.assertIn("sodium", by_id)
        self.assertEqual(by_id["client_thing"].side, "client")
        self.assertEqual(by_id["needs_lib"].issue, "error")  # missing dependency
        self.assertEqual(by_id["filler0"].issue, "")
        only = vm.filter_mod_rows(rows, only_issues=True)
        self.assertTrue(only and all(r.issue for r in only))
        self.assertEqual([r.mod_id for r in vm.filter_mod_rows(rows, "needs_lib")], ["needs_lib"])
        worst_first = vm.sort_mod_rows(rows, "issue")
        self.assertEqual(worst_first[0].issue, "error")
        self.assertEqual(vm.sort_mod_rows(rows, "name", True)[0].name.lower(),
                         max(r.name.lower() for r in rows))

    def test_recommended_fixes_and_facts(self):
        pairs = vm.recommended_fixes(self.res)
        self.assertTrue(pairs)
        self.assertTrue(all(fx.auto for _, fx in pairs))
        facts = dict(vm.pack_facts(self.res, load_rules()))
        self.assertEqual(facts["Minecraft"], "1.20.1")
        self.assertTrue(facts["Loader"].startswith("fabric"))
        self.assertIn("Allocated RAM", facts)
        self.assertIn("jar file(s) scanned", vm.details_text(self.res))

    def test_export_roundtrip(self):
        for ext in (".md", ".json", ".txt"):
            p = Path(self.tmp.name) / f"out{ext}"
            vm.export_report(self.res, str(p))
            self.assertTrue(p.read_text(encoding="utf-8").strip())
        data = json.loads((Path(self.tmp.name) / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(data["pack"]["minecraft"], "1.20.1")

    def test_reanalyze_after_fix_keeps_context_and_improves(self):
        before = self.res.counts()["error"]
        offline = [fx for _, fx in vm.recommended_fixes(self.res) if not fx.needs_network]
        fixer.apply_actions(self.inst, [a for fx in offline for a in fx.actions])
        again = pipeline.reanalyze(self.res)
        self.assertLess(again.counts()["error"], before)
        self.assertEqual(again.pack.name, self.res.pack.name)
        self.assertEqual(again.pack.mc_version, "1.20.1")
        self.assertEqual(again.pack.loader, "fabric")
        self.assertEqual(again.pack.loader_version, self.res.pack.loader_version)
        fixer.undo(self.inst)
        self.assertEqual(pipeline.reanalyze(again).counts()["error"], before)

    def test_autofix_offline_fixes_undoes_and_is_idempotent(self):
        before = self.res.counts()["error"]
        names = sorted(p.name for p in (self.inst / "mods").iterdir())
        out = pipeline.autofix(self.res, allow_network=False)
        self.assertGreaterEqual(out["passes"], 1)
        self.assertLessEqual(out["passes"], 3)
        self.assertTrue(out["changesets"] and out["applied"] > 0)
        self.assertEqual(out["errors"], [])
        self.assertLess(out["result"].counts()["error"], before)
        self.assertEqual(out["before"]["error"], before)
        self.assertEqual(out["before_score"], self.res.score)
        self.assertGreater(out["result"].score, self.res.score)
        # nothing recommended (offline) is left, so a second run does nothing
        again = pipeline.autofix(out["result"], allow_network=False)
        self.assertEqual((again["applied"], again["passes"], again["changesets"]), (0, 0, []))
        # the whole run is undone by undoing its change sets newest-first
        for cs_id in reversed(out["changesets"]):
            fixer.undo(self.inst, cs_id)
        self.assertEqual(names, sorted(p.name for p in (self.inst / "mods").iterdir()))

    def test_autofix_collects_download_failures_and_terminates(self):
        from unittest import mock
        with mock.patch.object(fixer, "_resolve_install", side_effect=fixer.FixError("offline (test)")):
            out = pipeline.autofix(self.res, allow_network=True)
        self.assertTrue(any("offline (test)" in e for e in out["errors"]))
        self.assertLessEqual(out["passes"], 3)
        self.assertLess(out["result"].counts()["error"], self.res.counts()["error"])  # disabling still happened

    def test_recommended_actions_can_skip_downloads(self):
        everything = pipeline.recommended_actions(self.res)
        offline = pipeline.recommended_actions(self.res, allow_network=False)
        self.assertLess(len(offline), len(everything))
        self.assertTrue(all(a.kind != "install_project" for a in offline))

    def test_autofix_needs_a_folder(self):
        self.res.pack.root = None
        with self.assertRaises(fixer.FixError):
            pipeline.autofix(self.res)

    def test_reanalyze_keeps_unresolved_files(self):
        self.res.pack.unresolved = ["mods/gone.jar: no download URL"]
        again = pipeline.reanalyze(self.res)
        self.assertEqual(again.pack.unresolved, ["mods/gone.jar: no download URL"])
        self.assertTrue(any(f.code == "unresolved" for f in again.findings))


class ArchiveReanalyzeTests(unittest.TestCase):
    """Fix + re-check cycle on a pack that came from an archive (the GUI re-reads the unpacked workspace)."""

    def test_curseforge_zip_survives_fix_and_recheck(self):
        import zipfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        mods = tmp / "src"
        mods.mkdir()
        fakepack.forge_jar(mods / "a.jar", "alpha", "1.0", [("minecraft", True, "[1.20.1]"), ("ghostlib", True, "[1,)")])
        fakepack.forge_jar(mods / "b.jar", "beta", "1.0", [("minecraft", True, "[1.20.1]")])
        manifest = {"name": "My CF Pack", "overrides": "overrides", "files": [{"projectID": 1, "fileID": 2, "required": True}],
                    "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.2.0", "primary": True}]}}
        pack = tmp / "pack.zip"
        with zipfile.ZipFile(pack, "w") as z:
            z.writestr("manifest.json", json.dumps(manifest))
            z.write(mods / "a.jar", "overrides/mods/a.jar")
            z.write(mods / "b.jar", "overrides/mods/b.jar")
        res = pipeline.run_analysis(str(pack), Options(workspace_dir=tmp / "ws"))
        self.assertEqual((res.pack.name, res.pack.loader, res.pack.loader_version), ("My CF Pack", "forge", "47.2.0"))
        self.assertTrue(any(f.code == "missing_dep" for f in res.findings))
        self.assertTrue(res.pack.unresolved)  # no API key, so the listed mod was not fetched
        # Disable the jar with the missing dependency inside the workspace, then re-check.
        bad = next(f for f in res.findings if f.code == "missing_dep")
        fixer.apply_actions(res.pack.root, [Action("disable_mod", {"file": bad.files[0]})])
        again = pipeline.reanalyze(res)
        self.assertFalse(any(f.code == "missing_dep" for f in again.findings))
        self.assertEqual((again.pack.name, again.pack.loader, again.pack.loader_version, again.pack.mc_version),
                         ("My CF Pack", "forge", "47.2.0", "1.20.1"))
        self.assertEqual(again.pack.unresolved, res.pack.unresolved)
        self.assertTrue(any(f.code == "unresolved" for f in again.findings))
        self.assertTrue(again.pack.source.startswith("CurseForge pack"))
        self.assertEqual(again.pack.mod_count, 1)


if __name__ == "__main__":
    unittest.main()


class PackFoldersTests(unittest.TestCase):
    def test_folders_from_source_and_result(self):
        import tempfile
        from pathlib import Path
        import fakepack
        from modpack_doctor import pipeline, viewmodel as vm
        with tempfile.TemporaryDirectory() as t:
            inst = fakepack.make_fabric_instance(Path(t) / "inst")
            f = vm.pack_folders(None, str(inst))
            self.assertIn("config", f)
            self.assertTrue(f["mods"].is_dir() and f["mods"].name == "mods")
            from modpack_doctor.sources import Options
            res = pipeline.run_analysis(str(inst), Options())
            self.assertEqual(vm.pack_folders(res)["mods"], res.pack.mods_dir)
            self.assertEqual(vm.pack_folders(None, ""), {})
            self.assertEqual(vm.pack_folders(None, str(Path(t) / "nope")), {})
            import shutil
            shutil.rmtree(inst / "config", ignore_errors=True)
            self.assertNotIn("config", vm.pack_folders(None, str(inst)))

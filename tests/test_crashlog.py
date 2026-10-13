import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import fakepack  # noqa: E402
from modpack_doctor import crashlog, fixer, pipeline, viewmodel as vm  # noqa: E402
from modpack_doctor.models import Action  # noqa: E402
from modpack_doctor.sources import Options  # noqa: E402

FORGE_CRASH = """---- Minecraft Crash Report ----
// Oops.

Time: 2026-01-01 10:00:00
Description: Exception in server tick loop

java.lang.NullPointerException: Cannot invoke "x" because "y" is null
\tat com.example.Foo.tick(Foo.java:12)

-- System Details --
Details:
\tMinecraft Version: 1.20.1
\tSuspected Mod(s): Sodium (sodium), Version: 0.5.3
\tMod List: stuff
"""

FABRIC_LOG = """[10:00:00] [main/ERROR]: Incompatible mods found!
net.fabricmc.loader.impl.FormattedException: Some of your mods are incompatible with the game or each other!
A potential solution has been determined:
\t- Install cool-lib, version 2.0 or later.
Unmet dependency listing:
\t- Mod 'Needs Lib' (needs_lib) 1.0.0 requires version 2.0 or later of mod 'Cool Lib' (cool-lib), which is missing!
"""


class ParseTests(unittest.TestCase):
    def test_forge_suspect_and_exception(self):
        r = crashlog.parse(FORGE_CRASH, "crash-2026.txt")
        self.assertEqual(r.kind, "crash")
        self.assertEqual(r.minecraft, "1.20.1")
        self.assertEqual(r.description, "Exception in server tick loop")
        self.assertTrue(r.exception.startswith("java.lang.NullPointerException"))
        self.assertEqual([(s.mod_id, s.name, s.version) for s in r.suspects], [("sodium", "Sodium", "0.5.3")])

    def test_no_suspects_gives_honest_message(self):
        r = crashlog.parse("---- Minecraft Crash Report ----\nDescription: x\n\nSuspected Mod(s): NONE\n", "crash-1.txt")
        self.assertEqual(r.suspects, [])
        self.assertFalse(r.understood)
        self.assertIn("Nothing here points at a specific mod", r.advice[0])

    def test_fabric_missing_dependency(self):
        r = crashlog.parse(FABRIC_LOG, "latest.log")
        self.assertEqual(r.kind, "log")
        self.assertIn("missing_dep", r.problems)
        self.assertEqual(r.missing[0]["mod"], "needs_lib")
        self.assertEqual(r.missing[0]["needs"], "cool-lib")
        self.assertEqual([s.mod_id for s in r.suspects], ["needs_lib"])
        self.assertTrue(any("Install cool-lib" in a for a in r.advice))

    def test_forge_dependency_table(self):
        text = "Missing or unsupported mandatory dependencies:\n\tMod ID: 'flywheel', Requested by: 'create', " \
               "Expected range: '[0.6,)', Actual version: '[MISSING]'\n"
        r = crashlog.parse(text, "latest.log")
        self.assertEqual(r.missing[0]["needs"], "flywheel")
        self.assertIn("not installed", r.missing[0]["detail"])

    def test_forge_wrong_version_dependency(self):
        text = "\tMod ID: 'flywheel', Requested by: 'create', Expected range: '[0.6,)', Actual version: '0.5.0'\n"
        r = crashlog.parse(text, "latest.log")
        self.assertIn("0.5.0", r.missing[0]["detail"])

    def test_out_of_memory_and_java_version(self):
        r = crashlog.parse("java.lang.OutOfMemoryError: Java heap space\n", "latest.log")
        self.assertIn("oom", r.problems)
        self.assertIn("more RAM", r.advice[0])
        r = crashlog.parse("java.lang.UnsupportedClassVersionError: x has been compiled by a more recent version of the Java "
                           "Runtime (class file version 65.0), this version of the Java Runtime only recognizes class file "
                           "versions up to 61.0\n", "latest.log")
        self.assertIn("java_version", r.problems)
        self.assertIn("Java 21", r.advice[0])
        self.assertIn("Java 17", r.advice[0])

    def test_mixin_failure_names_mod(self):
        r = crashlog.parse("[main/ERROR]: Mixin apply for mod bar failed bar.mixins.json:Thing -> net.minecraft.X\n", "latest.log")
        self.assertIn("mixin", r.problems)
        self.assertEqual(r.suspects[0].mod_id, "bar")

    def test_garbage_does_not_crash(self):
        r = crashlog.parse("\x00\x01 not a log at all", "x.txt")
        self.assertFalse(r.understood)
        crashlog.parse("", "empty.txt")

    def test_find_latest_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIsNone(crashlog.find_latest(root))
            (root / "logs").mkdir()
            (root / "logs" / "latest.log").write_text("hi")
            self.assertEqual(crashlog.find_latest(root).name, "latest.log")
            cr = root / "crash-reports"
            cr.mkdir()
            old, new = cr / "crash-a.txt", cr / "crash-b.txt"
            old.write_text(FORGE_CRASH)
            new.write_text(FORGE_CRASH)
            os.utime(old, (1, 1))
            self.assertEqual(crashlog.find_latest(root).name, "crash-b.txt")
            self.assertEqual(crashlog.analyze_file(new).suspects[0].mod_id, "sodium")

    def test_big_file_reads_only_the_end(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.log"
            p.write_bytes(b"x" * (crashlog.MAX_BYTES + 1000) + b"\njava.lang.OutOfMemoryError: Java heap space\n")
            self.assertIn("oom", crashlog.analyze_file(p).problems)

    def test_match_jars_by_id_and_filename(self):
        with tempfile.TemporaryDirectory() as d:
            inst = fakepack.make_fabric_instance(Path(d) / "inst")
            res = pipeline.run_analysis(str(inst), Options())
            rep = crashlog.parse(FORGE_CRASH, "crash-1.txt")
            crashlog.match_jars(rep, res.jars)
            self.assertEqual(rep.suspects[0].jar, "mods/sodium-0.5.3.jar")
            rep = crashlog.parse("-- MOD iris --\nFailure message: boom\n\tMod File: C:\\game\\mods\\iris-1.6.4.jar\n", "x.txt")
            crashlog.match_jars(rep, res.jars)
            self.assertEqual(rep.suspects[0].jar, "mods/iris-1.6.4.jar")
            self.assertIn("boom", rep.suspects[0].reasons[0])


class PasteTests(unittest.TestCase):
    def test_share_link_ids(self):
        self.assertEqual(crashlog.share_link_id("https://mclo.gs/AbC123x"), "AbC123x")
        self.assertEqual(crashlog.share_link_id("  mclo.gs/AbC123x \n"), "AbC123x")
        self.assertEqual(crashlog.share_link_id("https://api.mclo.gs/1/raw/AbC123x"), "AbC123x")
        self.assertIsNone(crashlog.share_link_id("https://evil.example/mclo.gs/AbC123x"))
        self.assertIsNone(crashlog.share_link_id("some log text mclo.gs/abcd1234 more"))
        self.assertIsNone(crashlog.share_link_id(""))

    def test_pasted_text_and_link(self):
        r = crashlog.analyze_pasted(FORGE_CRASH)
        self.assertEqual(r.name, "pasted text")
        self.assertEqual(r.suspects[0].mod_id, "sodium")
        with self.assertRaises(ValueError):
            crashlog.analyze_pasted("   ")
        with mock.patch.object(crashlog, "fetch_shared", return_value=FABRIC_LOG) as f:
            r = crashlog.analyze_pasted("https://mclo.gs/AbC123x")
        f.assert_called_once_with("AbC123x")
        self.assertEqual(r.name, "mclo.gs/AbC123x")
        self.assertIn("missing_dep", r.problems)

    def test_fetch_only_talks_to_the_allowed_host(self):
        seen = {}

        def fake(url, dest, allowed_hosts=None, max_bytes=0, **kw):
            seen.update(url=url, hosts=set(allowed_hosts))
            dest.write_text(FORGE_CRASH)
        with mock.patch("modpack_doctor.net.download", side_effect=fake):
            self.assertIn("Suspected", crashlog.fetch_shared("AbC123x"))
        self.assertEqual(seen["url"], "https://api.mclo.gs/1/raw/AbC123x")
        self.assertEqual(seen["hosts"], {"api.mclo.gs"})


class EnableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = fakepack.make_fabric_instance(Path(self.tmp.name) / "inst")

    def test_disable_then_enable_then_undo(self):
        rel = "mods/sodium-0.5.3.jar"
        fixer.apply_actions(self.root, [Action("disable_mod", {"file": rel})])
        base = ["mods/already-off.jar.disabled"]  # the fixture ships one disabled jar
        self.assertEqual(fixer.list_disabled(self.root), base + [rel + ".disabled"])
        cs = fixer.apply_actions(self.root, [Action("enable_mod", {"file": rel + ".disabled"})])
        self.assertEqual(cs["errors"], [])
        self.assertTrue((self.root / rel).is_file())
        self.assertEqual(fixer.list_disabled(self.root), base)
        fixer.undo(self.root)  # puts it back to disabled
        self.assertFalse((self.root / rel).exists())
        self.assertTrue((self.root / (rel + ".disabled")).is_file())

    def test_changesets_made_in_one_second_undo_in_order(self):
        for name in ("sodium-0.5.3.jar", "iris-1.6.4.jar", "lithium-0.11.2.jar"):
            fixer.apply_actions(self.root, [Action("disable_mod", {"file": "mods/" + name})])
        ids = [c["id"] for c in fixer.list_changesets(self.root)]
        self.assertEqual(len(ids), 3)
        fixer.undo(self.root)  # newest first, even when the ids differ only by a "-2" / "-3" suffix
        self.assertTrue((self.root / "mods" / "lithium-0.11.2.jar").is_file())
        self.assertFalse((self.root / "mods" / "iris-1.6.4.jar").exists())

    def test_numbered_disabled_name_and_conflicts(self):
        mods = self.root / "mods"
        (mods / "a.jar.disabled1").write_bytes(b"x")
        self.assertIn("mods/a.jar.disabled1", fixer.list_disabled(self.root))
        cs = fixer.apply_actions(self.root, [Action("enable_mod", {"file": "mods/a.jar.disabled1"})])
        self.assertTrue((mods / "a.jar").is_file())
        (mods / "b.jar").write_bytes(b"1")
        (mods / "b.jar.disabled").write_bytes(b"2")
        cs = fixer.apply_actions(self.root, [Action("enable_mod", {"file": "mods/b.jar.disabled"})])
        self.assertIn("already exists", cs["errors"][0])
        self.assertEqual((mods / "b.jar").read_bytes(), b"1")  # never overwrites

    def test_rejects_other_files_and_outside_paths(self):
        cs = fixer.apply_actions(self.root, [Action("enable_mod", {"file": "mods/sodium-0.5.3.jar"})])
        self.assertIn("Not a disabled jar", cs["errors"][0])
        cs = fixer.apply_actions(self.root, [Action("enable_mod", {"file": "../outside.jar.disabled"})])
        self.assertTrue(cs["errors"])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        inst = fakepack.make_fabric_instance(Path(self.tmp.name) / "inst")
        self.res = pipeline.run_analysis(str(inst), Options())

    def test_csv_roundtrip(self):
        rows = list(csv.reader(io.StringIO(vm.render_mod_list(self.res, "csv"))))
        self.assertEqual(rows[0], ["Name", "Mod id", "Version", "Loader", "Side", "File", "Issue"])
        ids = {r[1] for r in rows[1:]}
        self.assertTrue({"sodium", "iris", "lithium"} <= ids)
        self.assertEqual(len(rows) - 1, len(vm.mod_rows(self.res)))

    def test_markdown_escapes_pipes(self):
        self.res.jars[0].mods[0].name = "A|B"
        text = vm.render_mod_list(self.res, "md")
        self.assertIn("A\\|B", text)
        self.assertTrue(text.splitlines()[1].startswith("|---"))

    def test_export_file_format_by_extension(self):
        p = Path(self.tmp.name) / "m.csv"
        n = vm.export_mod_list(self.res, str(p))
        self.assertGreater(n, 5)
        self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf"))  # BOM for Excel
        md = Path(self.tmp.name) / "m.md"
        vm.export_mod_list(self.res, str(md))
        self.assertTrue(md.read_text(encoding="utf-8").startswith("| Name"))

    def test_plain_text_list(self):
        text = vm.render_mod_list(self.res, "txt")
        lines = text.splitlines()
        self.assertTrue(lines[0].endswith(f" - {len(vm.mod_rows(self.res))} mods"))
        self.assertEqual(lines[1], "")
        self.assertIn("sodium 0.5.3  [sodium]  (mods/sodium-0.5.3.jar)", lines)
        p = Path(self.tmp.name) / "m.txt"
        vm.export_mod_list(self.res, str(p))
        self.assertFalse(p.read_bytes().startswith(b"\xef\xbb\xbf"))  # no BOM: plain text
        self.assertEqual(p.read_text(encoding="utf-8"), text)

    def test_shortcuts_text_lists_every_binding(self):
        t = vm.shortcuts_text()
        self.assertIn("F5", t)
        self.assertIn("Ctrl+L", t)
        self.assertEqual(len(t.splitlines()), len(vm.SHORTCUTS))


if __name__ == "__main__":
    unittest.main()

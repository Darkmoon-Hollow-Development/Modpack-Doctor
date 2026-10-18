"""Minecraft theme, support summary, config backup and the new buttons."""
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import fakepack  # noqa: E402
from modpack_doctor import fixer, pipeline, viewmodel as vm  # noqa: E402
from modpack_doctor.sources import Options  # noqa: E402
from test_gui import HAVE_DISPLAY  # noqa: E402


class SummaryAndBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inst = fakepack.make_fabric_instance(Path(self.tmp.name) / "inst")
        self.res = pipeline.run_analysis(str(self.inst), Options())

    def test_support_summary_is_short_and_lists_worst_first(self):
        text = vm.support_summary(self.res, limit=3)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("Modpack Doctor report for"))
        self.assertIn(f"health {self.res.score}/100", lines[1])
        bullets = [ln for ln in lines if ln.startswith("- ")]
        self.assertEqual(len(bullets), 4)  # 3 findings + the "and N more" line
        self.assertTrue(bullets[0].startswith("- [ERROR]"))
        self.assertIn("more", bullets[-1])

    def test_backup_configs_zips_configs_and_mod_list(self):
        (self.inst / "config").mkdir(exist_ok=True)
        (self.inst / "config" / "a.toml").write_text("x=1", encoding="utf-8")
        (self.inst / "config" / "sub").mkdir()
        (self.inst / "config" / "sub" / "b.json").write_text("{}", encoding="utf-8")
        (self.inst / "options.txt").write_text("gamma:1", encoding="utf-8")
        out, n = fixer.backup_configs(self.inst, "mods here\n")
        self.assertEqual(out.parent, self.inst / ".modpack_doctor" / "backups")
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
        self.assertTrue({"config/a.toml", "config/sub/b.json", "options.txt", "mod-list.txt"} <= names)
        self.assertEqual(n, len(names))
        self.assertFalse(any(x.startswith("mods/") for x in names))  # the jars themselves are not copied
        out2, _ = fixer.backup_configs(self.inst)  # never overwrites an earlier backup
        self.assertNotEqual(out, out2)
        self.assertTrue(out.exists() and out2.exists())

    def test_backup_with_nothing_to_save_still_works(self):
        out, n = fixer.backup_configs(self.inst, dest_dir=Path(self.tmp.name) / "elsewhere")
        self.assertTrue(out.exists())
        self.assertGreaterEqual(n, 0)


@unittest.skipUnless(HAVE_DISPLAY, "tkinter and a display are required")
class UpdateGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["MODPACK_DOCTOR_HOME"] = str(Path(self.tmp.name) / "home")
        self.inst = fakepack.make_fabric_instance(Path(self.tmp.name) / "inst")
        from modpack_doctor import gui
        self.gui = gui
        self.app = gui.App(str(self.inst), restore=False)
        self.addCleanup(lambda: self._safe_destroy())

    def _safe_destroy(self):
        try:
            self.app.destroy()
        except Exception:  # noqa: BLE001
            pass

    def analyze(self):
        import time
        self.app.analyze()
        end = time.time() + 30
        while self.app.busy and time.time() < end:
            self.app.update()
            time.sleep(0.01)
        self.app.update()
        self.assertIsNotNone(self.app.result)

    def test_minecraft_look_draws_hearts(self):
        self.assertEqual(self.app.pal, self.gui.vm.PALETTE)
        self.analyze()
        self.assertGreaterEqual(len(self.app.gauge.find_all()), 100)  # pixel hearts are many small rectangles

    def test_health_hearts_follow_the_score(self):
        self.analyze()
        counts = {}
        for score in (0, 50, 100):
            self.app._draw_gauge(score)
            counts[score] = sum(1 for i in self.app.gauge.find_all()
                                if self.app.gauge.type(i) == "rectangle" and self.app.gauge.itemcget(i, "fill") == "#d62828")
        self.assertEqual(counts[0], 0)
        self.assertGreater(counts[50], 0)
        self.assertGreater(counts[100], counts[50] * 1.8)

    def _finish_slide(self):
        import time
        end = time.time() + 5
        while self.app._opt_anim is not None and time.time() < end:
            self.app.update()
            time.sleep(0.01)
        self.app.update()
        self.assertIsNone(self.app._opt_anim)

    def test_options_panel_slides_away_and_is_remembered(self):
        self.app.update()
        self.assertTrue(self.app.options_wrap.winfo_manager())
        tall = self.app._body.winfo_rooty()
        self.app._toggle_options()
        self.app.update()
        self.assertIsNotNone(self.app._opt_anim)  # animating, not an instant jump
        self.app._toggle_options()  # clicks during the slide are ignored
        self._finish_slide()
        self.assertFalse(self.app.options_wrap.winfo_manager())
        self.assertLess(self.app._body.winfo_rooty(), tall)  # everything below slid up
        self.assertFalse(vm.load_settings()["show_options"])
        self.app.destroy()
        self.app = self.gui.App(str(self.inst), restore=False)
        self.assertFalse(self.app.options_wrap.winfo_manager())
        self.app._toggle_options()
        self._finish_slide()
        self.assertTrue(self.app.options_wrap.winfo_manager())
        self.assertEqual(self.app._body.winfo_rooty(), tall)

    def test_findings_sit_left_and_health_panel_right(self):
        self.app.update()
        self.assertLess(self.app.notebook.winfo_rootx(), self.app.gauge.winfo_rootx())

    def test_copy_summary_and_mod_list(self):
        self.analyze()
        self.app.copy_summary()
        self.assertTrue(self.app.clipboard_get().startswith("Modpack Doctor report for"))
        self.app.copy_mod_list()
        text = self.app.clipboard_get()
        self.assertIn("sodium 0.5.3  [sodium]", text)

    def test_find_selected_mod_opens_search_page(self):
        self.analyze()
        with mock.patch.object(self.gui.messagebox, "showinfo") as info, \
                mock.patch.object(self.gui.webbrowser, "open") as op:
            self.app.find_selected_mod()  # nothing selected
            info.assert_called_once()
            op.assert_not_called()
            first = self.app.mod_tree.get_children()[0]
            self.app.mod_tree.selection_set(first)
            self.app.find_selected_mod()
        url = op.call_args[0][0]
        self.assertTrue(url.startswith("https://"))
        self.assertTrue("modrinth.com" in url or "curseforge.com" in url)

    def test_backup_button_writes_zip(self):
        self.analyze()
        (self.inst / "config").mkdir(exist_ok=True)
        (self.inst / "config" / "a.toml").write_text("x=1", encoding="utf-8")
        with mock.patch.object(self.gui.messagebox, "showinfo") as info:
            self.app.backup_configs()
        info.assert_called_once()
        zips = list((self.inst / ".modpack_doctor" / "backups").glob("configs-*.zip"))
        self.assertEqual(len(zips), 1)
        with zipfile.ZipFile(zips[0]) as z:
            self.assertIn("config/a.toml", z.namelist())
            self.assertIn("mod-list.txt", z.namelist())

    def test_new_buttons_are_disabled_until_a_scan(self):
        self.assertIn("disabled", self.app.summary_btn.state())
        self.assertIn("disabled", self.app.mods_copy_btn.state())
        self.analyze()
        self.assertNotIn("disabled", self.app.summary_btn.state())
        self.assertNotIn("disabled", self.app.mods_copy_btn.state())


if __name__ == "__main__":
    unittest.main()

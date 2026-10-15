"""GUI tests. They need tkinter and a display (on Linux CI: ``xvfb-run python -m unittest``); otherwise skipped."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import tkinter
    _root = tkinter.Tk()
    _root.destroy()
    HAVE_DISPLAY = True
except Exception:  # noqa: BLE001 - no tkinter or no display
    HAVE_DISPLAY = False

import fakepack  # noqa: E402


@unittest.skipUnless(HAVE_DISPLAY, "tkinter and a display are required")
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["MODPACK_DOCTOR_HOME"] = str(Path(self.tmp.name) / "home")
        self.inst = fakepack.make_fabric_instance(Path(self.tmp.name) / "inst")
        from modpack_doctor import gui
        self.gui = gui
        self.app = gui.App(str(self.inst), restore=False)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.app.destroy()
        except Exception:  # noqa: BLE001
            pass

    def wait(self, timeout=30):
        end = time.time() + timeout
        self.app.update()
        while self.app.busy and time.time() < end:
            self.app.update()
            time.sleep(0.01)
        self.app.update()
        self.assertFalse(self.app.busy, "background task did not finish")

    def analyze(self):
        self.app.analyze()
        self.wait()
        self.assertIsNotNone(self.app.result)

    def test_starts_with_welcome_and_disabled_actions(self):
        self.assertIn("not been scanned", self.app.welcome_box.get("1.0", "end"))  # source given, not scanned yet
        self.assertIn("disabled", self.app.undo_btn.state())
        self.assertNotIn("disabled", self.app.autofix_btn.state())  # usable from the start: it scans first

    def test_analysis_fills_every_view(self):
        self.analyze()
        res = self.app.result
        self.assertGreater(len(self.app.tree.get_children()), 5)
        self.assertEqual(len(self.app.tree.get_children()), len(res.findings))
        self.assertGreater(len(self.app.mod_tree.get_children()), 15)
        self.assertIn("Test Pack", self.app.pack_text.get("1.0", "end") + str([w.cget("text") for w in self.app.facts_frame.winfo_children()]))
        self.assertRegex(str(self.app.autofix_btn.cget("text")), r"Auto-fix pack \(\d+\)")
        # first finding is selected and its detail + fix buttons are shown
        self.assertTrue(self.app.tree.selection())
        self.assertTrue(self.app.detail.get("1.0", "end").strip())

    def test_folder_buttons_and_icon(self):
        self.assertTrue(self.app.iconphoto)  # window icon was set without error
        self.assertTrue(__import__("pkgutil").get_data("modpack_doctor", "data/icon.png").startswith(b"\x89PNG"))
        self.assertTrue(getattr(self.app, "_icon_img", None))  # the icon image was actually loaded
        self.app.update()
        self.assertNotIn("disabled", self.app.mods_btn.state())  # known from the source folder before any scan
        opened = []
        with mock.patch.object(self.gui, "open_in_file_manager", side_effect=opened.append):
            self.app.open_pack_folder("mods")
            self.app.open_pack_folder("config")
        self.assertEqual([p.name for p in opened], ["mods", "config"])
        self.app.src_var.set("")
        self.app._refresh_buttons()
        self.assertIn("disabled", self.app.config_btn.state())
        with mock.patch.object(self.gui.messagebox, "showinfo") as info:
            self.app.open_pack_folder("mods")
        self.assertTrue(info.called)

    def test_crash_tab_analyzes_and_disables(self):
        self.analyze()
        cr = self.inst / "crash-reports"
        cr.mkdir()
        (cr / "crash-1.txt").write_text(
            "---- Minecraft Crash Report ----\nDescription: boom\n\njava.lang.IllegalStateException: x\n\n"
            "-- System Details --\nDetails:\n\tSuspected Mod(s): Sodium (sodium), Version: 0.5.3\n")
        self.app.analyze_crash()
        self.app.update()
        text = self.app.crash_text.get("1.0", "end")
        self.assertIn("crash-1.txt", text)
        self.assertIn("mods/sodium-0.5.3.jar", text)
        self.assertEqual(len(self.app.crash_fix_frame.winfo_children()), 1)
        # the result belongs to the tab: switching away and back keeps it
        self.app.new_tab()
        self.assertIn("Analyze latest crash", self.app.crash_text.get("1.0", "end"))
        self.app.switch_to(self.app.tabs[0])
        self.assertIn("crash-1.txt", self.app.crash_text.get("1.0", "end"))
        with mock.patch.object(self.gui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(self.gui.messagebox, "showinfo"), mock.patch.object(self.gui.messagebox, "showwarning"):
            self.app.crash_fix_frame.winfo_children()[0].invoke()
            self.wait()
        self.assertTrue((self.inst / "mods" / "sodium-0.5.3.jar.disabled").is_file())

    def test_paste_crash_text(self):
        self.analyze()
        with mock.patch.object(self.app, "clipboard_get", return_value="---- Minecraft Crash Report ----\n"
                               "\tSuspected Mod(s): Iris (iris), Version: 1.6.4\n"):
            self.app.paste_crash()
        win = [w for w in self.app.winfo_children() if isinstance(w, self.gui.tk.Toplevel)][-1]
        self.assertIn("Suspected", win.text.get("1.0", "end"))  # pre-filled from the clipboard
        win.analyze_btn.invoke()
        self.app.update()
        text = self.app.crash_text.get("1.0", "end")
        self.assertIn("pasted text", text)
        self.assertIn("mods/iris-1.6.4.jar", text)
        self.assertEqual(len(self.app.crash_fix_frame.winfo_children()), 1)

    def test_paste_crash_link_failure_is_reported(self):
        from modpack_doctor import net
        with mock.patch.object(self.gui.crashlog, "fetch_shared", side_effect=net.NetError("offline")), \
                mock.patch.object(self.gui.messagebox, "showerror") as err:
            self.app.paste_crash()
            win = [w for w in self.app.winfo_children() if isinstance(w, self.gui.tk.Toplevel)][-1]
            win.text.delete("1.0", "end")
            win.text.insert("1.0", "https://mclo.gs/AbC123x")
            win.analyze_btn.invoke()
            self.wait()
        self.assertIn("offline", err.call_args[0][1])

    def test_launcher_choice_is_used_remembered_and_per_tab(self):
        self.assertEqual(self.app.launcher_var.get(), "Auto-detect")
        self.app.launcher_var.set("CurseForge app")
        self.analyze()
        self.assertEqual(self.app.result.pack.launcher, "curseforge")
        self.assertIn("CurseForge app", [v for k, v in self.gui.vm.pack_facts(self.app.result, {}) if k == "Launcher"])
        self.app.new_tab()
        self.assertEqual(self.app.launcher_var.get(), "Auto-detect")  # a new tab starts fresh
        self.app.switch_to(self.app.tabs[0])
        self.assertEqual(self.app.launcher_var.get(), "CurseForge app")
        self.app._capture_active()
        self.assertEqual(self.app.tabs[0].spec().launcher, "curseforge")  # saved with the tab / saved pack

    def test_dialogs_start_in_the_launchers_folder(self):
        start = Path(self.tmp.name) / "instances"
        start.mkdir()
        self.app.launcher_var.set("Prism / MultiMC")
        with mock.patch.object(self.gui.launchers, "instances_folder", return_value=start), \
                mock.patch.object(self.gui.filedialog, "askdirectory", return_value="") as ask:
            self.app.pick_folder()
        self.assertEqual(ask.call_args.kwargs["initialdir"], str(start))
        self.app.launcher_var.set("Modrinth App")
        with mock.patch.object(self.gui.filedialog, "askopenfilename", return_value="") as ask:
            self.app.pick_file()
        self.assertEqual(ask.call_args.kwargs["filetypes"][0], ("Modrinth pack", "*.mrpack"))
        self.app.launcher_var.set("Auto-detect")
        with mock.patch.object(self.gui.filedialog, "askopenfilename", return_value="") as ask:
            self.app.pick_file()
        self.assertEqual(ask.call_args.kwargs["filetypes"][0][0], "Modpack files")

    def test_missing_mod_has_download_and_search_buttons(self):
        self.app.launcher_var.set("CurseForge app")
        self.analyze()
        f = self._select("missing_dep")
        labels = [b.cget("text") for b in self.app.fix_frame.winfo_children()]
        self.assertTrue(any(l.startswith("Download") and "Modrinth" in l for l in labels), labels)
        search = [b for b in self.app.fix_frame.winfo_children() if "Search" in b.cget("text")]
        self.assertEqual(len(search), 2)
        self.assertIn("CurseForge", search[0].cget("text"))
        with mock.patch.object(self.gui.webbrowser, "open") as op:
            search[0].invoke()
        self.assertTrue(op.call_args[0][0].startswith("https://www.curseforge.com/"))
        with mock.patch.object(self.gui.webbrowser, "open") as op:
            self.app.open_link("file:///etc/passwd")
            self.app.open_link("javascript:alert(1)")
        self.assertFalse(op.called)  # only http(s) pages are ever opened

    def test_download_a_mod_dialog_builds_the_install_action(self):
        self.analyze()
        got = {}

        def fake_apply(root, actions, prog=None):
            got["actions"] = actions
            return {"id": "x", "entries": [], "errors": []}
        with mock.patch.object(self.gui.simpledialog, "askstring", return_value="  Cool Lib "), \
                mock.patch.object(self.gui.messagebox, "askokcancel", return_value=True) as ok, \
                mock.patch.object(self.gui.messagebox, "showinfo"), mock.patch.object(self.gui.messagebox, "showwarning"), \
                mock.patch.object(self.gui.fixer, "apply_actions", side_effect=fake_apply):
            self.app.download_mod()
            self.wait()
        a = got["actions"][0]
        self.assertEqual(a.kind, "install_project")
        self.assertEqual((a.params["query"], a.params["mc"], a.params["loader"]), ("Cool Lib", "1.20.1", "fabric"))
        self.assertIn("download files from Modrinth", ok.call_args[0][1])
        with mock.patch.object(self.gui.simpledialog, "askstring", return_value="  "), \
                mock.patch.object(self.gui.fixer, "apply_actions") as ap:
            self.app.download_mod()
        self.assertFalse(ap.called)  # an empty name does nothing

    def test_download_a_mod_needs_known_version_and_loader(self):
        self.analyze()
        self.app.result.pack.loader = None
        with mock.patch.object(self.gui.messagebox, "showinfo") as info, \
                mock.patch.object(self.gui.simpledialog, "askstring") as ask:
            self.app.download_mod()
        self.assertTrue(info.called)
        self.assertFalse(ask.called)

    def test_crash_tab_without_files(self):
        self.app.analyze_crash()
        self.assertIn("No crash report found", self.app.crash_text.get("1.0", "end"))

    def test_manage_disabled_reenables(self):
        self.analyze()
        self.assertEqual(self.gui.fixer.list_disabled(self.inst), ["mods/already-off.jar.disabled"])
        fixer = self.gui.fixer
        from modpack_doctor.models import Action
        fixer.apply_actions(self.inst, [Action("disable_mod", {"file": "mods/lithium-0.11.1-old.jar"})])
        self.app.analyze()
        self.wait()
        self.app.manage_disabled()
        win = self.app._disabled_win
        self.assertEqual(win.listbox.size(), 2)
        win.listbox.selection_set(win.listbox.get(0, "end").index("mods/lithium-0.11.1-old.jar.disabled"))
        with mock.patch.object(self.gui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(self.gui.messagebox, "showinfo"), mock.patch.object(self.gui.messagebox, "showwarning"):
            win.update()
            win.enable_btn.invoke()
            self.wait()
        self.assertTrue((self.inst / "mods" / "lithium-0.11.1-old.jar").is_file())

    def test_export_mod_list_and_shortcuts(self):
        self.analyze()
        out = Path(self.tmp.name) / "mods.csv"
        with mock.patch.object(self.gui.filedialog, "asksaveasfilename", return_value=str(out)):
            self.app.export_mod_list()
        self.assertIn("sodium", out.read_text(encoding="utf-8-sig"))
        self.assertIn("mods", self.app.status_var.get())
        self.app.show_shortcuts()
        self.app.update()
        txt = Path(self.tmp.name) / "mods.txt"
        self.assertNotIn("disabled", self.app.mods_txt_btn.state())
        with mock.patch.object(self.gui.filedialog, "asksaveasfilename", return_value=str(txt)) as ask:
            self.app.mods_txt_btn.invoke()
        self.assertEqual(ask.call_args.kwargs["filetypes"], [("Text file", "*.txt")])
        self.assertIn("sodium 0.5.3", txt.read_text(encoding="utf-8"))
        self.assertIn("mods", self.app.status_var.get())

    def _select(self, code):
        for iid, f in self.app._finding_by_iid.items():
            if f.code == code:
                self.app.tree.selection_set(iid)
                self.app.update()
                return f
        self.fail(f"no {code} finding")

    def test_developer_trace_and_buttons(self):
        import json
        self.analyze()
        f = self._select("missing_dep")
        detail = self.app.detail.get("1.0", "end")
        self.assertIn("Where this comes from", detail)
        self.assertIn('depends."cool-lib"', detail)
        self.assertEqual([b.state() for b in self.app.dev_btns], [(), (), ()])  # all three usable
        self.app.copy_debug()
        doc = json.loads(self.app.clipboard_get())
        self.assertEqual(doc["finding"]["code"], "missing_dep")
        self.assertTrue(doc["finding"]["trace"] and doc["files"][0]["sha1"])
        opened = []
        with mock.patch.object(self.gui, "reveal_in_file_manager", side_effect=opened.append):
            self.app.reveal_jar()
        self.assertEqual([p.name for p in opened], ["needs-lib.jar"])

    def test_metadata_viewer_highlights_the_failing_line(self):
        import zipfile
        fakepack.forge_jar(self.inst / "mods" / "halfbroken.jar", "halfbroken", "1.0")
        with zipfile.ZipFile(self.inst / "mods" / "halfbroken.jar", "a") as z:
            z.writestr("fabric.mod.json", '{\n  "id": "x",\n  oops\n}')
        self.analyze()
        f = next(x for x in self.app.result.findings if x.code == "bad_metadata" and "halfbroken" in x.title)
        before = set(self.app.winfo_children())
        self.app.view_metadata(f.files[0])
        self.app.update()
        win = next(w for w in self.app.winfo_children() if w not in before and isinstance(w, self.gui.tk.Toplevel))
        texts = [w for w in win.winfo_children()[2].winfo_children() if isinstance(w, self.gui.tk.Text)]
        self.assertIn("oops", texts[0].get("1.0", "end"))
        self.assertTrue(texts[0].tag_ranges("err") or "line" not in f.trace[-1].lower())
        win.destroy()

    def test_ignore_button_hides_the_warning_and_it_can_be_shown_again(self):
        import zipfile
        self._patched_dialogs()
        fakepack.forge_jar(self.inst / "mods" / "halfbroken.jar", "halfbroken", "1.0")
        with zipfile.ZipFile(self.inst / "mods" / "halfbroken.jar", "a") as z:
            z.writestr("fabric.mod.json", "{ not json")
        self.analyze()
        f = self._select("bad_metadata")
        fx = next(x for x in f.fixes if x.actions[0].kind == "ignore_finding")
        n = len(self.app.result.findings)
        self.app._apply([fx])
        self.wait()
        self.wait()
        self.assertLess(len(self.app.result.findings), n)
        self.app.show_ignored_again()
        self.wait()
        self.assertEqual(len(self.app.result.findings), n)

    def test_filters(self):
        self.analyze()
        total = len(self.app.tree.get_children())
        self.app.sev_vars["info"].set(False)
        self.app.sev_vars["warning"].set(False)
        self.app._refresh_findings()
        errors = len(self.app.tree.get_children())
        self.assertEqual(errors, self.app.result.counts()["error"])
        self.assertLess(errors, total)
        self.app.sev_vars["info"].set(True)
        self.app.sev_vars["warning"].set(True)
        self.app.search_var.set("lithium")
        self.app.update()
        rows = [self.app.tree.item(i, "values")[2] for i in self.app.tree.get_children()]
        self.assertTrue(rows)
        self.assertTrue(any("lithium" in r.lower() for r in rows))
        self.assertLess(len(rows), total)
        self.app.search_var.set("zzz-no-such-thing")
        self.app.update()
        self.assertEqual(len(self.app.tree.get_children()), 0)
        self.assertIn("No findings match", self.app.detail.get("1.0", "end"))

    def test_mods_tab_search_and_jump(self):
        self.analyze()
        self.app.mod_search_var.set("sodium")
        self.app.update()
        ids = {self.app.mod_tree.set(i, "mod_id") for i in self.app.mod_tree.get_children()}
        self.assertIn("sodium", ids)
        self.assertNotIn("lithium", ids)
        self.app.mod_search_var.set("")
        self.app.mod_issue_var.set(True)
        self.app._refresh_mods()
        flagged = self.app.mod_tree.get_children()
        self.assertTrue(flagged)
        self.assertLess(len(flagged), len(self.app._mod_rows))

    def test_apply_recommended_and_undo(self):
        self.analyze()
        before = self.app.result.counts()["error"]
        jars_before = sorted(p.name for p in (self.inst / "mods").iterdir())
        with mock.patch.object(self.gui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(self.gui.messagebox, "showinfo"), mock.patch.object(self.gui.messagebox, "showwarning"):
            # Only offline fixes: drop the network ones so the test never touches the internet.
            pairs = self.gui.vm.recommended_fixes(self.app.result)
            offline = [fx for _, fx in pairs if not fx.needs_network]
            self.assertTrue(offline)
            self.app._apply(offline)
            self.wait()  # apply
            self.wait()  # automatic re-check
        after = self.app.result.counts()["error"]
        self.assertLess(after, before)
        self.assertNotEqual(jars_before, sorted(p.name for p in (self.inst / "mods").iterdir()))
        self.assertNotIn("disabled", self.app.undo_btn.state())
        with mock.patch.object(self.gui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(self.gui.messagebox, "showwarning"):
            self.app.undo_last()
            self.wait()
        self.assertEqual(jars_before, sorted(p.name for p in (self.inst / "mods").iterdir()))
        self.assertEqual(self.app.result.counts()["error"], before)

    def _patched_dialogs(self):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        m = self.gui.messagebox
        mocks = {name: stack.enter_context(mock.patch.object(m, name, return_value=ret))
                 for name, ret in (("askokcancel", True), ("showinfo", None), ("showwarning", None), ("showerror", None))}
        return mocks

    def test_auto_fix_then_undo_whole_run(self):
        from modpack_doctor import fixer
        self.analyze()
        before = self.app.result.counts()["error"]
        jars_before = sorted(p.name for p in (self.inst / "mods").iterdir())
        dialogs = self._patched_dialogs()
        # Downloads are replaced by a deterministic failure so the test never touches the network.
        with mock.patch.object(fixer, "_resolve_install", side_effect=fixer.FixError("offline (test)")):
            self.app.auto_fix()
            self.wait()
        self.assertLess(self.app.result.counts()["error"], before)
        self.assertNotEqual(jars_before, sorted(p.name for p in (self.inst / "mods").iterdir()))
        dialogs["askokcancel"].assert_called_once()
        self.assertIn("Auto-fix", dialogs["askokcancel"].call_args[0][0])
        report = (dialogs["showwarning"].call_args or dialogs["showinfo"].call_args)[0][1]
        self.assertIn("Health score", report)
        self.assertIn("offline (test)", report)  # the failed download is reported, not hidden
        self.assertTrue(self.app._undo_groups)
        self.assertNotIn("disabled", self.app.undo_btn.state())
        self.app.undo_last()
        self.wait()
        self.assertEqual(jars_before, sorted(p.name for p in (self.inst / "mods").iterdir()))
        self.assertEqual(self.app.result.counts()["error"], before)
        self.assertEqual(self.app._undo_groups, [])

    def test_auto_fix_scans_first_when_nothing_is_loaded(self):
        from modpack_doctor import fixer
        dialogs = self._patched_dialogs()
        jars_before = sorted(p.name for p in (self.inst / "mods").iterdir())
        with mock.patch.object(fixer, "_resolve_install", side_effect=fixer.FixError("offline (test)")):
            self.app.auto_fix()
            # scan -> (50 ms later) confirmation + fix step; wait for the whole chain, not just the first task
            end = time.time() + 30
            while time.time() < end and not (dialogs["askokcancel"].called and not self.app.busy):
                self.app.update()
                time.sleep(0.01)
            self.wait()
        dialogs["askokcancel"].assert_called_once()
        self.assertNotEqual(jars_before, sorted(p.name for p in (self.inst / "mods").iterdir()))

    def test_auto_fix_without_source_asks_for_one(self):
        dialogs = self._patched_dialogs()
        self.app.src_var.set("")
        self.app.auto_fix()
        dialogs["showinfo"].assert_called_once()
        dialogs["askokcancel"].assert_not_called()

    def test_auto_fix_on_clean_pack_explains_there_is_nothing_to_do(self):
        clean = Path(self.tmp.name) / "clean"
        (clean / "mods").mkdir(parents=True)
        (clean / "mmc-pack.json").write_text('{"components":[{"uid":"net.minecraft","version":"1.20.1"},'
                                             '{"uid":"net.fabricmc.fabric-loader","version":"0.15.7"}]}')
        for i in range(3):
            fakepack.fabric_jar(clean / "mods" / f"m{i}.jar", f"m{i}", "1.0.0", {"minecraft": "~1.20.1"})
        dialogs = self._patched_dialogs()
        self.app.src_var.set(str(clean))
        self.analyze()
        self.app.auto_fix()
        dialogs["askokcancel"].assert_not_called()
        self.assertEqual(dialogs["showinfo"].call_args[0][0], "Nothing to auto-fix")

    def test_theme_switching_recolours_everything_and_is_saved(self):
        from tkinter import ttk
        self.analyze()
        vm = self.gui.vm
        for choice, expected in (("Dark", "dark"), ("Light", "light"), ("Dark", "dark")):
            self.app.theme_var.set(choice)
            self.app._on_theme_change()
            self.app.update()
            pal = vm.PALETTES[expected]
            self.assertEqual(self.app.pal, pal)
            self.assertEqual(str(ttk.Style().lookup("Treeview", "background")).lower(), pal["surface"])
            self.assertEqual(str(self.app.detail.cget("bg")).lower(), pal["surface"])
            self.assertEqual(str(self.app.pack_text.cget("bg")).lower(), pal["surface"])
            self.assertEqual(str(self.app.welcome_box.cget("bg")).lower(), pal["surface"])
            self.assertEqual(str(self.app.gauge.cget("bg")).lower(), pal["bg"])
            self.assertEqual(str(self.app.cget("background")).lower(), pal["page"])
            self.assertEqual(str(self.app.tabbar.cget("bg")).lower(), pal["page"])
            self.assertEqual(str(self.app._cards[0].cget("bg")).lower(), pal["page"])
            self.assertEqual(self.app.analyze_btn.pal, pal)  # custom-drawn buttons follow the theme too
            self.assertEqual(str(self.app.count_lbls["error"].cget("fg")).lower(), pal["error"])
            self.assertEqual(str(self.app.tree.tag_configure("error", "foreground")).lower(), pal["error"])
            self.assertEqual(vm.load_settings()["theme"], choice)
        self.assertEqual(self.app.theme_var.get(), "Dark")

    def test_theme_system_follows_the_os_and_survives_restart(self):
        vm = self.gui.vm
        with mock.patch.object(vm, "system_prefers_dark", return_value=True):
            self.app.theme_var.set("System")
            self.app._on_theme_change()
            self.assertEqual(self.app.pal, vm.PALETTES["dark"])
        with mock.patch.object(vm, "system_prefers_dark", return_value=False):
            self.app._on_theme_change()
            self.assertEqual(self.app.pal, vm.PALETTES["light"])
        self.app.theme_var.set("Dark")
        self.app._on_theme_change()
        self.app.destroy()
        self.app = self.gui.App()  # a new window picks up the saved choice
        self.assertEqual(self.app.theme_var.get(), "Dark")
        self.assertEqual(self.app.pal, vm.PALETTES["dark"])

    def test_bad_theme_in_settings_falls_back_to_system(self):
        vm = self.gui.vm
        self.app.destroy()
        vm.save_settings({"theme": "Neon", "ui_version": 2})
        with mock.patch.object(vm, "system_prefers_dark", return_value=False):
            self.app = self.gui.App()
        self.assertEqual(self.app.theme_var.get(), "System")

    def test_bad_source_shows_error_not_crash(self):
        self.app.src_var.set(str(Path(self.tmp.name) / "does-not-exist"))
        with mock.patch.object(self.gui.messagebox, "showerror") as err:
            self.app.analyze()
            self.wait()
        err.assert_called_once()
        self.assertIsNone(self.app.result)
        self.assertFalse(self.app.busy)

    def test_export_report(self):
        self.analyze()
        for ext, needle in ((".md", "# Modpack report"), (".json", '"findings"'), (".txt", "Health score")):
            p = Path(self.tmp.name) / f"r{ext}"
            self.gui.vm.export_report(self.app.result, str(p))
            self.assertIn(needle, p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_DISPLAY, "tkinter and a display are required")
class TabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        os.environ["MODPACK_DOCTOR_HOME"] = str(self.home)
        self.a = fakepack.make_fabric_instance(Path(self.tmp.name) / "packA")
        self.b = fakepack.make_fabric_instance(Path(self.tmp.name) / "packB")
        from modpack_doctor import gui, viewmodel
        self.gui, self.vm = gui, viewmodel
        self.apps = []
        self.addCleanup(self._close)

    def _close(self):
        for app in self.apps:
            try:
                app.destroy()
            except Exception:  # noqa: BLE001
                pass

    def make(self, *a, **k):
        app = self.gui.App(*a, **k)
        self.apps.append(app)
        return app

    def wait(self, app, timeout=30):
        end = time.time() + timeout
        app.update()
        while app.busy and time.time() < end:
            app.update()
            time.sleep(0.01)
        app.update()
        self.assertFalse(app.busy)

    def test_two_packs_keep_separate_results(self):
        app = self.make(restore=False)
        app.open_in_tab(str(self.a))
        app.analyze(); self.wait(app)
        app.open_in_tab(str(self.b))
        self.assertEqual(len(app.tabs), 2)
        self.assertIsNone(app.result)  # new tab is not scanned
        app.analyze(); self.wait(app)
        first, second = app.tabs
        self.assertIsNot(first.result, second.result)
        app.switch_to(first)
        self.assertIs(app.result, first.result)
        self.assertEqual(len(app.tree.get_children()), len(first.result.findings))

    def test_same_pack_reuses_tab_and_blank_tab_is_reused(self):
        app = self.make(restore=False)
        t = app.open_in_tab(str(self.a))
        self.assertEqual(len(app.tabs), 1)  # blank tab reused
        app.open_in_tab(str(self.b))
        again = app.open_in_tab(str(self.a))
        self.assertIs(again, t)
        self.assertEqual(len(app.tabs), 2)

    def test_close_and_reopen_closed_tab(self):
        app = self.make([str(self.a), str(self.b)], restore=False)
        self.assertEqual(len(app.tabs), 2)
        app.close_tab(app.tab)
        self.assertEqual(len(app.tabs), 1)
        app.reopen_closed_tab()
        self.wait(app)  # a local folder is scanned on reopening
        self.assertEqual(len(app.tabs), 2)
        app.close_tab(app.tabs[0]); app.close_tab(app.tabs[0])
        self.assertEqual(len(app.tabs), 1)  # never zero tabs
        self.assertFalse(app.tab.source.strip())

    def test_session_is_restored(self):
        app = self.make([str(self.a), str(self.b)], restore=False)
        app.switch_to(app.tabs[0])
        app.tabs[0].custom_title = "Mine"
        app._after_tab_change()
        app.destroy()
        again = self.make()
        self.assertEqual([t.source for t in again.tabs], [str(self.a), str(self.b)])
        self.assertEqual(again.tabs[0].title(), "Mine")
        self.assertIs(again.tab, again.tabs[0])

    def test_restore_off_deletes_session(self):
        app = self.make(str(self.a), restore=False)
        app.restore_var.set(True)
        app._on_restore_toggle()
        self.assertTrue(self.vm.session_path().exists())
        app.restore_var.set(False)
        app._on_restore_toggle()
        self.assertFalse(self.vm.session_path().exists())

    def test_save_pack_then_open_from_library(self):
        app = self.make(str(self.a), restore=False)
        with mock.patch.object(self.gui.simpledialog, "askstring", return_value="My pack"):
            app.save_pack()
        self.assertEqual([p.name for p in app.library.saved], ["My pack"])
        self.assertTrue(app._is_saved(app.tab))
        app.close_tab(app.tab)
        app.open_saved(app.library.saved[0])
        self.wait(app)
        self.assertEqual(app.tab.title(), "My pack")
        self.assertIsNotNone(app.result)  # local folder auto-scans
        later = self.make(restore=False)
        self.assertEqual([p.name for p in later.library.saved], ["My pack"])

    def test_saved_menu_and_manage_dialog_build(self):
        app = self.make(str(self.a), restore=False)
        with mock.patch.object(self.gui.simpledialog, "askstring", return_value="X"):
            app.save_pack()
        menu = self.gui.tk.Menu(app)
        app._fill_saved_menu(menu)
        self.assertGreater(menu.index("end"), 2)
        app._fill_tabs_menu(menu)

    def test_background_result_goes_to_originating_tab(self):
        app = self.make([str(self.a), str(self.b)], restore=False)
        first, second = app.tabs
        app.switch_to(first)
        app.analyze()
        app.switch_to(second)  # leave while it is working
        self.wait(app)
        self.assertIsNotNone(first.result)
        self.assertIsNone(second.result)
        self.assertIsNone(app.result)
        self.assertTrue(first.attention)

    def test_missing_folder_tab_does_not_crash(self):
        app = self.make(str(Path(self.tmp.name) / "gone"), restore=False)
        with mock.patch.object(self.gui.messagebox, "showerror") as err:
            app.analyze(); self.wait(app)
        self.assertTrue(err.called)
        self.assertIsNone(app.result)

    def test_corrupt_session_file_is_ignored(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "tabs.json").write_text("{not json", encoding="utf-8")
        app = self.make()
        self.assertEqual(len(app.tabs), 1)

"""Update check from GitHub Releases (no real network: everything is faked or served from localhost)."""
import hashlib
import http.server
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipapp
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from modpack_doctor import net, updater  # noqa: E402
from test_gui import HAVE_DISPLAY  # noqa: E402

REPO = "someone/modpack-doctor"


def release_json(tag="v9.9.0", assets=None, **kw):
    base = f"https://github.com/{REPO}/releases/download/{tag}/"
    if assets is None:
        assets = [{"name": "ModpackDoctor.pyzw", "browser_download_url": base + "ModpackDoctor.pyzw", "size": 10,
                   "digest": "sha256:" + "ab" * 32},
                  {"name": "ModpackDoctor-Setup.exe", "browser_download_url": base + "ModpackDoctor-Setup.exe", "size": 20}]
    return {"tag_name": tag, "name": "Big update", "body": "Notes here", "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
            "assets": assets, **kw}


def make_pyzw(path: Path, version: str, main=True, package=True) -> Path:
    with tempfile.TemporaryDirectory() as d:
        stage = Path(d) / "app"
        (stage / "modpack_doctor").mkdir(parents=True)
        if package:
            (stage / "modpack_doctor" / "__init__.py").write_text(f'__version__ = "{version}"\n')
        (stage / "modpack_doctor" / "cli.py").write_text("def main():\n    return 0\n")
        if main:
            zipapp.create_archive(stage, path, main="modpack_doctor.cli:main")
        else:
            with zipfile.ZipFile(path, "w") as z:
                z.write(stage / "modpack_doctor" / "cli.py", "modpack_doctor/cli.py")
    return path


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updater.parse_version("Release-4.1"), (4, 1))  # the project's own tag style
        self.assertEqual(updater.parse_version("Release-4.0"), (4,))
        self.assertEqual(updater.parse_version("1.2.0"), (1, 2))
        self.assertEqual(updater.parse_version("2.0.0-beta.1"), (2,))
        self.assertIsNone(updater.parse_version("latest"))
        self.assertIsNone(updater.parse_version(""))

    def test_is_newer(self):
        self.assertTrue(updater.is_newer("v0.3.1", "0.3.0"))
        self.assertTrue(updater.is_newer("0.10.0", "0.9.0"))  # numbers, not text
        self.assertTrue(updater.is_newer("1.0", "0.9.9"))
        self.assertFalse(updater.is_newer("v0.3.0", "0.3.0"))
        self.assertFalse(updater.is_newer("0.3", "0.3.0"))
        self.assertFalse(updater.is_newer("0.2.9", "0.3.0"))
        self.assertFalse(updater.is_newer("garbage", "0.3.0"))
        self.assertTrue(updater.is_newer("Release-4.1", "4.0"))
        self.assertFalse(updater.is_newer("Release-4.0", "4.0"))


class ReleaseParsingTests(unittest.TestCase):
    def test_assets_and_digest(self):
        rel = updater._release_from_json(release_json(), REPO)
        self.assertEqual(rel.version, "9.9")
        self.assertEqual(rel.tag, "v9.9.0")
        self.assertEqual(rel.asset("ModpackDoctor.pyzw").sha256, "ab" * 32)
        self.assertEqual(rel.asset("ModpackDoctor-Setup.exe").sha256, "")
        self.assertEqual(updater.asset_for("zipapp", rel).name, "ModpackDoctor.pyzw")
        self.assertEqual(updater.asset_for("installer", rel).name, "ModpackDoctor-Setup.exe")
        self.assertIsNone(updater.asset_for("source", rel))

    def test_assets_from_other_places_are_ignored(self):
        evil = [{"name": "ModpackDoctor.pyzw", "browser_download_url": "https://evil.example/ModpackDoctor.pyzw"},
                {"name": "x", "browser_download_url": "http://github.com/someone/modpack-doctor/releases/download/v1/x"},
                {"name": "y", "browser_download_url": "https://github.com/other/repo/releases/download/v1/y"}]
        rel = updater._release_from_json(release_json(assets=evil), REPO)
        self.assertEqual(rel.assets, [])

    def test_unreadable_tag(self):
        with self.assertRaises(updater.UpdateError):
            updater._release_from_json(release_json(tag="nightly"), REPO)

    def test_latest_release_and_check(self):
        with mock.patch.object(net, "http_json", return_value=release_json("v9.9.0")) as hj:
            rel = updater.latest_release(REPO)
            self.assertIn("api.github.com/repos/someone/modpack-doctor/releases/latest", hj.call_args[0][0])
            self.assertEqual(hj.call_args[1]["allowed_hosts"], {"api.github.com"})
            self.assertEqual(updater.check("0.3.0", REPO).version, "9.9")
            self.assertIsNone(updater.check("9.9.0", REPO))
        with mock.patch.object(net, "http_json", return_value=None):  # repository without releases
            self.assertIsNone(updater.check("0.3.0", REPO))
        with mock.patch.object(net, "http_json", side_effect=net.NetError("offline")):
            with self.assertRaises(updater.UpdateError):
                updater.check("0.3.0", REPO)

    def test_repository_name_is_validated(self):
        for bad in ("", "nonsense", "a/b/c", "../x", "a b/c"):
            with self.assertRaises(updater.UpdateError):
                updater.latest_release(bad)
        with mock.patch.dict(os.environ, {"MODPACK_DOCTOR_REPO": "owner/name"}):
            self.assertTrue(updater.configured())
        with mock.patch.dict(os.environ, {"MODPACK_DOCTOR_REPO": ""}), mock.patch.object(updater, "REPO", ""):
            self.assertFalse(updater.configured())

    def test_shipped_repository_is_set(self):
        with mock.patch.dict(os.environ, {"MODPACK_DOCTOR_REPO": ""}):
            self.assertEqual(updater.repo(), "Darkmoon-Hollow-Development/Modpack-Doctor")
            self.assertTrue(updater.configured())

    def test_this_checkout_runs_from_source(self):
        self.assertEqual(updater.install_kind(), "source")


class VerifyAndDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_verify_zipapp(self):
        updater.verify_zipapp(make_pyzw(self.dir / "ok.pyzw", "9.9.0"), "9.9.0")
        updater.verify_zipapp(make_pyzw(self.dir / "ok2.pyzw", "9.9.0"), "v9.9")  # same version, different spelling
        with self.assertRaisesRegex(updater.UpdateError, "says it is version 1.0.0"):
            updater.verify_zipapp(make_pyzw(self.dir / "old.pyzw", "1.0.0"), "9.9.0")
        with self.assertRaisesRegex(updater.UpdateError, "not a Modpack Doctor build"):
            updater.verify_zipapp(make_pyzw(self.dir / "nomain.pyzw", "9.9.0", main=False), "9.9.0")
        (self.dir / "junk.pyzw").write_bytes(b"not a zip")
        with self.assertRaisesRegex(updater.UpdateError, "not a valid archive"):
            updater.verify_zipapp(self.dir / "junk.pyzw", "9.9.0")

    def _serve(self, files: dict):
        srv_dir = self.dir / "srv"
        srv_dir.mkdir()
        for name, data in files.items():
            (srv_dir / name).write_bytes(data)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(srv_dir), **kw)

            def log_message(self, *a):
                pass
        httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        old = {k: os.environ.pop(k, None) for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")}
        net.ALLOW_LOCAL_HTTP = True

        def cleanup():
            net.ALLOW_LOCAL_HTTP = False
            for k, v in old.items():
                if v:
                    os.environ[k] = v
        self.addCleanup(cleanup)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_download_checks_hash_and_version(self):
        good = make_pyzw(self.dir / "g.pyzw", "9.9.0").read_bytes()
        wrong_version = make_pyzw(self.dir / "w.pyzw", "1.0.0").read_bytes()
        base = self._serve({"ModpackDoctor.pyzw": good, "Other.pyzw": wrong_version})
        hosts = {"127.0.0.1"}
        rel = updater.Release("v9.9.0", "9.9", assets=[
            updater.Asset("ModpackDoctor.pyzw", base + "/ModpackDoctor.pyzw", sha256=hashlib.sha256(good).hexdigest())])
        out = updater.download_update(rel, "zipapp", self.dir / "dl", allowed_hosts=hosts)
        self.assertEqual(out.read_bytes(), good)
        # a tampered or truncated download is refused and nothing is left behind
        bad = updater.Release("v9.9.0", "9.9", assets=[
            updater.Asset("ModpackDoctor.pyzw", base + "/ModpackDoctor.pyzw", sha256="0" * 64)])
        with self.assertRaisesRegex(updater.UpdateError, "SHA-256 mismatch"):
            updater.download_update(bad, "zipapp", self.dir / "dl2", allowed_hosts=hosts)
        self.assertFalse(list((self.dir / "dl2").glob("*")))
        # right hash but the build inside is the wrong version (a release built without bumping __version__)
        mism = updater.Release("v9.9.0", "9.9", assets=[
            updater.Asset("ModpackDoctor.pyzw", base + "/Other.pyzw", sha256=hashlib.sha256(wrong_version).hexdigest())])
        with self.assertRaisesRegex(updater.UpdateError, "says it is version 1.0.0"):
            updater.download_update(mism, "zipapp", self.dir / "dl3", allowed_hosts=hosts)
        self.assertFalse((self.dir / "dl3" / "ModpackDoctor.pyzw").exists())

    def test_download_refuses_hosts_that_are_not_allowed(self):
        rel = updater.Release("v9.9.0", "9.9", assets=[updater.Asset("ModpackDoctor.pyzw", "https://evil.example/x")])
        with self.assertRaises(updater.UpdateError):
            updater.download_update(rel, "zipapp", self.dir / "dl")

    def test_missing_asset(self):
        rel = updater.Release("v9.9.0", "9.9")
        with self.assertRaisesRegex(updater.UpdateError, "no ModpackDoctor.pyzw"):
            updater.download_update(rel, "zipapp", self.dir)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_helper_swaps_the_file_and_restarts_it(self):
        marker = self.dir / "marker.txt"
        old = self.dir / "app.py"
        new = self.dir / "new.py"
        old.write_text(f"open({str(marker)!r}, 'w').write('old')\n")
        new.write_text(f"open({str(marker)!r}, 'w').write('new')\n")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        subprocess.run([sys.executable, "-c", updater._HELPER, str(dead.pid), str(new), str(old)], timeout=30, check=True)
        end = time.time() + 10
        while not marker.exists() and time.time() < end:
            time.sleep(0.1)
        self.assertEqual(marker.read_text(), "new")  # the new file replaced the old one and was started
        self.assertFalse(new.exists())

    def test_apply_zipapp_starts_helper(self):
        calls = []
        target = self.dir / "ModpackDoctor.pyzw"
        target.write_bytes(b"x")
        with mock.patch.object(updater, "zipapp_path", return_value=target):
            updater.apply_update("zipapp", self.dir / "new.pyzw", popen=lambda *a, **k: calls.append((a, k)))
        args = calls[0][0][0]
        self.assertEqual(args[:2], [sys.executable, "-c"])
        self.assertEqual(args[3:], [str(os.getpid()), str(self.dir / "new.pyzw"), str(target)])

    def test_apply_installer_runs_silently(self):
        calls = []
        with mock.patch.object(sys, "executable", str(self.dir / "ModpackDoctor.exe")):
            updater.apply_update("installer", self.dir / "Setup.exe", popen=lambda *a, **k: calls.append(a[0]))
        self.assertEqual(calls[0][0], str(self.dir / "Setup.exe"))
        for flag in ("/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/CURRENTUSER"):
            self.assertIn(flag, calls[0])

    def test_source_installs_cannot_self_update(self):
        with self.assertRaises(updater.UpdateError):
            updater.apply_update("source", self.dir / "x")


@unittest.skipUnless(HAVE_DISPLAY, "tkinter and a display are required")
class UpdateGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["MODPACK_DOCTOR_HOME"] = str(Path(self.tmp.name) / "home")
        from modpack_doctor import gui
        self.gui = gui
        self.app = gui.App(restore=False)
        self.addCleanup(self._close)
        p = mock.patch.object(updater, "configured", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _close(self):
        try:
            self.app.destroy()
        except Exception:  # noqa: BLE001
            pass

    def pump(self):
        end = time.time() + 5
        while time.time() < end:
            self.app.update()
            if not self.app._upd_busy and self.app._upd_queue.empty():
                break
            time.sleep(0.01)
        self.app.update()

    def rel(self):
        return updater._release_from_json(release_json("v9.9.0"), REPO)

    def test_manual_check_says_up_to_date(self):
        with mock.patch.object(updater, "check", return_value=None), \
                mock.patch.object(self.gui.messagebox, "showinfo") as info:
            self.app._start_update_check(manual=True)
            self.pump()
        self.assertIn("newest version", info.call_args[0][1])

    def test_automatic_check_is_silent_when_nothing_is_new_or_offline(self):
        with mock.patch.object(updater, "check", return_value=None), \
                mock.patch.object(self.gui.messagebox, "showinfo") as info:
            self.app._start_update_check(manual=False)
            self.pump()
        info.assert_not_called()
        with mock.patch.object(updater, "check", side_effect=updater.UpdateError("offline")), \
                mock.patch.object(self.gui.messagebox, "showerror") as err:
            self.app._start_update_check(manual=False)
            self.pump()
        err.assert_not_called()
        with mock.patch.object(updater, "check", side_effect=updater.UpdateError("offline")), \
                mock.patch.object(self.gui.messagebox, "showerror") as err:
            self.app._start_update_check(manual=True)  # but a manual check tells you
            self.pump()
        err.assert_called_once()

    def test_not_configured_does_nothing_automatically_and_explains_when_asked(self):
        with mock.patch.object(updater, "configured", return_value=False), \
                mock.patch.object(self.gui.messagebox, "showinfo") as info, \
                mock.patch.object(updater, "check") as chk:
            self.app._start_update_check(manual=False)
            info.assert_not_called()
            self.app._start_update_check(manual=True)
            info.assert_called_once()
            chk.assert_not_called()

    def test_new_version_from_source_opens_release_page(self):
        with mock.patch.object(updater, "check", return_value=self.rel()), \
                mock.patch.object(self.gui.messagebox, "askyesno", return_value=True) as ask, \
                mock.patch.object(self.gui.webbrowser, "open") as op:
            self.app._start_update_check(manual=True)
            self.pump()
        ask.assert_called_once()
        self.assertIn("9.9", ask.call_args[0][1])
        op.assert_called_once()
        self.assertIn("/releases/tag/v9.9.0", op.call_args[0][0])

    def test_skip_this_version_is_remembered_for_automatic_checks(self):
        with mock.patch.object(updater, "install_kind", return_value="zipapp"), \
                mock.patch.object(updater, "check", return_value=self.rel()), \
                mock.patch.object(self.gui.messagebox, "askyesnocancel", return_value=None) as ask:
            self.app._start_update_check(manual=True)
            self.pump()
            self.assertEqual(self.app.settings["skip_update"], "9.9")
            self.assertEqual(self.gui.vm.load_settings()["skip_update"], "9.9")
            ask.reset_mock()
            self.app._start_update_check(manual=False)  # automatic: stays quiet about the skipped version
            self.pump()
            ask.assert_not_called()
            self.app._start_update_check(manual=True)  # but asking by hand still offers it
            self.pump()
            ask.assert_called_once()

    def test_remind_me_later_does_nothing(self):
        with mock.patch.object(updater, "install_kind", return_value="zipapp"), \
                mock.patch.object(updater, "check", return_value=self.rel()), \
                mock.patch.object(self.gui.messagebox, "askyesnocancel", return_value=False), \
                mock.patch.object(updater, "download_update") as dl:
            self.app._start_update_check(manual=True)
            self.pump()
        dl.assert_not_called()
        self.assertNotIn("skip_update", self.app.settings)

    def test_update_now_downloads_applies_and_closes(self):
        new = Path(self.tmp.name) / "ModpackDoctor.pyzw"
        with mock.patch.object(updater, "install_kind", return_value="zipapp"), \
                mock.patch.object(updater, "check", return_value=self.rel()), \
                mock.patch.object(self.gui.messagebox, "askyesnocancel", return_value=True), \
                mock.patch.object(updater, "download_update", return_value=new) as dl, \
                mock.patch.object(updater, "apply_update") as ap, \
                mock.patch.object(self.app, "_on_close") as close:
            self.app._start_update_check(manual=True)
            self.pump()
            self.pump()
        dl.assert_called_once()
        ap.assert_called_once_with("zipapp", new)
        close.assert_called_once()

    def test_failed_download_offers_the_release_page_and_keeps_running(self):
        with mock.patch.object(updater, "install_kind", return_value="zipapp"), \
                mock.patch.object(updater, "check", return_value=self.rel()), \
                mock.patch.object(self.gui.messagebox, "askyesnocancel", return_value=True), \
                mock.patch.object(updater, "download_update", side_effect=updater.UpdateError("SHA-256 mismatch")), \
                mock.patch.object(updater, "apply_update") as ap, \
                mock.patch.object(self.gui.messagebox, "askyesno", return_value=False) as ask:
            self.app._start_update_check(manual=True)
            self.pump()
            self.pump()
        ap.assert_not_called()
        self.assertIn("SHA-256 mismatch", ask.call_args[0][1])

    def test_startup_check_is_throttled_and_can_be_switched_off(self):
        with mock.patch.object(self.app, "_start_update_check") as st:
            self.app.settings["last_update_check"] = time.time()
            self.app._auto_update_check()
            st.assert_not_called()
            self.app.settings["last_update_check"] = time.time() - 7 * 3600
            self.app._auto_update_check()
            st.assert_called_once_with(manual=False)
        self.app.updates_var.set(False)
        self.app._save_settings()
        self.assertFalse(self.gui.vm.load_settings()["check_updates"])


if __name__ == "__main__":
    unittest.main()

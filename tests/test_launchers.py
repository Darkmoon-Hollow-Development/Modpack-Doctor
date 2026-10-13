import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import fakepack  # noqa: E402
from modpack_doctor import launchers, pipeline, viewmodel as vm  # noqa: E402
from modpack_doctor.sources import Options  # noqa: E402


class LauncherModuleTests(unittest.TestCase):
    def test_labels_and_keys_roundtrip(self):
        for key, text in launchers.LAUNCHERS.items():
            self.assertEqual(launchers.key_for_label(text), key)
            self.assertEqual(launchers.label(key), text)
        self.assertEqual(launchers.key_for_label("nonsense"), "auto")
        self.assertEqual(launchers.label(None), "Auto-detect")

    def test_side_for(self):
        self.assertIsNone(launchers.side_for("auto"))
        self.assertIsNone(launchers.side_for(None))
        self.assertEqual(launchers.side_for("server"), "server")
        for k in ("curseforge", "modrinth", "prism", "atlauncher", "gdlauncher", "official"):
            self.assertEqual(launchers.side_for(k), "client")

    def test_instances_folder_uses_first_existing_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            self.assertIsNone(launchers.instances_folder("prism", platform="linux", env={}, home=home))
            want = home / ".local" / "share" / "PrismLauncher" / "instances"
            want.mkdir(parents=True)
            self.assertEqual(launchers.instances_folder("prism", platform="linux", env={}, home=home), want)
            appdata = home / "Roaming"
            (appdata / "ModrinthApp" / "profiles").mkdir(parents=True)
            self.assertEqual(launchers.instances_folder("modrinth", platform="win32", env={"APPDATA": str(appdata)}, home=home),
                             appdata / "ModrinthApp" / "profiles")
            cf = home / "curseforge" / "minecraft" / "Instances"
            cf.mkdir(parents=True)
            self.assertEqual(launchers.instances_folder("curseforge", platform="win32", env={}, home=home), cf)
            (home / ".minecraft").mkdir()
            self.assertEqual(launchers.instances_folder("official", platform="linux", env={}, home=home), home / ".minecraft")
            self.assertIsNone(launchers.instances_folder("server", home=home))
            self.assertIsNone(launchers.instances_folder("auto", home=home))
            self.assertIsNone(launchers.instances_folder("bogus", platform="linux", env={}, home=home))

    def test_search_links_put_the_launchers_site_first(self):
        cf = launchers.search_links("Fabric API", "curseforge")
        self.assertEqual([t for t, _ in cf], ["Search CurseForge", "Search Modrinth"])
        self.assertIn("search=Fabric+API", cf[0][1])
        self.assertTrue(all(u.startswith("https://") for _, u in cf))
        mr = launchers.search_links("a&b=c", "modrinth")
        self.assertEqual(mr[0][0], "Search Modrinth")
        self.assertIn("q=a%26b%3Dc", mr[0][1])  # the query cannot add parameters of its own
        self.assertEqual(launchers.search_links("   ", "modrinth"), [])
        self.assertEqual(launchers.search_links("x", None)[0][0], "Search Modrinth")

    def test_pack_file_types(self):
        self.assertEqual(launchers.pack_file_type("curseforge"), ("CurseForge pack", "*.zip"))
        self.assertEqual(launchers.pack_file_type("modrinth"), ("Modrinth pack", "*.mrpack"))
        self.assertIsNone(launchers.pack_file_type("prism"))


class LauncherInAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inst = fakepack.make_fabric_instance(Path(self.tmp.name) / "inst")  # has mmc-pack.json = Prism

    def test_launcher_is_detected_from_files(self):
        res = pipeline.run_analysis(str(self.inst), Options())
        self.assertEqual(res.pack.launcher, "prism")
        self.assertIn(("Launcher", "Prism / MultiMC"), vm.pack_facts(res, {}))
        self.assertEqual(res.to_dict()["pack"]["launcher"], "prism")

    def test_choice_overrides_and_a_mismatch_is_explained(self):
        res = pipeline.run_analysis(str(self.inst), Options(launcher="curseforge"))
        self.assertEqual(res.pack.launcher, "curseforge")
        self.assertTrue(any("looks like a Prism / MultiMC instance" in n for n in res.pack.notes))
        # the folder's own files still decide the Minecraft version and loader
        self.assertEqual(res.pack.mc_version, "1.20.1")
        self.assertEqual(res.pack.loader, "fabric")

    def test_server_choice_sets_side_and_survives_reanalysis(self):
        bare = Path(self.tmp.name) / "bare"
        (bare / "mods").mkdir(parents=True)
        fakepack.fabric_jar(bare / "mods" / "a.jar", "a", "1.0", {"minecraft": "~1.20.1"})
        res = pipeline.run_analysis(str(bare), Options(launcher="server", mc="1.20.1", loader="fabric"))
        self.assertEqual(res.pack.side, "server")
        self.assertEqual(res.pack.launcher, "server")
        again = pipeline.reanalyze(res)
        self.assertEqual((again.pack.side, again.pack.launcher), ("server", "server"))
        client = pipeline.run_analysis(str(bare), Options(launcher="atlauncher", mc="1.20.1", loader="fabric"))
        self.assertEqual(client.pack.side, "client")
        auto = pipeline.run_analysis(str(bare), Options(mc="1.20.1", loader="fabric"))
        self.assertEqual((auto.pack.side, auto.pack.launcher), ("both", ""))

    def test_missing_dependency_has_download_fix_and_search_links(self):
        res = pipeline.run_analysis(str(self.inst), Options(launcher="curseforge"))
        f = next(x for x in res.findings if x.code == "missing_dep" and "cool-lib" in x.title)
        self.assertTrue(f.fixes[0].label.startswith('Download "cool-lib" to your pack from Modrinth'))
        self.assertEqual(f.fixes[0].actions[0].kind, "install_project")
        self.assertEqual([t for t, _ in f.links], ["Search CurseForge", "Search Modrinth"])
        self.assertIn("search buttons", f.detail)
        d = f.to_dict()
        self.assertEqual(d["links"][0]["label"], "Search CurseForge")

    def test_no_download_button_without_version_but_it_says_why(self):
        bare = Path(self.tmp.name) / "bare2"
        (bare / "mods").mkdir(parents=True)
        fakepack.forge_jar(bare / "mods" / "x.jar", "x", "1.0", [("lib", True, "[1,)")])
        res = pipeline.run_analysis(str(bare), Options())
        f = next(x for x in res.findings if x.code == "missing_dep")
        if not (res.pack.loader and res.pack.mc_version):
            self.assertEqual(f.fixes, [])
            self.assertIn("unknown", f.detail)
        self.assertTrue(f.links)


class TabSpecLauncherTests(unittest.TestCase):
    def test_roundtrip_and_validation(self):
        sp = vm.TabSpec(source="x", launcher="modrinth")
        self.assertEqual(vm.TabSpec.from_dict(sp.to_dict()).launcher, "modrinth")
        self.assertEqual(vm.TabSpec.from_dict({"source": "x", "launcher": "evil"}).launcher, "auto")
        self.assertEqual(vm.TabSpec.from_dict({"source": "x", "launcher": 5}).launcher, "auto")
        self.assertEqual(vm.TabSpec.from_dict({"source": "x"}).launcher, "auto")  # files from older versions


if __name__ == "__main__":
    unittest.main()

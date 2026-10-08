import functools
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest
import zipfile
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import fakepack  # noqa: E402
from modpack_doctor import analyzer, configcheck, curseforge, enrich, fixer, modrinth, net, pipeline, report  # noqa: E402
from modpack_doctor.jarscan import scan_jar  # noqa: E402
from modpack_doctor.models import Action  # noqa: E402
from modpack_doctor.rules import load_rules  # noqa: E402
from modpack_doctor.sources import Options, SourceError, open_source, safe_relpath  # noqa: E402
from modpack_doctor.version import fabric_matches, maven_matches, Version  # noqa: E402

os.environ["MODPACK_DOCTOR_HOME"] = tempfile.mkdtemp()


def codes(res):
    return [f.code for f in res.findings]


class VersionTests(unittest.TestCase):
    def test_ordering(self):
        self.assertTrue(Version("1.2") == Version("1.2.0"))
        self.assertTrue(Version("1.2.0-beta") < Version("1.2.0"))
        self.assertTrue(Version("1.2.0-rc1") > Version("1.2.0-beta2"))
        self.assertTrue(Version("0.10.0") > Version("0.9.9"))
        self.assertTrue(Version("1.20.1-47.2.0") < Version("1.20.1-47.10.0"))
        self.assertTrue(Version("v1.0") == Version("1.0"))

    def test_maven(self):
        self.assertTrue(maven_matches("[1.20,1.21)", "1.20.1"))
        self.assertFalse(maven_matches("[1.20,1.21)", "1.21"))
        self.assertTrue(maven_matches("[47,)", "47.2.0"))
        self.assertFalse(maven_matches("[48,)", "47.2.0"))
        self.assertTrue(maven_matches("[1.20.1]", "1.20.1"))
        self.assertFalse(maven_matches("[1.20.1]", "1.20.2"))
        self.assertTrue(maven_matches("(,2.0]", "1.0"))
        self.assertTrue(maven_matches("[1,2),[3,4)", "3.5"))
        self.assertFalse(maven_matches("[1,2),[3,4)", "2.5"))
        self.assertTrue(maven_matches("1.0", "5.0"))  # soft requirement
        self.assertIsNone(maven_matches("[1,2)", None))

    def test_fabric(self):
        self.assertTrue(fabric_matches("~1.20.1", "1.20.4"))
        self.assertFalse(fabric_matches("~1.20.1", "1.21"))
        self.assertTrue(fabric_matches("~1.20", "1.20.6"))
        self.assertTrue(fabric_matches("^1.2.3", "1.9.0"))
        self.assertFalse(fabric_matches("^1.2.3", "2.0.0"))
        self.assertTrue(fabric_matches(">=0.14.21", "0.15.7"))
        self.assertFalse(fabric_matches(">=0.16", "0.15.7"))
        self.assertTrue(fabric_matches("1.20.x", "1.20.4"))
        self.assertFalse(fabric_matches("1.20.x", "1.21.1"))
        self.assertTrue(fabric_matches(">=1.0 <2.0", "1.5"))
        self.assertFalse(fabric_matches(">=1.0 <2.0", "2.5"))
        self.assertTrue(fabric_matches([">=3", "1.x"], "1.4"))
        self.assertTrue(fabric_matches("*", "anything"))
        self.assertTrue(fabric_matches({"any": ["1.x", "3.x"]}, "3.1"))
        self.assertIsNone(fabric_matches("1.x", None))


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_forge_and_neoforge_metadata(self):
        fakepack.forge_jar(self.d / "a.jar", "alpha", "${file.jarVersion}", [("beta", True, "[2,)"), ("gamma", "incompatible", "*"),
                                                                           ("delta", False, "*")],
                           manifest_version="3.1.4")
        j = scan_jar(self.d / "a.jar", self.d)
        m = j.mods[0]
        self.assertEqual((m.mod_id, m.version, m.loader), ("alpha", "3.1.4", "forge"))
        kinds = {d.mod_id: d.kind for d in m.deps}
        self.assertEqual(kinds, {"beta": "required", "gamma": "breaks", "delta": "optional"})
        fakepack.forge_jar(self.d / "n.jar", "nf", "1.0", [("neoforge", "required", "[21,)")], loader="neoforge")
        self.assertEqual(scan_jar(self.d / "n.jar", self.d).mods[0].loader, "neoforge")

    def test_client_only_hint(self):
        fakepack.forge_jar(self.d / "c.jar", "clienty", "1.0", client_only=True)
        self.assertTrue(scan_jar(self.d / "c.jar", self.d).mods[0].client_only_hint)

    def test_quilt(self):
        doc = {"quilt_loader": {"id": "qmod", "version": "1.0", "depends": [
            "qsl", {"id": "minecraft", "versions": ">=1.20"}, {"id": "opt", "optional": True}],
            "metadata": {"name": "Q"}}}
        with zipfile.ZipFile(self.d / "q.jar", "w") as z:
            z.writestr("quilt.mod.json", json.dumps(doc))
        m = scan_jar(self.d / "q.jar", self.d).mods[0]
        self.assertEqual(m.loader, "quilt")
        self.assertEqual({d.mod_id: d.kind for d in m.deps}, {"qsl": "required", "minecraft": "required", "opt": "optional"})

    def test_broken_inputs_do_not_crash(self):
        (self.d / "bad.jar").write_bytes(b"nope")
        with zipfile.ZipFile(self.d / "badjson.jar", "w") as z:
            z.writestr("fabric.mod.json", "{ not json")
        self.assertTrue(scan_jar(self.d / "bad.jar", self.d).errors)
        j = scan_jar(self.d / "badjson.jar", self.d)
        self.assertEqual(j.mods, [])
        self.assertTrue(j.errors)

    def test_jar_in_jar(self):
        fakepack.fabric_jar(self.d / "outer.jar", "outer", "1.0", nested={
            "inner.jar": fakepack._zip_bytes({"fabric.mod.json": {"id": "inner", "version": "2.0"}})})
        j = scan_jar(self.d / "outer.jar", self.d)
        self.assertEqual([b.mod_id for b in j.bundled], ["inner"])


class AnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = fakepack.make_fabric_instance(Path(tempfile.mkdtemp()) / "inst")
        cls.res = pipeline.run_analysis(str(cls.root), Options())

    def titles(self, code):
        return [f.title for f in self.res.findings if f.code == code]

    def test_context_from_launcher(self):
        p = self.res.pack
        self.assertEqual((p.mc_version, p.loader, p.loader_version, p.side), ("1.20.1", "fabric", "0.15.7", "client"))
        self.assertEqual(p.name, "Test Pack")

    def test_expected_findings(self):
        c = codes(self.res)
        for expected in ("missing_dep", "dep_version", "duplicate", "rule_conflict", "mc_mismatch",
                         "loader_mismatch", "incompatible", "bad_jar", "config_invalid", "no_metadata",
                         "recommended_deps"):
            self.assertIn(expected, c, expected)

    def test_no_false_positives(self):
        text = " ".join(f.title + f.detail for f in self.res.findings)
        self.assertNotIn("fabric-resource-loader-v0", text)  # satisfied by jar-in-jar
        self.assertNotIn("uses_fabric_modules", " ".join(self.titles("missing_dep")))
        self.assertNotIn("filler", text)
        self.assertEqual([t for t in self.titles("missing_dep") if "Fabric API" in t], [])

    def test_duplicate_keeps_newest(self):
        dup = next(f for f in self.res.findings if f.code == "duplicate")
        disabled = [a.params["file"] for a in dup.fixes[0].actions]
        self.assertEqual(disabled, ["mods/lithium-0.11.1-old.jar"])
        self.assertTrue(dup.fixes[0].auto)

    def test_score(self):
        self.assertLess(self.res.score, 60)

    def test_client_only_on_server(self):
        res = pipeline.run_analysis(str(self.root), Options(side="server"))
        self.assertIn("client_only", codes(res))

    def test_ram_and_config_rules(self):
        (self.root / "server.properties").write_text("view-distance=24\nsimulation-distance=4\n")
        for i in range(60):
            fakepack.fabric_jar(self.root / "mods" / f"extra{i}.jar", f"extra{i}", "1", {"minecraft": "~1.20.1"})
        try:
            res = pipeline.run_analysis(str(self.root), Options(side="client"))
            self.assertIn("ram", codes(res))
            vals = [f for f in res.findings if f.code == "config_value"]
            self.assertEqual(len(vals), 1)  # only view-distance exceeds its threshold
            self.assertIn("view-distance", vals[0].title)
        finally:
            for i in range(60):
                (self.root / "mods" / f"extra{i}.jar").unlink()
            (self.root / "server.properties").unlink()

    def test_missing_fabric_api_is_one_finding(self):
        d = Path(tempfile.mkdtemp()) / "i"
        (d / "mods").mkdir(parents=True)
        for n in ("a", "b"):
            fakepack.fabric_jar(d / "mods" / f"{n}.jar", n, "1", {"minecraft": "~1.20.1", "fabric-api": "*",
                                                                   "fabric-resource-loader-v0": "*",
                                                                   "fabric-lifecycle-events-v1": "*"})
        res = pipeline.run_analysis(str(d), Options(mc="1.20.1", loader="fabric"))
        missing = [f for f in res.findings if f.code == "missing_dep"]
        self.assertEqual(len(missing), 1)
        self.assertIn("Fabric API", missing[0].title)
        self.assertEqual(missing[0].fixes[0].actions[0].params["slug"], "fabric-api")

    def test_inference_when_no_launcher_data(self):
        d = Path(tempfile.mkdtemp()) / "i"
        (d / "mods").mkdir(parents=True)
        for i in range(6):
            fakepack.forge_jar(d / "mods" / f"m{i}.jar", f"m{i}", "1.0", [("minecraft", True, "[1.20.1]"), ("forge", True, "[47,)")])
        fakepack.forge_jar(d / "mods" / "old.jar", "old", "1.0", [("minecraft", True, "[1.19.2]")])
        res = pipeline.run_analysis(str(d), Options())
        self.assertEqual((res.pack.loader, res.pack.mc_version), ("forge", "1.20.1"))
        self.assertTrue(any("inferred" in n for n in res.pack.notes))
        self.assertIn("mc_mismatch", codes(res))

    def test_neoforge_accepts_forge_only_on_1201(self):
        d = Path(tempfile.mkdtemp()) / "i"
        (d / "mods").mkdir(parents=True)
        fakepack.forge_jar(d / "mods" / "f.jar", "fm", "1.0", [("forge", True, "[47,)")])
        ok = pipeline.run_analysis(str(d), Options(mc="1.20.1", loader="neoforge"))
        self.assertNotIn("loader_mismatch", codes(ok))
        self.assertNotIn("missing_dep", codes(ok))
        bad = pipeline.run_analysis(str(d), Options(mc="1.21.1", loader="neoforge"))
        self.assertIn("loader_mismatch", codes(bad))

    def test_connector_softens_fabric_mods_on_forge(self):
        d = Path(tempfile.mkdtemp()) / "i"
        (d / "mods").mkdir(parents=True)
        fakepack.forge_jar(d / "mods" / "connector.jar", "connector", "1.0", [])
        fakepack.fabric_jar(d / "mods" / "fm.jar", "fm", "1.0", {"minecraft": "~1.20.1"})
        res = pipeline.run_analysis(str(d), Options(mc="1.20.1", loader="forge"))
        lm = [f for f in res.findings if f.code == "loader_mismatch"]
        self.assertEqual([f.severity for f in lm], ["warning"])

    def test_redundant_and_loader_preference(self):
        d = Path(tempfile.mkdtemp()) / "i"
        (d / "mods").mkdir(parents=True)
        for mid in ("sodium", "embeddium", "jei", "emi"):
            fakepack.forge_jar(d / "mods" / f"{mid}.jar", mid, "1.0", [])
        res = pipeline.run_analysis(str(d), Options(mc="1.20.1", loader="forge"))
        sod = next(f for f in res.findings if f.code == "rule_redundant" and "sodium" in f.title)
        self.assertTrue(sod.fixes[0].label.startswith("Keep embeddium"))
        self.assertTrue(any(f.code == "rule_redundant" and "jei" in f.title for f in res.findings))


class FixerTests(unittest.TestCase):
    def setUp(self):
        self.root = fakepack.make_fabric_instance(Path(tempfile.mkdtemp()) / "inst")
        self.res = pipeline.run_analysis(str(self.root), Options())

    def test_disable_and_undo_roundtrip(self):
        actions = [a for a in pipeline.recommended_actions(self.res) if a.kind == "disable_mod"]
        self.assertTrue(actions)
        cs = pipeline.apply(self.res, actions)
        self.assertEqual(cs["errors"], [])
        self.assertTrue((self.root / "mods/lithium-0.11.1-old.jar.disabled").exists())
        self.assertFalse((self.root / "mods/lithium-0.11.1-old.jar").exists())
        again = pipeline.run_analysis(str(self.root), Options())
        self.assertNotIn("duplicate", codes(again))
        fixer.undo(self.root)
        self.assertTrue((self.root / "mods/lithium-0.11.1-old.jar").exists())
        with self.assertRaises(fixer.FixError):
            fixer.undo(self.root)

    def test_path_traversal_is_refused(self):
        outside = self.root.parent / "outside.jar"
        outside.write_bytes(b"x")
        cs = fixer.apply_actions(self.root, [Action("disable_mod", {"file": "../outside.jar"})])
        self.assertTrue(cs["errors"])
        self.assertTrue(outside.exists())

    def test_edit_properties_and_undo(self):
        p = self.root / "server.properties"
        p.write_text("motd=hi\nview-distance=32\n")
        a = Action("edit_config", {"file": "server.properties", "format": "properties", "key": "view-distance", "value": "10"})
        cs = fixer.apply_actions(self.root, [a])
        self.assertEqual(cs["errors"], [])
        self.assertEqual(p.read_text(), "motd=hi\nview-distance=10\n")
        fixer.undo(self.root)
        self.assertEqual(p.read_text(), "motd=hi\nview-distance=32\n")

    def test_edit_toml_keeps_comments_and_validates(self):
        text = '# header\n[a]\nx = 1 # keep\n[b]\nx = 2\n'
        self.assertEqual(fixer.edit_toml(text, "x", "9", "b"), '# header\n[a]\nx = 1 # keep\n[b]\nx = 9\n')
        self.assertEqual(fixer.edit_toml(text, "x", "9", "a"), '# header\n[a]\nx = 9 # keep\n[b]\nx = 2\n')
        with self.assertRaises(fixer.FixError):
            fixer.edit_toml(text, "missing", "1", "a")
        p = self.root / "config" / "t.toml"
        p.write_text("[s]\nv = 1\n")
        cs = fixer.apply_actions(self.root, [Action("edit_config", {"file": "config/t.toml", "key": "v", "value": "= =", "table": "s"})])
        self.assertTrue(cs["errors"])  # invalid result is rejected
        self.assertEqual(p.read_text(), "[s]\nv = 1\n")

    def test_edit_json(self):
        p = self.root / "config" / "x.json"
        p.write_text('{"a": {"b": 1}}')
        cs = fixer.apply_actions(self.root, [Action("edit_config", {"file": "config/x.json", "key": "a.b", "value": "5"})])
        self.assertEqual(cs["errors"], [])
        self.assertEqual(json.loads(p.read_text()), {"a": {"b": 5}})

    def test_install_refuses_unverified_and_non_modrinth(self):
        a = Action("install_mod", {"url": "https://example.com/x.jar", "filename": "x.jar", "sha1": "abc"})
        cs = fixer.apply_actions(self.root, [a])
        self.assertTrue(cs["errors"])
        b = Action("install_mod", {"url": "https://cdn.modrinth.com/x.jar", "filename": "x.exe", "sha1": "abc"})
        self.assertTrue(fixer.apply_actions(self.root, [b])["errors"])
        c = Action("install_mod", {"url": "https://cdn.modrinth.com/x.jar", "filename": "x.jar"})
        self.assertIn("hash", fixer.apply_actions(self.root, [c])["errors"][0])


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv_dir = Path(tempfile.mkdtemp())
        fakepack.fabric_jar(cls.srv_dir / "alpha.jar", "alpha", "1.0.0", {"minecraft": "~1.20.1", "beta": ">=2"})
        fakepack.fabric_jar(cls.srv_dir / "beta.jar", "beta", "1.0.0", {"minecraft": "~1.20.1"})
        handler = functools.partial(_Handler, directory=str(cls.srv_dir))
        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        net.ALLOW_LOCAL_HTTP = True
        cls.old_proxy = {k: os.environ.pop(k, None) for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")}

    @classmethod
    def tearDownClass(cls):
        net.ALLOW_LOCAL_HTTP = False
        cls.httpd.shutdown()
        for k, v in cls.old_proxy.items():
            if v:
                os.environ[k] = v

    def _sha1(self, name):
        return hashlib.sha1((self.srv_dir / name).read_bytes()).hexdigest()

    def test_mrpack(self):
        index = {"formatVersion": 1, "game": "minecraft", "name": "Tiny Pack", "versionId": "1",
                 "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.15.7"},
                 "files": [
                     {"path": "mods/alpha.jar", "hashes": {"sha1": self._sha1("alpha.jar")},
                      "downloads": [f"http://127.0.0.1:{self.port}/alpha.jar"]},
                     {"path": "mods/beta.jar", "hashes": {"sha1": self._sha1("beta.jar")},
                      "downloads": [f"http://127.0.0.1:{self.port}/beta.jar"]},
                     {"path": "mods/tampered.jar", "hashes": {"sha1": "0" * 40},
                      "downloads": [f"http://127.0.0.1:{self.port}/beta.jar"]},
                     {"path": "../../evil.jar", "hashes": {"sha1": "0" * 40}, "downloads": []},
                     {"path": "mods/offsite.jar", "hashes": {"sha1": "0" * 40}, "downloads": ["https://evil.example/x.jar"]},
                 ]}
        pack = Path(tempfile.mkdtemp()) / "tiny.mrpack"
        with zipfile.ZipFile(pack, "w") as z:
            z.writestr("modrinth.index.json", json.dumps(index))
            z.writestr("overrides/config/test.toml", "[a]\nb = 1\n")
            z.writestr("overrides/../../escape.txt", "no")
            z.writestr("overrides/resourcepacks/big.zip", "ignored")
        ws = Path(tempfile.mkdtemp())
        res = pipeline.run_analysis(str(pack), Options(workspace_dir=ws, mrpack_hosts={"127.0.0.1"}))
        self.assertEqual((res.pack.name, res.pack.mc_version, res.pack.loader, res.pack.loader_version),
                         ("Tiny Pack", "1.20.1", "fabric", "0.15.7"))
        self.assertEqual(res.pack.mod_count, 2)  # alpha + beta; the hash-mismatched download is rejected
        self.assertTrue((res.pack.root / "config" / "test.toml").exists())
        self.assertFalse((ws / "escape.txt").exists())
        self.assertFalse((res.pack.root / "resourcepacks").exists())
        self.assertEqual(len(res.pack.unresolved), 3)
        self.assertIn("unresolved", codes(res))

    def test_curseforge_with_stubbed_api(self):
        manifest = {"name": "CF Pack", "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.2.0", "primary": True}]},
                    "files": [{"projectID": 1, "fileID": 11, "required": True}, {"projectID": 2, "fileID": 22, "required": True}],
                    "overrides": "overrides"}
        pack = Path(tempfile.mkdtemp()) / "cf.zip"
        with zipfile.ZipFile(pack, "w") as z:
            z.writestr("manifest.json", json.dumps(manifest))
            z.writestr("overrides/config/c.toml", "x=1\n")
        # without a key: no network, explanatory note
        res = pipeline.run_analysis(str(pack), Options(workspace_dir=Path(tempfile.mkdtemp())))
        self.assertEqual((res.pack.loader, res.pack.loader_version, res.pack.mc_version), ("forge", "47.2.0", "1.20.1"))
        self.assertTrue(any("API key" in u for u in res.pack.unresolved))

        fakepack.forge_jar(self.srv_dir / "cfmod.jar", "cfmod", "1.0", [("minecraft", True, "[1.20.1]")])
        sha = self._sha1("cfmod.jar")
        original = curseforge.resolve_files
        curseforge.resolve_files = lambda ids, key: [
            {"id": 11, "fileName": "cfmod.jar", "downloadUrl": f"http://127.0.0.1:{self.port}/cfmod.jar",
             "hashes": [{"algo": 1, "value": sha}]},
            {"id": 22, "fileName": "blocked.jar", "downloadUrl": None, "hashes": []}]
        try:
            res = pipeline.run_analysis(str(pack), Options(workspace_dir=Path(tempfile.mkdtemp()), cf_key="k",
                                                           mrpack_hosts={"127.0.0.1"}))
        finally:
            curseforge.resolve_files = original
        self.assertEqual(res.pack.mod_count, 1)
        self.assertTrue(any("blocked.jar" in u and "manually" in u for u in res.pack.unresolved))

    def test_bad_inputs(self):
        with self.assertRaises(SourceError):
            open_source("/definitely/not/here", Options())
        junk = Path(tempfile.mkdtemp()) / "junk.zip"
        with zipfile.ZipFile(junk, "w") as z:
            z.writestr("hello.txt", "x")
        with self.assertRaises(SourceError):
            open_source(str(junk), Options())

    def test_safe_relpath(self):
        for bad in ("../x", "/etc/passwd", "a/../../b", "C:/x", "", "a//b"):
            self.assertIsNone(safe_relpath(bad), bad)
        self.assertEqual(safe_relpath("mods/a.jar").as_posix(), "mods/a.jar")


class OnlineEnrichTests(unittest.TestCase):
    def test_updates_and_replacements(self):
        root = fakepack.make_fabric_instance(Path(tempfile.mkdtemp()) / "inst")
        res = pipeline.run_analysis(str(root), Options())
        by_name = {j.rel: j for j in res.jars}
        sod, old = by_name["mods/sodium-0.5.3.jar"], by_name["mods/old-mc-mod.jar"]

        def fv(pid, vid, num):
            return {"id": vid, "project_id": pid, "version_number": num}

        def newv(pid, vid, num, fn):
            return {**fv(pid, vid, num), "files": [{"primary": True, "filename": fn, "url": f"https://cdn.modrinth.com/{fn}",
                                                    "hashes": {"sha1": "a" * 40, "sha512": "b" * 128}}]}

        saved = (modrinth.versions_by_hash, modrinth.latest_by_hash, modrinth.project_titles)
        modrinth.versions_by_hash = lambda hs, algorithm="sha1": {
            sod.sha1: fv("P1", "v1", "0.5.3"), old.sha1: fv("P2", "v2", "3.0.0")}
        modrinth.latest_by_hash = lambda hs, loaders, gv, algorithm="sha1": {
            sod.sha1: newv("P1", "v9", "0.5.8", "sodium-0.5.8.jar"), old.sha1: newv("P2", "v8", "3.1.0", "old-mc-mod-3.1.0.jar")}
        modrinth.project_titles = lambda ids: {"P1": {"title": "Sodium"}, "P2": {"title": "Old Mod"}}
        try:
            enrich.add_update_findings(res.pack, res.jars, res.findings, None)
        finally:
            modrinth.versions_by_hash, modrinth.latest_by_hash, modrinth.project_titles = saved
        upd = [f for f in res.findings if f.code == "update"]
        self.assertEqual(len(upd), 1)
        self.assertIn("0.5.3 -> 0.5.8", upd[0].title)
        self.assertFalse(upd[0].fixes[0].auto)
        mm = next(f for f in res.findings if f.code == "mc_mismatch")
        self.assertEqual(mm.fixes[0].actions[0].kind, "replace_mod")  # replacement offered first
        self.assertTrue(mm.fixes[0].auto)


class ReportTests(unittest.TestCase):
    def test_formats(self):
        root = fakepack.make_fabric_instance(Path(tempfile.mkdtemp()) / "inst")
        res = pipeline.run_analysis(str(root), Options())
        self.assertIn("Health score", report.to_text(res))
        self.assertIn("# Modpack report", report.to_markdown(res))
        data = json.loads(report.to_json(res))
        self.assertEqual(data["pack"]["loader"], "fabric")
        self.assertTrue(data["findings"])


if __name__ == "__main__":
    unittest.main()

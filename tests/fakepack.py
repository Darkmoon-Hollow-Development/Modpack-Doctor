"""Build synthetic mod jars and instances for tests (no real mods needed)."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content if isinstance(content, (bytes, str)) else json.dumps(content))
    return buf.getvalue()


def fabric_jar(path: Path, mod_id, version, depends=None, breaks=None, conflicts=None, recommends=None,
               provides=None, env="*", nested=None, name=None):
    doc = {"schemaVersion": 1, "id": mod_id, "version": version, "name": name or mod_id,
           "depends": depends or {}, "environment": env}
    if breaks:
        doc["breaks"] = breaks
    if conflicts:
        doc["conflicts"] = conflicts
    if recommends:
        doc["recommends"] = recommends
    if provides:
        doc["provides"] = provides
    files = {"fabric.mod.json": doc}
    for n, data in (nested or {}).items():
        files[f"META-INF/jars/{n}"] = data
    path.write_bytes(_zip_bytes(files))


def forge_jar(path: Path, mod_id, version, deps=None, loader="forge", manifest_version=None, client_only=False):
    """deps: list of (modid, mandatory_or_type, versionRange)."""
    lines = ['modLoader="javafml"', 'loaderVersion="[47,)"', 'license="MIT"', "", "[[mods]]",
             f'modId="{mod_id}"', f'version="{version}"', f'displayName="{mod_id}"', ""]
    allv = list(deps or [])
    if client_only:
        allv = [("minecraft", True, "[1.20.1]", "CLIENT"), ("forge", True, "[47,)", "CLIENT")] + allv
    for d in allv:
        dep, mand, rng = d[0], d[1], d[2]
        side = d[3] if len(d) > 3 else "BOTH"
        lines += [f'[[dependencies.{mod_id}]]', f'modId="{dep}"']
        if isinstance(mand, str):
            lines.append(f'type="{mand}"')
        else:
            lines.append(f"mandatory={'true' if mand else 'false'}")
        lines += [f'versionRange="{rng}"', 'ordering="NONE"', f'side="{side}"', ""]
    name = "META-INF/neoforge.mods.toml" if loader == "neoforge" else "META-INF/mods.toml"
    files = {name: "\n".join(lines)}
    if manifest_version:
        files["META-INF/MANIFEST.MF"] = f"Manifest-Version: 1.0\nImplementation-Version: {manifest_version}\n"
    path.write_bytes(_zip_bytes(files))


def plain_jar(path: Path):
    path.write_bytes(_zip_bytes({"some/Library.class": b"\xca\xfe\xba\xbe"}))


def make_fabric_instance(root: Path) -> Path:
    """A deliberately broken Fabric 1.20.1 client instance."""
    mods = root / "mods"
    mods.mkdir(parents=True)
    (root / "mmc-pack.json").write_text(json.dumps({"formatVersion": 1, "components": [
        {"uid": "net.minecraft", "version": "1.20.1"},
        {"uid": "net.fabricmc.fabric-loader", "version": "0.15.7"}]}))
    (root / "instance.cfg").write_text("name=Test Pack\nMaxMemAlloc=2048\n")
    mc = {"minecraft": "~1.20.1", "fabricloader": ">=0.14.21"}

    fabric_jar(mods / "fabric-api-0.92.jar", "fabric-api", "0.92.0+1.20.1", {**mc}, provides=["fabric"],
               nested={"fabric-api-base-0.4.31.jar": _zip_bytes({"fabric.mod.json": {
                   "schemaVersion": 1, "id": "fabric-api-base", "version": "0.4.31", "depends": {}}}),
                       "fabric-resource-loader-v0-0.11.jar": _zip_bytes({"fabric.mod.json": {
                           "schemaVersion": 1, "id": "fabric-resource-loader-v0", "version": "0.11.10", "depends": {}}})})
    fabric_jar(mods / "sodium-0.5.3.jar", "sodium", "0.5.3", {**mc})
    fabric_jar(mods / "iris-1.6.4.jar", "iris", "1.6.4", {**mc, "sodium": ">=0.5.0"})
    fabric_jar(mods / "lithium-0.11.2.jar", "lithium", "0.11.2", {**mc})
    fabric_jar(mods / "lithium-0.11.1-old.jar", "lithium", "0.11.1", {**mc})  # duplicate
    fabric_jar(mods / "needs-lib.jar", "needs_lib", "1.0.0", {**mc, "cool-lib": ">=2.0"})  # missing dep
    fabric_jar(mods / "needs-new-sodium.jar", "needs_new_sodium", "1.0.0", {**mc, "sodium": ">=0.6"})  # wrong version
    fabric_jar(mods / "uses-fabric-modules.jar", "uses_fabric_modules", "1.0.0",
               {**mc, "fabric-resource-loader-v0": "*", "fabric-api-base": "*"})  # satisfied via jar-in-jar
    fabric_jar(mods / "optifabric.jar", "optifabric", "1.14.0", {**mc})  # conflicts with sodium
    fabric_jar(mods / "old-mc-mod.jar", "old_mc_mod", "3.0.0", {"minecraft": "~1.19.2", "fabricloader": "*"})
    forge_jar(mods / "forge-only.jar", "forge_only", "1.0.0", [("minecraft", True, "[1.20.1]")])  # wrong loader
    fabric_jar(mods / "breaker-a.jar", "breaker_a", "1.0.0", {**mc}, breaks={"breaker_b": "*"})
    fabric_jar(mods / "breaker-b.jar", "breaker_b", "1.0.0", {**mc})
    fabric_jar(mods / "client-thing.jar", "client_thing", "1.0.0", {**mc}, env="client")
    fabric_jar(mods / "recommender.jar", "recommender", "1.0.0", {**mc}, recommends={"nice-addon": "*"})
    plain_jar(mods / "some-library.jar")
    (mods / "corrupt.jar").write_bytes(b"this is not a zip")
    (mods / "already-off.jar.disabled").write_bytes(b"x")
    for i in range(12):  # enough mods for suggestions to apply
        fabric_jar(mods / f"filler{i}.jar", f"filler{i}", "1.0.0", {**mc})

    cfg = root / "config"
    cfg.mkdir()
    (cfg / "good.toml").write_text('[general]\nvalue = 5\n')
    (cfg / "bad.toml").write_text('[general\nvalue = = 5\n')
    (cfg / "ok.json").write_text('{"a": 1, // comment\n "b": [1,2,],}')
    (cfg / "bad.json").write_text('{"a": ')
    return root

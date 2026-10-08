# Modpack Doctor

A desktop app (and command-line tool) that scans a Minecraft modpack and tells you what is wrong with it, why, and
what to do about it. It supports **Forge, NeoForge, Fabric and Quilt** packs and has no dependencies beyond Python.

## Run it

You need Python 3.11 or newer. On Windows and macOS, `tkinter` (the GUI toolkit) is included; on Debian/Ubuntu run
`sudo apt install python3-tk` once.

```
python -m modpack_doctor                     # opens the app
python -m modpack_doctor gui "C:\path\to\instance"   # opens the app and loads a pack
```

On Windows you can also double-click `ModpackDoctor.pyw`. Want to try it safely first?

```
python tools/make_demo_pack.py demo-pack     # creates a deliberately broken sample instance
python -m modpack_doctor gui demo-pack
```

## What you can open

| Input | Notes |
|---|---|
| **Instance folder** | Prism, MultiMC, CurseForge app, Modrinth app, a server folder, or any folder with `mods/` in it. Minecraft version, loader and RAM are read from the launcher's files when present. |
| **Modrinth `.mrpack`** | Mods are downloaded from the hosts the pack format allows, checked against the hashes in the pack, and analysed. |
| **CurseForge pack `.zip`** | These only *list* mods, so downloading them needs a free API key from console.curseforge.com (paste it in the app; it is never saved). Without a key, only mods bundled in the pack are analysed, and the app says so. Authors who disable third-party downloads must be fetched manually. |

Packs are unpacked into `~/ModpackDoctor/workspaces/`; your original file is never changed.

## What it checks

- **Dependencies:** missing required mods, wrong dependency versions, wrong Minecraft or loader version, recommended
  companions, and libraries bundled inside other jars (jar-in-jar) are understood.
- **Loader problems:** mods built for another loader (Sinytra Connector is recognised and downgrades Fabric-on-Forge to a warning).
- **Conflicts:** incompatibilities the mods declare themselves, duplicate mod ids, and a built-in list of known
  clashes and redundant mods (OptiFine vs Sodium family, Sodium/Embeddium/Rubidium, Phosphor vs Starlight, several
  recipe viewers or minimaps).
- **Client/server:** client-only mods in a server pack.
- **Configs:** `.toml`/`.json` files in `config/` that do not parse, server view/simulation distance in big packs, and
  RAM guidance for the pack's size.
- **Performance suggestions:** missing optimizers such as Sodium/Embeddium, Lithium, FerriteCore and ModernFix.
- **Updates (optional, uses the internet):** asks Modrinth for newer builds and for replacements of mods that don't fit the pack.

Every finding has a plain-language explanation. The **health score** is a quick summary (errors weigh more than
warnings); it is not a guarantee that the game will launch.

## Fixing

- **Auto-fix pack** (button next to *Analyze*, also under Tools): applies every recommended fix in one go. It shows what
  it is about to do and asks once, then re-checks the pack and repeats (up to 3 rounds) while more can be fixed safely.
  If nothing is loaded yet it scans first. Afterwards it reports the before/after health score and anything left that
  needs your decision. Some recommended fixes download a missing library from Modrinth; failures are reported, not hidden.
- Or fix things one at a time: click a finding, then a button under *What you can do*.
- Mods are **never deleted**: "disable" renames `x.jar` to `x.jar.disabled`.
- Edited config files are backed up first; everything goes into `.modpack_doctor/` inside the instance.
- **Undo last fix** reverses the most recent run, and an Auto-fix run is undone as a whole. Downloads only come from
  Modrinth and are hash-verified.

## Light and dark mode

Pick **System**, **Light** or **Dark** with the *Theme* selector in the options box (or View > Theme). *System* follows
Windows/macOS; your choice is remembered. On Windows the menu bar at the very top stays in the system colours.

## Tabs and saved packs

- **Tabs:** every pack opens in its own tab, so you can keep several packs side by side. Each tab keeps its own
  results, filters, undo history and options. **Ctrl+T** new tab, **Ctrl+W** close, **Ctrl+Shift+T** reopen a closed
  tab, **Ctrl+Tab** / **Ctrl+Shift+Tab** switch, **Ctrl+1..9** jump. Right-click a tab to rename, duplicate, save or
  close others; middle-click closes. A dot on a tab means a scan finished while you were looking at another tab.
- **Your tabs come back:** when you start the app again, the tabs you had open reopen (File > *Reopen my tabs on
  startup* turns this off). Reopened tabs are not rescanned until you press Analyze, except the active tab when it is a
  local folder.
- **Saved packs:** *Saved packs* (next to the folder buttons, also under File, **Ctrl+D**) remembers a folder or pack
  file together with its Minecraft version / loader / side settings under a name you choose. Saved packs show a star
  on their tab, open in one click and are scanned straight away when they are local folders. *Manage saved packs...*
  renames and removes them. A *Recent* list remembers the last ten packs you opened.
- Everything is stored in `~\.modpack_doctor` (`tabs.json`, `library.json`). The CurseForge API key is never saved.
- Only one scan or fix runs at a time; you can switch tabs meanwhile and the result lands in the tab it belongs to.
- From the command line: `python -m modpack_doctor gui "C:\packs\one" "C:\packs\two"` opens one tab each.

## Command line

```
python -m modpack_doctor analyze <folder|.mrpack|.zip> [--format text|markdown|json] [--out report.md] [--online]
python -m modpack_doctor fix <folder> [--yes]
python -m modpack_doctor undo <folder>
```

`analyze` exits with code 1 when errors are found, so it can be used in CI.

## Teaching it more

Compatibility knowledge lives in `modpack_doctor/data/rules.json` (known conflicts, redundant mods, recommended mods,
config value rules, RAM tiers). Add your own in a separate JSON file and pick it under *Extra rules file* in the app
(or `--rules` on the command line); its lists are appended to the built-in ones.

## Limits worth knowing

- It reads mod **metadata**; it cannot detect every crash (for example mixin conflicts or code-level incompatibilities
  that no mod declares). Treat a clean result as "no known problems", not "will run".
- Config analysis is rule-based: syntax checks plus the value rules in `rules.json`. It does not understand every
  mod's settings, so extend the rules for the mods your packs use.
- The built-in known-conflict list is a starter set.

## Windows installer

The packaging files are ready, but the installer has to be built on Windows (PyInstaller cannot cross-compile).

**Easiest: on a Windows PC**, install Python 3.11+ (tick "Add python.exe to PATH"), then double-click
`build_windows.bat` in this folder. It builds the program with PyInstaller, installs Inno Setup through `winget` if it
is missing, and produces `dist\ModpackDoctor-Setup-<version>.exe`.

**Or on GitHub:** push this folder to a repository and run the *Windows installer* workflow (Actions tab, or push a
tag like `v0.1.0`). It runs the tests, builds the installer and attaches it as a downloadable artifact.

What you get: a per-user installer (no admin prompt), a Start-menu entry, an optional desktop shortcut and a normal
uninstaller. Your settings (`~\.modpack_doctor`) and unpacked packs (`~\ModpackDoctor`) are left alone on uninstall.
The installer is unsigned, so Windows SmartScreen may show "unknown publisher" (More info, then Run anyway); removing
that needs a code-signing certificate.

Files: `ModpackDoctor.spec` (PyInstaller), `installer/ModpackDoctor.iss` (Inno Setup), `installer/ModpackDoctor.ico`,
`build_windows.bat`, `.github/workflows/windows-installer.yml`. Bump the version in `modpack_doctor/__init__.py`
and `pyproject.toml`; the installer picks it up automatically.

## Tests

```
python -m unittest discover -s tests          # engine tests (the GUI tests are skipped without tkinter/a display)
xvfb-run python -m unittest discover -s tests # Linux: include the GUI tests
```

"""Desktop app for Modpack Doctor (tkinter: ships with Python on Windows and macOS).

All slow work (scanning, downloading, applying fixes) runs on a worker thread and reports back through a
queue that the Tk main loop polls, so the window never freezes.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from . import __version__, crashlog, fixer, net, pipeline, viewmodel as vm
from .models import LOADERS, Action, Fix
from .rules import load_rules
from .sources import Options, SourceError
from .widgets import Card, PillNotebook, RoundButton, TabPill, mix

SEV_TITLES = {"error": "Errors", "warning": "Warnings", "info": "Tips"}

WELCOME = """Welcome to Modpack Doctor

It checks a Minecraft modpack for mods that cannot work together, missing or wrong-version dependencies, \
mods built for the wrong loader or Minecraft version, broken config files, and common performance gaps.

How to use it
  1. Pick your modpack:
       - "Instance folder..." : the folder that contains your "mods" folder (Prism, MultiMC, CurseForge app, \
Modrinth app, a server folder, or any folder with mods in it)
       - "Pack file..." : a Modrinth .mrpack or a CurseForge modpack .zip
  2. Press Analyze (F5).
  3. Click a finding to see why it matters and what you can do. Or press "Auto-fix pack" to let the \
app apply all the safe fixes in one go.

Safety
  - Mods are never deleted: "disabling" a mod renames it to <name>.jar.disabled.
  - Edited config files are backed up first, and every fix run can be undone with "Undo last fix".
  - Pack files (.mrpack / .zip) are unpacked into a working copy; your original file is never modified.

CurseForge packs only list their mods, so downloading them needs a free CurseForge API key \
(console.curseforge.com). Without a key, only mods bundled inside the pack are analysed.
"""


def _enable_hidpi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - purely cosmetic
            pass


def open_in_file_manager(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as e:
        messagebox.showerror("Modpack Doctor", f"Could not open {path}:\n{e}")


def reveal_in_file_manager(path: Path) -> None:
    """Open the folder containing ``path`` with the file selected where the platform supports it."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError as e:
        messagebox.showerror("Modpack Doctor", f"Could not open {path.parent}:\n{e}")


RIGHT_CLICK = "<Button-2>" if sys.platform == "darwin" else "<Button-3>"
MIDDLE_CLICK = "<Button-3>" if sys.platform == "darwin" else "<Button-2>"


class Tab:
    """One open pack. The window's widgets show the active tab; every other tab is parked in one of these."""

    def __init__(self, spec: Optional[vm.TabSpec] = None):
        sp = spec or vm.TabSpec()
        self.source, self.custom_title = sp.source, sp.title
        self.mc, self.loader, self.side, self.online = sp.mc, sp.loader, sp.side, sp.online
        self.result = None
        self.undo_groups: list = []  # one list of change-set ids per fix run (an auto-fix run spans several)
        self.attention = False  # a background task finished while this tab was not showing
        self.crash = None  # (path, CrashReport) of the last crash analysis
        self.view: dict = {}  # filters / sub-tab to put back when the tab is shown again

    def spec(self) -> vm.TabSpec:
        return vm.TabSpec(self.source, self.custom_title, self.mc, self.loader, self.side, self.online)

    def title(self) -> str:
        if self.custom_title:
            return self.custom_title
        if self.result is not None and self.result.pack.name:
            return self.result.pack.name
        return vm.default_title(self.source)


class App(tk.Tk):
    def __init__(self, initial=None, *, restore: bool = True, autoscan: bool = False):
        """``initial``: a path or a list of paths to open as tabs. ``restore``: bring back the tabs from last time.
        ``autoscan``: analyse the active tab at start-up if it is a local folder (cheap, no network)."""
        super().__init__()
        self.title("Modpack Doctor")
        self.geometry("1200x820")
        self.minsize(960, 720)
        self._set_icon()

        self.settings = vm.load_settings()
        self.base_rules = load_rules()
        self.rules = self.base_rules
        self.tabs: list = [Tab()]
        self.tab: Tab = self.tabs[0]
        self._closed: list = []  # specs of recently closed tabs, newest last
        self._task_tab: Optional[Tab] = None  # the tab a finishing background task belongs to
        self._busy_tab: Optional[Tab] = None
        self._loading_tab = False
        self.library = vm.load_library()
        self._saved_win = None
        self.busy = False
        self._queue: "queue.Queue" = queue.Queue()
        self._finding_by_iid: dict = {}
        self._mod_rows: list = []
        self._mod_sort = ("issue", False)

        # These variables show the *active* tab; switching tabs copies them to/from the Tab objects.
        self.src_var = tk.StringVar()
        self.mc_var = tk.StringVar()
        self.loader_var = tk.StringVar(value="auto")
        self.side_var = tk.StringVar(value="auto")
        self.online_var = tk.BooleanVar(value=False)
        self.restore_var = tk.BooleanVar(value=bool(self.settings.get("restore_tabs", True)))
        self.cf_var = tk.StringVar(value=os.environ.get("CURSEFORGE_API_KEY", ""))
        self.rules_var = tk.StringVar(value=self.settings.get("rules", ""))
        self.sev_vars = {s: tk.BooleanVar(value=True) for s in ("error", "warning", "info")}
        self.cat_var = tk.StringVar(value=vm.ALL_CATEGORIES)
        self.search_var = tk.StringVar()
        self.mod_search_var = tk.StringVar()
        self.mod_issue_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready. Choose a modpack to analyse.")
        theme = self.settings.get("theme", "System")
        self.theme_var = tk.StringVar(value=theme if theme in vm.THEME_CHOICES else "System")
        self.pal = vm.PALETTES[vm.resolve_theme(self.theme_var.get())]
        self.bg = self.pal["bg"]
        self._score_shown: Optional[int] = None
        self._themed_texts: list = []
        self._combos: list = []
        self._menus: list = []
        self._cards: list = []  # custom-drawn widgets that re-colour themselves on a theme change
        self._rbuttons: list = []
        self._page_frames: list = []

        self._build_style()
        self._build_menu()
        self._build_tabbar()
        self._build_header()
        self._build_body()
        self._build_status()
        self._bind_keys()
        self._show_welcome()
        self._apply_theme()
        self._init_tabs(initial, restore, autoscan)
        self._refresh_buttons()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_id = self.after(80, self._poll)

    # The pack-specific state lives on the active tab; these keep older call sites simple.
    @property
    def result(self):
        return self.tab.result

    @result.setter
    def result(self, value) -> None:
        self.tab.result = value

    @property
    def _undo_groups(self) -> list:
        return self.tab.undo_groups

    def destroy(self) -> None:
        try:
            self.after_cancel(self._poll_id)
        except (AttributeError, tk.TclError):
            pass
        super().destroy()

    # ------------------------------------------------------------------ styling
    @staticmethod
    def _pick_font_family(root) -> Optional[str]:
        """The platform's modern UI font if it is installed (Segoe UI on Windows, SF on macOS, then common Linux ones)."""
        have = {f.lower(): f for f in tkfont.families(root)}
        for name in ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", ".AppleSystemUIFont", "Helvetica Neue",
                     "Inter", "Noto Sans", "Cantarell", "Ubuntu", "Open Sans"):
            if name.lower() in have:
                return have[name.lower()]
        return None

    def _build_style(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        base = tkfont.nametofont("TkDefaultFont")
        fam = self._pick_font_family(self)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                f = tkfont.nametofont(name)
                if fam:
                    f.configure(family=fam)
                f.configure(size=max(10, abs(int(f.cget("size")))) if sys.platform != "darwin" else f.cget("size"))
            except tk.TclError:
                pass
        self.f_base = base
        self.f_bold = base.copy()
        self.f_bold.configure(weight="bold")
        self.f_h1 = base.copy()
        self.f_h1.configure(weight="bold", size=base.cget("size") + 3)
        self.f_big = base.copy()
        self.f_big.configure(weight="bold", size=base.cget("size") + 18)
        self.f_small = base.copy()
        self.f_small.configure(size=max(8, base.cget("size") - 1))
        self.f_mono = tkfont.nametofont("TkFixedFont").copy()
        rowh = base.metrics("linespace") + 11
        st.configure("Treeview", rowheight=rowh, borderwidth=0, relief="flat")
        st.configure("Treeview.Heading", font=self.f_bold, padding=(8, 7), relief="flat", borderwidth=0)
        st.configure("TButton", padding=(14, 6), relief="flat", borderwidth=1)
        st.configure("Accent.TButton", font=self.f_bold, padding=(18, 7), relief="flat", borderwidth=1)
        st.configure("Fix.TButton", padding=(12, 7))
        st.configure("TMenubutton", padding=(14, 7), relief="flat", borderwidth=1)
        st.configure("TEntry", padding=(8, 5), relief="flat", borderwidth=1)
        st.configure("TCombobox", padding=(8, 5), relief="flat", borderwidth=1, arrowsize=14)
        st.configure("TCheckbutton", padding=(2, 3))
        st.configure("TNotebook", borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", padding=(18, 9), borderwidth=0, font=self.f_base)
        st.configure("TScrollbar", arrowsize=13, borderwidth=0, relief="flat")
        st.configure("Count.TLabel", font=self.f_bold)
        st.configure("Section.TLabel", font=self.f_small)

    def _apply_theme(self) -> None:
        """(Re)colour every widget from the palette chosen in the Theme selector."""
        p = self.pal = vm.PALETTES[vm.resolve_theme(self.theme_var.get())]
        self.bg = p["bg"]
        st = ttk.Style(self)
        self.configure(background=p["page"])
        st.configure(".", background=p["bg"], foreground=p["fg"], bordercolor=p["border"], lightcolor=p["bg"],
                     darkcolor=p["bg"], troughcolor=p["trough"], focuscolor=p["accent"], fieldbackground=p["field"],
                     insertcolor=p["fg"], selectbackground=p["select_bg"], selectforeground=p["select_fg"])
        st.map(".", foreground=[("disabled", p["muted"])])
        st.configure("Muted.TLabel", foreground=p["muted"])
        st.configure("TButton", background=p["button"], foreground=p["fg"], lightcolor=p["button"], darkcolor=p["button"],
                     bordercolor=p["border"], focusthickness=0)
        st.map("TButton", background=[("disabled", p["bg"]), ("pressed", p["button_active"]), ("active", p["button_active"])],
               foreground=[("disabled", p["muted"])])
        st.configure("Accent.TButton", background=p["accent"], foreground=p["accent_fg"], lightcolor=p["accent"],
                     darkcolor=p["accent"], bordercolor=p["accent"])
        st.map("Accent.TButton", background=[("disabled", p["button"]), ("pressed", p["accent_active"]),
                                              ("active", p["accent_active"])],
               foreground=[("disabled", p["muted"])], bordercolor=[("disabled", p["border"])])
        st.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"], insertcolor=p["fg"], bordercolor=p["border"],
                     lightcolor=p["field"], darkcolor=p["field"])
        st.configure("TCombobox", fieldbackground=p["field"], background=p["button"], foreground=p["fg"],
                     arrowcolor=p["fg"], bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"],
                     selectbackground=p["field"], selectforeground=p["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", p["field"])], foreground=[("readonly", p["fg"])],
               selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["fg"])],
               background=[("active", p["button_active"])])
        st.configure("TCheckbutton", background=p["bg"], foreground=p["fg"], indicatorbackground=p["field"],
                     indicatorforeground=p["fg"])
        st.map("TCheckbutton", background=[("active", p["bg"])], indicatorbackground=[("selected", p["accent"]),
                                                                                      ("!selected", p["field"])],
               indicatorforeground=[("selected", p["accent_fg"])])
        st.configure("TNotebook", background=p["bg"], bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"])
        st.configure("TNotebook.Tab", background=p["bg"], foreground=p["muted"], bordercolor=p["bg"],
                     lightcolor=p["bg"], darkcolor=p["bg"])
        st.map("TNotebook.Tab", background=[("selected", p["surface"]), ("active", p["button_active"])],
               foreground=[("selected", p["accent"]), ("active", p["fg"])], bordercolor=[("selected", p["border"])],
               lightcolor=[("selected", p["surface"])], darkcolor=[("selected", p["surface"])])
        st.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["fg"],
                     bordercolor=p["surface"], lightcolor=p["surface"], darkcolor=p["surface"])
        st.map("Treeview", background=[("selected", p["select_bg"])], foreground=[("selected", p["select_fg"])])
        st.configure("Treeview.Heading", background=p["surface"], foreground=p["muted"], bordercolor=p["border"],
                     lightcolor=p["surface"], darkcolor=p["surface"], relief="flat")
        st.map("Treeview.Heading", background=[("active", p["button_active"])])
        st.configure("TScrollbar", background=p["button"], troughcolor=p["trough"], bordercolor=p["border"],
                     arrowcolor=p["fg"], lightcolor=p["button"], darkcolor=p["button"])
        st.map("TScrollbar", background=[("active", p["button_active"])])
        st.configure("TLabelframe", background=p["bg"], bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"])
        st.configure("TLabelframe.Label", background=p["bg"], foreground=p["muted"])
        st.configure("Section.TLabel", foreground=p["muted"])
        st.configure("Card.TFrame", background=p["surface"])
        st.configure("TSeparator", background=p["border"])
        st.configure("TProgressbar", background=p["accent"], troughcolor=p["trough"], bordercolor=p["border"],
                     lightcolor=p["accent"], darkcolor=p["accent"])
        st.configure("TMenubutton", background=p["button"], foreground=p["fg"], bordercolor=p["border"],
                     arrowcolor=p["fg"], lightcolor=p["button"], darkcolor=p["button"])
        st.map("TMenubutton", background=[("active", p["button_active"]), ("disabled", p["bg"])],
               foreground=[("disabled", p["muted"])])
        st.configure("TPanedwindow", background=p["bg"])
        st.configure("Sash", background=p["bg"], sashthickness=8, gripcount=0)

        # Drop-down lists of comboboxes are plain tk widgets.
        for opt, key in (("background", "field"), ("foreground", "fg"), ("selectBackground", "select_bg"),
                         ("selectForeground", "select_fg")):
            self.option_add(f"*TCombobox*Listbox.{opt}", p[key])
        for cb in self._combos:
            try:
                pd = self.tk.call("ttk::combobox::PopdownWindow", str(cb))
                self.tk.call(f"{pd}.f.l", "configure", "-background", p["field"], "-foreground", p["fg"],
                             "-selectbackground", p["select_bg"], "-selectforeground", p["select_fg"])
            except tk.TclError:
                pass

        for t in self._themed_texts:
            t.configure(bg=p["surface"], fg=p["fg"], insertbackground=p["fg"], highlightbackground=p["border"],
                        highlightcolor=p["border"], selectbackground=p["select_bg"], selectforeground=p["select_fg"])
        self.detail.tag_configure("muted", foreground=p["muted"])
        for sev in ("error", "warning", "info"):
            self.detail.tag_configure("sev_" + sev, foreground=p[sev])
            self.tree.tag_configure(sev, foreground=p[sev])
            self.mod_tree.tag_configure(sev, foreground=p[sev])
            self.count_lbls[sev].configure(fg=p[sev], bg=p["bg"])
        self.gauge.configure(bg=p["bg"])
        self._draw_gauge(self._score_shown)
        for f in self._page_frames:
            f.configure(bg=p["page"])
        self._status_lbl.configure(bg=p["page"], fg=p["muted"])
        for c in self._cards:
            c.restyle(p)
        for b in [x for x in self._rbuttons if x.winfo_exists()]:
            b.restyle(p)
        self.notebook.restyle(p)
        for m in self._menus:  # honoured on Linux/macOS; Windows draws its own menu bar
            self._style_menu(m)
        self._render_tabbar()

    def _style_menu(self, m: tk.Menu) -> None:
        p = self.pal
        try:
            m.configure(bg=p["surface"], fg=p["fg"], activebackground=p["select_bg"], activeforeground=p["select_fg"],
                        disabledforeground=p["muted"])
        except tk.TclError:
            pass

    def _on_theme_change(self) -> None:
        self._apply_theme()
        self._save_settings()
        self.status_var.set(f"Theme: {self.theme_var.get()}")

    def _set_icon(self) -> None:
        """Window, taskbar and Alt-Tab icon. Purely cosmetic, so any failure is ignored."""
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ModpackDoctor.App")  # own taskbar group
        except Exception:  # noqa: BLE001
            pass
        try:
            import base64
            import pkgutil
            png = pkgutil.get_data("modpack_doctor", "data/icon.png")  # works inside a single-file .pyz too
            self._icon_img = tk.PhotoImage(data=base64.b64encode(png))  # keep a reference or Tk drops it
            self.iconphoto(True, self._icon_img)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ menu / header
    def _build_menu(self) -> None:
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=False)
        f.add_command(label="Open instance folder...", accelerator="Ctrl+O", command=self.pick_folder)
        f.add_command(label="Open pack file (.mrpack / .zip)...", accelerator="Ctrl+Shift+O", command=self.pick_file)
        f.add_cascade(label="Saved packs", menu=self._make_saved_menu(f))
        f.add_separator()
        f.add_command(label="Export report...", accelerator="Ctrl+S", command=self.export)
        f.add_command(label="Copy report as Markdown", command=self.copy_report)
        f.add_command(label="Export mod list...", command=self.export_mod_list)
        f.add_command(label="Save mod list as .txt...", command=self.export_mod_txt)
        f.add_separator()
        f.add_command(label="Quit", command=self._on_close)
        m.add_cascade(label="File", menu=f)
        t = tk.Menu(m, tearoff=False)
        t.add_command(label="Analyze", accelerator="F5", command=self.analyze)
        t.add_command(label="Auto-fix pack", command=self.auto_fix)
        t.add_command(label="Undo last fix", command=self.undo_last)
        t.add_separator()
        t.add_command(label="Open analysed folder", command=self.open_folder)
        t.add_command(label="Show ignored warnings again", command=self.show_ignored_again)
        t.add_command(label="Analyze latest crash report", accelerator="Ctrl+L", command=self.analyze_crash)
        t.add_command(label="Manage disabled mods...", command=self.manage_disabled)
        t.add_separator()
        t.add_command(label="Open mods folder", accelerator="Ctrl+M", command=lambda: self.open_pack_folder("mods"))
        t.add_command(label="Open config folder", accelerator="Ctrl+Shift+C",
                      command=lambda: self.open_pack_folder("config"))
        m.add_cascade(label="Tools", menu=t)
        tm = tk.Menu(m, tearoff=False)
        tm.configure(postcommand=lambda: self._fill_tabs_menu(tm))
        m.add_cascade(label="Tabs", menu=tm)
        v = tk.Menu(m, tearoff=False)
        th = tk.Menu(v, tearoff=False)
        for choice in vm.THEME_CHOICES:
            th.add_radiobutton(label=choice, variable=self.theme_var, value=choice, command=self._on_theme_change)
        v.add_cascade(label="Theme", menu=th)
        v.add_checkbutton(label="Reopen my tabs on startup", variable=self.restore_var, command=self._on_restore_toggle)
        m.add_cascade(label="View", menu=v)
        h = tk.Menu(m, tearoff=False)
        h.add_command(label="Keyboard shortcuts", command=self.show_shortcuts)
        h.add_separator()
        h.add_command(label="About", command=self.about)
        m.add_cascade(label="Help", menu=h)
        self.config(menu=m)
        self.menu_tools = t
        self.menu_file = f
        self._menus = [m, f, t, tm, v, th, h] + self._menus  # + the Saved packs menu made above

    def _btn(self, parent, text, command, kind="default", **kw) -> RoundButton:
        b = RoundButton(parent, self.pal, text, command, kind=kind, font=self.f_bold if kind == "accent" else self.f_base,
                        **kw)
        self._rbuttons = [x for x in self._rbuttons if x.winfo_exists()]  # forget destroyed fix buttons
        self._rbuttons.append(b)
        return b

    def _card(self, parent, **kw) -> Card:
        c = Card(parent, self.pal, **kw)
        self._cards.append(c)
        return c

    def _page_frame(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=self.pal["page"], bd=0, highlightthickness=0)
        self._page_frames.append(f)
        return f

    def _build_header(self) -> None:
        card = self._card(self, fit=True, height=60)
        card.pack(fill="x", padx=12, pady=(2, 0))
        head = ttk.Frame(card.body)
        head.pack(fill="x")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="Modpack").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.src_entry = ttk.Entry(head, textvariable=self.src_var)
        self.src_entry.bind("<KeyRelease>", lambda e: self._refresh_buttons())  # folder buttons follow what is typed
        self.src_entry.grid(row=0, column=1, sticky="ew")
        self._btn(head, "Instance folder...", self.pick_folder).grid(row=0, column=2, padx=(8, 0))
        self._btn(head, "Pack file...", self.pick_file).grid(row=0, column=3, padx=(6, 0))
        self.saved_btn = self._btn(head, "Saved packs", None, arrow=True)
        saved_menu = self._make_saved_menu(self.saved_btn)
        self.saved_btn.configure(command=lambda: self._popup_below(saved_menu, self.saved_btn))
        self.saved_btn.grid(row=0, column=4, padx=(6, 0))
        self.analyze_btn = self._btn(head, "Analyze", self.analyze, kind="accent", padx=20)
        self.analyze_btn.grid(row=0, column=5, padx=(14, 0))
        self.autofix_btn = self._btn(head, "Auto-fix pack", self.auto_fix, kind="accent", padx=20)
        self.autofix_btn.grid(row=0, column=6, padx=(8, 0))

        opt = ttk.Frame(card.body)
        opt.pack(fill="x", pady=(12, 0))
        r0 = ttk.Frame(opt)
        r0.pack(fill="x")
        ttk.Label(r0, text="Minecraft").pack(side="left")
        ttk.Entry(r0, textvariable=self.mc_var, width=9).pack(side="left", padx=(6, 14))
        ttk.Label(r0, text="Loader").pack(side="left")
        self._combos.append(ttk.Combobox(r0, textvariable=self.loader_var, values=("auto",) + LOADERS, width=9,
                                         state="readonly"))
        self._combos[-1].pack(side="left", padx=(6, 14))
        ttk.Label(r0, text="Pack is for").pack(side="left")
        self._combos.append(ttk.Combobox(r0, textvariable=self.side_var, values=("auto", "client", "server", "both"),
                                         width=8, state="readonly"))
        self._combos[-1].pack(side="left", padx=(6, 14))
        ttk.Checkbutton(r0, text="Check Modrinth for updates (online)", variable=self.online_var).pack(side="left")
        theme_cb = ttk.Combobox(r0, textvariable=self.theme_var, values=vm.THEME_CHOICES, width=8, state="readonly")
        theme_cb.pack(side="right")
        theme_cb.bind("<<ComboboxSelected>>", lambda e: self._on_theme_change())
        self._combos.append(theme_cb)
        ttk.Label(r0, text="Theme").pack(side="right", padx=(0, 6))
        r1 = ttk.Frame(opt)
        r1.pack(fill="x", pady=(8, 0))
        ttk.Label(r1, text="CurseForge API key").pack(side="left")
        ttk.Entry(r1, textvariable=self.cf_var, show="\u2022", width=26).pack(side="left", padx=(6, 4))
        ttk.Label(r1, text="(only for CurseForge packs, not saved)", style="Muted.TLabel").pack(side="left", padx=(0, 14))
        ttk.Label(r1, text="Extra rules file").pack(side="left")
        ttk.Entry(r1, textvariable=self.rules_var).pack(side="left", fill="x", expand=True, padx=(6, 8))
        self._btn(r1, "Browse...", self.pick_rules, pady=5).pack(side="left")

    def _popup_below(self, menu: tk.Menu, widget) -> None:
        try:
            menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height() + 2)
        finally:
            menu.grab_release()

    # ------------------------------------------------------------------ body
    def _build_body(self) -> None:
        body = self._body = self._page_frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 0))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = self._card(body, width=300)
        left.grid(row=0, column=0, sticky="ns")
        left.grid_propagate(False)
        self._build_summary(left.body)
        main = self._card(body)
        main.grid(row=0, column=1, sticky="nsew")
        right = PillNotebook(main.body, self.pal, self.f_base)
        right.pack(fill="both", expand=True)
        self.notebook = right
        self._build_findings_tab(right)
        self._build_mods_tab(right)
        self._build_details_tab(right)
        self._build_crash_tab(right)

    def _build_summary(self, parent) -> None:
        box = parent
        self.gauge = tk.Canvas(box, width=150, height=150, highlightthickness=0, bg=self.bg)
        self.gauge.pack(pady=(2, 0))
        self.verdict_lbl = ttk.Label(box, text="", wraplength=230, justify="center", font=self.f_bold)
        self.verdict_lbl.pack(pady=(0, 6))
        counts = ttk.Frame(box)
        counts.pack()
        self.count_lbls = {}
        for sev in ("error", "warning", "info"):
            lbl = tk.Label(counts, text="-", fg=self.pal[sev], bg=self.bg, font=self.f_bold)
            lbl.pack(side="left", padx=7)
            self.count_lbls[sev] = lbl
        ttk.Separator(box).pack(fill="x", pady=8)
        self.facts_frame = ttk.Frame(box)
        self.facts_frame.pack(fill="x")
        ttk.Separator(box).pack(fill="x", pady=8)
        self.undo_btn = self._btn(box, "Undo last fix", self.undo_last)
        self.undo_btn.pack(fill="x")
        self.export_btn = self._btn(box, "Export report...", self.export)
        self.export_btn.pack(fill="x", pady=(6, 0))
        ttk.Label(box, text="OPEN FOLDER", style="Section.TLabel").pack(anchor="w", pady=(12, 4))
        row = ttk.Frame(box)
        row.pack(fill="x")
        row.columnconfigure((0, 1, 2), weight=1, uniform="fold")
        self.folder_btn = self._btn(row, "Pack", self.open_folder, padx=6)
        self.folder_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.mods_btn = self._btn(row, "Mods", lambda: self.open_pack_folder("mods"), padx=6)
        self.mods_btn.grid(row=0, column=1, sticky="ew", padx=3)
        self.config_btn = self._btn(row, "Config", lambda: self.open_pack_folder("config"), padx=6)
        self.config_btn.grid(row=0, column=2, sticky="ew", padx=(3, 0))
        self._draw_gauge(None)

    def _draw_gauge(self, score: Optional[int]) -> None:
        c = self.gauge
        c.delete("all")
        self._score_shown = score
        a, b = 14, 136
        c.create_oval(a, a, b, b, outline=self.pal["trough"], width=12)
        if score is None:
            c.create_text(75, 75, text="--", font=self.f_big, fill=self.pal["muted"])
            return
        color = vm.score_color(score)
        if score > 0:
            c.create_arc(a, a, b, b, start=90, extent=-359.9 * score / 100, style="arc", outline=color, width=12)
        c.create_text(75, 66, text=str(score), font=self.f_big, fill=color)
        c.create_text(75, 100, text="health score", fill=self.pal["muted"])

    def _build_findings_tab(self, nb) -> None:
        tab = ttk.Frame(nb, padding=(0, 8, 0, 0))
        nb.add(tab, text="Findings")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        self.sev_checks = {}
        for sev in ("error", "warning", "info"):
            cb = ttk.Checkbutton(bar, text=SEV_TITLES[sev], variable=self.sev_vars[sev], command=self._refresh_findings)
            cb.pack(side="left", padx=(0, 10))
            self.sev_checks[sev] = cb
        self.cat_combo = ttk.Combobox(bar, textvariable=self.cat_var, values=[vm.ALL_CATEGORIES], state="readonly", width=22)
        self._combos.append(self.cat_combo)
        self.cat_combo.pack(side="left", padx=(6, 10))
        self.cat_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_findings())
        ttk.Label(bar, text="Search").pack(side="left")
        se = ttk.Entry(bar, textvariable=self.search_var, width=22)
        se.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.search_var.trace_add("write", lambda *a: self._refresh_findings())
        self.shown_lbl = ttk.Label(bar, text="", style="Muted.TLabel")
        self.shown_lbl.pack(side="right", padx=(8, 0))

        holder = ttk.Frame(tab)
        holder.pack(fill="both", expand=True, pady=(8, 0))
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        pane = ttk.PanedWindow(holder, orient="vertical")
        pane.grid(row=0, column=0, sticky="nsew")
        # Shown on top of the pane until the first analysis finishes.
        self.welcome_box = tk.Text(holder, wrap="word", relief="flat", padx=16, pady=12, state="disabled", font=self.f_base,
                                   highlightthickness=1)
        self._themed_texts.append(self.welcome_box)
        self.welcome_box.grid(row=0, column=0, sticky="nsew")
        self.welcome_box.tag_configure("h1", font=self.f_h1, spacing3=4)
        self.welcome_box.tag_configure("h2", font=self.f_bold, spacing1=10)
        top = ttk.Frame(pane)
        pane.add(top, weight=1)
        cols = ("sev", "cat", "title")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="browse", height=6)
        for c, text, w, stretch in (("sev", "Severity", 80, False), ("cat", "Category", 190, False), ("title", "What was found", 600, True)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, minwidth=60, stretch=stretch)
        sb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        bottom = ttk.Frame(pane, padding=(0, 6, 0, 0))
        pane.add(bottom, weight=1)
        self.dev_frame = ttk.Frame(bottom)  # developer tools for the selected finding (below the fix buttons)
        self.dev_frame.pack(side="bottom", fill="x", pady=(6, 0))
        self.dev_frame.columnconfigure((0, 1, 2), weight=1, uniform="dev")
        self.dev_btns = []
        for col, (label, cmd) in enumerate((("Show jar in folder", self.reveal_jar),
                                            ("View metadata files...", self.view_metadata_for_finding),
                                            ("Copy debug info", self.copy_debug))):
            b = self._btn(self.dev_frame, label, cmd, padx=8, pady=6)
            b.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 3, 0 if col == 2 else 3))
            self.dev_btns.append(b)
        self._current_finding = None
        self.fix_frame = ttk.Frame(bottom)
        self.fix_frame.pack(side="bottom", fill="x", pady=(6, 0))
        tf = ttk.Frame(bottom)
        tf.pack(side="top", fill="both", expand=True)
        self.detail = tk.Text(tf, wrap="word", height=11, relief="flat", padx=10, pady=8, state="disabled",
                              font=self.f_base, highlightthickness=1)
        self._themed_texts.append(self.detail)
        dsb = ttk.Scrollbar(tf, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dsb.set)
        self.detail.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")
        self.detail.tag_configure("h1", font=self.f_h1, spacing3=2)
        self.detail.tag_configure("h2", font=self.f_bold, spacing1=8)
        self.detail.tag_configure("mono", font=self.f_mono)
        for sev in ("error", "warning", "info"):
            self.detail.tag_configure("sev_" + sev, font=self.f_bold)

    def _build_mods_tab(self, nb) -> None:
        tab = ttk.Frame(nb, padding=(0, 8, 0, 0))
        nb.add(tab, text="Mods")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Label(bar, text="Search").pack(side="left")
        ttk.Entry(bar, textvariable=self.mod_search_var, width=30).pack(side="left", padx=(6, 12))
        ttk.Checkbutton(bar, text="Only mods with findings", variable=self.mod_issue_var,
                        command=self._refresh_mods).pack(side="left")
        self.mod_search_var.trace_add("write", lambda *a: self._refresh_mods())
        self.mods_export_btn = self._btn(bar, "Export list...", self.export_mod_list, padx=10, pady=3)
        self.mods_export_btn.pack(side="right", padx=(8, 0))
        self.mods_txt_btn = self._btn(bar, "Save as .txt", self.export_mod_txt, padx=10, pady=3)
        self.mods_txt_btn.pack(side="right", padx=(8, 0))
        self.mods_lbl = ttk.Label(bar, text="", style="Muted.TLabel")
        self.mods_lbl.pack(side="right")
        cols = ("name", "mod_id", "version", "loader", "side", "file", "issue")
        heads = {"name": ("Name", 160), "mod_id": ("Mod id", 140), "version": ("Version", 90), "loader": ("Loader", 78),
                 "side": ("Side", 50), "file": ("File", 250), "issue": ("Issue", 70)}
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True, pady=(8, 0))
        self.mod_tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            text, w = heads[c]
            self.mod_tree.heading(c, text=text, command=lambda c=c: self._sort_mods(c))
            self.mod_tree.column(c, width=w, minwidth=(40 if c == "file" else 50), stretch=(c == "file"))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.mod_tree.yview)
        self.mod_tree.configure(yscrollcommand=sb.set)
        self.mod_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.mod_tree.bind("<Double-1>", self._on_mod_double_click)

    def _build_details_tab(self, nb) -> None:
        tab = ttk.Frame(nb, padding=(0, 8, 0, 0))
        nb.add(tab, text="Pack details")
        self.pack_text = tk.Text(tab, wrap="word", relief="flat", padx=10, pady=8, state="disabled", font=self.f_base,
                                 highlightthickness=1)
        self._themed_texts.append(self.pack_text)
        sb = ttk.Scrollbar(tab, orient="vertical", command=self.pack_text.yview)
        self.pack_text.configure(yscrollcommand=sb.set)
        self.pack_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _build_crash_tab(self, nb) -> None:
        tab = ttk.Frame(nb, padding=(0, 8, 0, 0))
        nb.add(tab, text="Crash log")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        self.crash_btn = self._btn(bar, "Analyze latest crash", self.analyze_crash, kind="accent", padx=12, pady=5)
        self.crash_btn.pack(side="left")
        self._btn(bar, "Open a crash file...", self.open_crash_file, padx=12, pady=5).pack(side="left", padx=(8, 0))
        self.paste_btn = self._btn(bar, "Paste text or link...", self.paste_crash, padx=12, pady=5)
        self.paste_btn.pack(side="left", padx=(8, 0))
        self.crash_fix_frame = ttk.Frame(tab)
        self.crash_fix_frame.pack(side="bottom", fill="x", pady=(6, 0))
        tf = ttk.Frame(tab)
        tf.pack(side="top", fill="both", expand=True, pady=(8, 0))
        self.crash_text = tk.Text(tf, wrap="word", relief="flat", padx=10, pady=8, state="disabled", font=self.f_base,
                                  highlightthickness=1)
        self._themed_texts.append(self.crash_text)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.crash_text.yview)
        self.crash_text.configure(yscrollcommand=sb.set)
        self.crash_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.crash_text.tag_configure("h1", font=self.f_h1, spacing3=2)
        self.crash_text.tag_configure("h2", font=self.f_bold, spacing1=8)
        self.crash_text.tag_configure("mono", font=self.f_mono)
        self._render_crash()

    def _build_status(self) -> None:
        bar = self._page_frame(self)
        bar.pack(fill="x", side="bottom", before=self._body, padx=16, pady=(4, 8))  # never squeezed out
        self._status_lbl = tk.Label(bar, textvariable=self.status_var, bg=self.pal["page"], fg=self.pal["muted"],
                                    anchor="w", font=self.f_base)
        self._status_lbl.pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bar, mode="determinate", length=200, maximum=100)
        self.progress.pack(side="right")

    def _bind_keys(self) -> None:
        self.bind("<F5>", lambda e: self.analyze())
        self.bind("<Control-o>", lambda e: self.pick_folder())
        self.bind("<Control-O>", lambda e: self.pick_file())
        self.bind("<Control-s>", lambda e: self.export())
        self.bind("<Control-m>", lambda e: self.open_pack_folder("mods"))
        self.bind("<Control-l>", lambda e: self.analyze_crash())
        self.bind("<Control-C>", lambda e: self.open_pack_folder("config"))
        self.src_entry.bind("<Return>", lambda e: self.analyze())
        for seq, fn in (("<Control-t>", self.new_tab), ("<Control-w>", self.close_tab), ("<Control-T>", self.reopen_closed_tab),
                        ("<Control-d>", self.save_pack), ("<Control-Tab>", lambda: self.cycle_tab(1)),
                        ("<Control-Shift-Tab>", lambda: self.cycle_tab(-1)),
                        ("<Control-ISO_Left_Tab>", lambda: self.cycle_tab(-1))):
            try:
                self.bind(seq, lambda e, fn=fn: (fn(), "break")[1])
            except tk.TclError:  # key name unknown on this platform
                pass
        for i in range(1, 10):
            self.bind(f"<Control-Key-{i}>", lambda e, i=i: (self.jump_to_tab(i - 1), "break")[1])

    # ------------------------------------------------------------------ text helpers
    def _set_text(self, widget: tk.Text, parts: list) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for text, *tags in parts:
            widget.insert("end", text, tuple(tags))
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _show_welcome(self) -> None:
        title, _, rest = WELCOME.partition("\n")
        parts = [(title + "\n", "h1")]
        for block in rest.strip("\n").split("\n\n"):
            head, _, body = block.partition("\n")
            if head in ("How to use it", "Safety") and body:
                parts += [(head + "\n", "h2"), (body + "\n\n",)]
            else:
                parts.append((block + "\n\n",))
        self._set_text(self.welcome_box, parts)
        self.welcome_box.grid()

    # ------------------------------------------------------------------ tabs
    def _build_tabbar(self) -> None:
        self.tabbar = self._page_frame(self)
        self.tabbar.pack(fill="x", padx=14, pady=(10, 6))

    def _is_saved(self, tab: Tab) -> bool:
        key = vm.norm_source(tab.source)
        return bool(key) and any(vm.norm_source(p.spec.source) == key for p in self.library.saved)

    def _render_tabbar(self) -> None:
        if not hasattr(self, "tabbar"):
            return
        p = self.pal
        self.tabbar.configure(bg=p["page"])
        for w in self.tabbar.winfo_children():
            w.destroy()
        for tab in self.tabs:
            text = ("\u2605 " if self._is_saved(tab) else "") + vm.shorten(tab.title(), 24) \
                + ("  \u2022" if tab.attention else "")
            pill = TabPill(self.tabbar, p, text, active=tab is self.tab,
                           font=self.f_bold if tab is self.tab else self.f_base,
                           on_select=lambda t=tab: self.switch_to(t), on_close=lambda t=tab: self.close_tab(t),
                           on_menu=(RIGHT_CLICK, lambda e, t=tab: self._tab_context_menu(t, e)),
                           on_middle=(MIDDLE_CLICK, lambda e, t=tab: self.close_tab(t)))
            pill.pack(side="left", padx=(0, 4))
        plus = RoundButton(self.tabbar, p, "+", self.new_tab, kind="ghost", font=self.f_h1, padx=12, pady=2, radius=16,
                           on_page=True)
        plus.pack(side="left")

    def _tab_context_menu(self, tab: Tab, event) -> None:
        m = tk.Menu(self, tearoff=False)
        self._style_menu(m)
        m.add_command(label="Rename...", command=lambda: self.rename_tab(tab))
        m.add_command(label="Duplicate", command=lambda: self.duplicate_tab(tab))
        m.add_command(label="Save pack...", command=lambda: self.save_pack(tab))
        folders = vm.pack_folders(tab.result, tab.source)
        for key, label in (("pack", "Open pack folder"), ("mods", "Open mods folder"),
                           ("config", "Open config folder")):
            m.add_command(label=label, state="normal" if key in folders else "disabled",
                          command=lambda k=key: self.open_pack_folder(k, tab))
        m.add_separator()
        m.add_command(label="Close", command=lambda: self.close_tab(tab))
        m.add_command(label="Close other tabs", state="normal" if len(self.tabs) > 1 else "disabled",
                      command=lambda: self.close_other_tabs(tab))
        m.add_command(label="Reopen closed tab", state="normal" if self._closed else "disabled",
                      command=self.reopen_closed_tab)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _fill_tabs_menu(self, menu: tk.Menu) -> None:
        menu.delete(0, "end")
        menu.add_command(label="New tab", accelerator="Ctrl+T", command=self.new_tab)
        menu.add_command(label="Duplicate tab", command=self.duplicate_tab)
        menu.add_command(label="Rename tab...", command=self.rename_tab)
        menu.add_command(label="Close tab", accelerator="Ctrl+W", command=self.close_tab)
        menu.add_command(label="Close other tabs", state="normal" if len(self.tabs) > 1 else "disabled",
                         command=lambda: self.close_other_tabs(self.tab))
        menu.add_command(label="Reopen closed tab", accelerator="Ctrl+Shift+T", command=self.reopen_closed_tab,
                         state="normal" if self._closed else "disabled")
        menu.add_separator()
        menu.add_command(label="Next tab", accelerator="Ctrl+Tab", command=lambda: self.cycle_tab(1))
        menu.add_command(label="Previous tab", accelerator="Ctrl+Shift+Tab", command=lambda: self.cycle_tab(-1))
        menu.add_command(label="Move tab left", command=lambda: self.move_tab(-1))
        menu.add_command(label="Move tab right", command=lambda: self.move_tab(1))
        menu.add_separator()
        for i, t in enumerate(self.tabs):
            mark = "✓ " if t is self.tab else "     "
            menu.add_command(label=f"{mark}{i + 1}. {vm.shorten(t.title(), 40)}", command=lambda t=t: self.switch_to(t))

    def _who(self, tab: Tab) -> str:
        """Prefix naming the tab when a message is about a tab other than the one on screen."""
        return "" if tab is self.tab else f"[{vm.shorten(tab.title(), 30)}]\n"

    def _update_window_title(self) -> None:
        self.title(f"{self.tab.title()} - Modpack Doctor")

    def _after_tab_change(self, save: bool = True) -> None:
        self._render_tabbar()
        self._update_window_title()
        self._refresh_buttons()
        if save:
            self._save_session()

    def _capture_active(self) -> None:
        """Copy what the widgets show into the active Tab so it can be put back later."""
        t = self.tab
        t.source, t.mc = self.src_var.get(), self.mc_var.get()
        t.loader, t.side, t.online = self.loader_var.get(), self.side_var.get(), bool(self.online_var.get())
        try:
            inner = self.notebook.index(self.notebook.select())
        except tk.TclError:
            inner = 0
        t.view = {"sev": {k: v.get() for k, v in self.sev_vars.items()}, "cat": self.cat_var.get(),
                  "search": self.search_var.get(), "mod_search": self.mod_search_var.get(),
                  "mod_issue": bool(self.mod_issue_var.get()), "inner": inner, "mod_sort": list(self._mod_sort)}

    def _restore_active(self) -> None:
        """Show the active Tab: its options, its filters and its results (or the empty state)."""
        t = self.tab
        v = t.view or {}
        self._loading_tab = True  # the filter variables trigger refreshes; do one clean refresh afterwards instead
        try:
            self.src_var.set(t.source)
            self.mc_var.set(t.mc)
            self.loader_var.set(t.loader)
            self.side_var.set(t.side)
            self.online_var.set(t.online)
            for k, var in self.sev_vars.items():
                var.set(bool(v.get("sev", {}).get(k, True)))
            self.cat_var.set(v.get("cat", vm.ALL_CATEGORIES))
            self.search_var.set(v.get("search", ""))
            self.mod_search_var.set(v.get("mod_search", ""))
            self.mod_issue_var.set(bool(v.get("mod_issue", False)))
        finally:
            self._loading_tab = False
        ms = v.get("mod_sort", ("issue", False))
        self._mod_sort = (ms[0], bool(ms[1])) if isinstance(ms, (list, tuple)) and len(ms) == 2 else ("issue", False)
        if t.result is not None:
            self._refresh_all()
        else:
            self._show_empty()
            if not self.busy:
                self.status_var.set("Ready." if not t.source.strip() else "Not scanned yet. Press Analyze (F5).")
        self._render_crash()
        try:
            self.notebook.select(int(v.get("inner", 0)))
        except (tk.TclError, ValueError, TypeError):
            pass
        t.attention = False

    def _show_empty(self) -> None:
        """The views for a tab that has not been scanned (yet)."""
        tab = self.tab
        self._draw_gauge(None)
        self.verdict_lbl.configure(text="")
        for lbl in self.count_lbls.values():
            lbl.configure(text="-")
        for sev, cb in self.sev_checks.items():
            cb.configure(text=SEV_TITLES[sev])
        for w in self.facts_frame.winfo_children():
            w.destroy()
        self.tree.delete(*self.tree.get_children())
        self.mod_tree.delete(*self.mod_tree.get_children())
        self._finding_by_iid.clear()
        self._mod_rows = []
        self.cat_combo.configure(values=[vm.ALL_CATEGORIES])
        self.cat_var.set(vm.ALL_CATEGORIES)
        self.shown_lbl.configure(text="")
        self.mods_lbl.configure(text="")
        self._set_text(self.detail, [("",)])
        self._set_text(self.pack_text, [("",)])
        self._clear_fix_buttons()
        if tab.source.strip():
            self._set_text(self.welcome_box, [
                (tab.title() + "\n", "h1"), ("This pack has not been scanned in this session.\n\n",),
                (f"Source: {tab.source.strip()}\n\n",),
                ("Press Analyze (F5) to check it, or Auto-fix pack to check it and apply the safe fixes in one go.\n",)])
            self.welcome_box.grid()
        else:
            self._show_welcome()

    def switch_to(self, tab: Tab) -> None:
        if tab is self.tab or tab not in self.tabs:
            return
        self._capture_active()
        self.tab = tab
        self._restore_active()
        self._after_tab_change()

    def new_tab(self, spec: Optional[vm.TabSpec] = None, activate: bool = True) -> Optional[Tab]:
        if len(self.tabs) >= vm.MAX_TABS:
            messagebox.showinfo("Modpack Doctor", f"You can have up to {vm.MAX_TABS} tabs open. Close one first.")
            return None
        tab = Tab(spec)
        self.tabs.append(tab)
        if activate:
            self.switch_to(tab)
        else:
            self._after_tab_change()
        return tab

    def close_tab(self, tab: Optional[Tab] = None) -> None:
        tab = tab or self.tab
        if tab not in self.tabs:
            return
        if self.busy and self._busy_tab is tab:
            messagebox.showinfo("Modpack Doctor", "This tab is busy. Close it when the current task has finished.")
            return
        if tab is self.tab:
            self._capture_active()
        spec = tab.spec()
        if spec.source.strip():
            self._closed.append(spec)
            del self._closed[:-vm.MAX_CLOSED]
        idx = self.tabs.index(tab)
        self.tabs.remove(tab)
        if not self.tabs:
            self.tabs.append(Tab())
        if tab is self.tab:
            self.tab = self.tabs[min(idx, len(self.tabs) - 1)]
            self._restore_active()
        self._after_tab_change()

    def close_other_tabs(self, tab: Optional[Tab] = None) -> None:
        keep = tab or self.tab
        for t in list(self.tabs):
            if t is not keep and not (self.busy and self._busy_tab is t):
                self.close_tab(t)
        if keep in self.tabs:
            self.switch_to(keep)

    def reopen_closed_tab(self) -> None:
        if not self._closed:
            return
        spec = self._closed.pop()
        self._open_and_scan(spec.source, spec)

    def duplicate_tab(self, tab: Optional[Tab] = None) -> None:
        tab = tab or self.tab
        if tab is self.tab:
            self._capture_active()
        self.new_tab(tab.spec())

    def rename_tab(self, tab: Optional[Tab] = None) -> None:
        tab = tab or self.tab
        name = simpledialog.askstring("Rename tab", "Name for this tab (leave empty to use the pack's own name):",
                                      initialvalue=tab.title(), parent=self)
        if name is None:
            return
        tab.custom_title = name.strip()[:60]
        self._after_tab_change()

    def move_tab(self, delta: int) -> None:
        i = self.tabs.index(self.tab)
        j = max(0, min(len(self.tabs) - 1, i + delta))
        if i != j:
            self.tabs[i], self.tabs[j] = self.tabs[j], self.tabs[i]
            self._after_tab_change()

    def cycle_tab(self, delta: int) -> None:
        if len(self.tabs) > 1:
            self.switch_to(self.tabs[(self.tabs.index(self.tab) + delta) % len(self.tabs)])

    def jump_to_tab(self, index: int) -> None:
        if 0 <= index < len(self.tabs):
            self.switch_to(self.tabs[index])

    def open_in_tab(self, source: str, spec: Optional[vm.TabSpec] = None) -> Optional[Tab]:
        """Show a pack in a tab: the tab that already has it, else the blank tab, else a new one."""
        copy = vm.TabSpec.from_dict(spec.to_dict()) if spec else None
        spec = copy or vm.TabSpec()
        spec.source = (source or "").strip().strip('"')
        self._capture_active()
        key = vm.norm_source(spec.source)
        if key:
            for t in self.tabs:
                if vm.norm_source(t.source) == key:
                    self.switch_to(t)
                    return t
        cur = self.tab
        if not cur.source.strip() and cur.result is None:  # an empty tab is reused rather than left behind
            cur.source, cur.custom_title, cur.mc = spec.source, spec.title, spec.mc
            cur.loader, cur.side, cur.online = spec.loader, spec.side, spec.online
            self._restore_active()
            self._after_tab_change()
            return cur
        return self.new_tab(spec)

    @staticmethod
    def _is_local_folder(source: str) -> bool:
        try:
            return bool(source.strip()) and Path(source.strip().strip('"')).expanduser().is_dir()
        except OSError:
            return False

    def _open_and_scan(self, source: str, spec: Optional[vm.TabSpec] = None) -> None:
        """Open a pack in a tab and, if it is a local folder (fast, no downloads), scan it straight away."""
        tab = self.open_in_tab(source, spec)
        if tab is not None and tab is self.tab and tab.result is None and self._is_local_folder(source):
            self.analyze()

    def _init_tabs(self, initial, restore: bool, autoscan: bool) -> None:
        specs, active = ([], 0)
        if restore and self.restore_var.get():
            specs, active = vm.load_session()
        wanted = [initial] if isinstance(initial, str) else list(initial or [])
        for src in wanted:
            src = str(src).strip().strip('"')
            if not src:
                continue
            key = vm.norm_source(src)
            idx = next((i for i, sp in enumerate(specs) if vm.norm_source(sp.source) == key), None)
            if idx is None:
                if len(specs) >= vm.MAX_TABS:
                    continue
                specs.append(vm.TabSpec(source=src))
                idx = len(specs) - 1
            active = idx
        self.tabs = [Tab(sp) for sp in (specs or [vm.TabSpec()])]
        self.tab = self.tabs[active]
        self._restore_active()
        self._after_tab_change(save=False)
        if autoscan and self._is_local_folder(self.tab.source):
            self.after(300, self.analyze)

    def _save_session(self) -> None:
        if not self.restore_var.get():
            return
        self._capture_active()
        vm.save_session([t.spec() for t in self.tabs], self.tabs.index(self.tab))

    def _on_restore_toggle(self) -> None:
        self._save_settings()
        if self.restore_var.get():
            self._save_session()
        else:
            try:
                vm.session_path().unlink()
            except OSError:
                pass
        self.status_var.set("Your tabs will be reopened next time." if self.restore_var.get()
                            else "Tabs will not be reopened next time.")

    # ------------------------------------------------------------------ saved packs
    def _make_saved_menu(self, parent) -> tk.Menu:
        menu = tk.Menu(parent, tearoff=False)
        menu.configure(postcommand=lambda: self._fill_saved_menu(menu))
        self._menus.append(menu)
        self._style_menu(menu)
        return menu

    @staticmethod
    def _exists(source: str) -> bool:
        try:
            return Path(source.strip().strip('"')).expanduser().exists()
        except OSError:
            return False

    def _fill_saved_menu(self, menu: tk.Menu) -> None:
        menu.delete(0, "end")
        menu.add_command(label="Save this pack...", accelerator="Ctrl+D", command=self.save_pack)
        menu.add_separator()
        if self.library.saved:
            for pack in self.library.saved:
                gone = "" if self._exists(pack.spec.source) else "   (not found)"
                menu.add_command(label=vm.shorten(pack.name, 48) + gone, command=lambda p=pack: self.open_saved(p))
        else:
            menu.add_command(label="(no saved packs yet)", state="disabled")
        menu.add_separator()
        rec = tk.Menu(menu, tearoff=False)
        self._style_menu(rec)
        if self.library.recent:
            for src in self.library.recent:
                rec.add_command(label=vm.shorten(f"{vm.default_title(src)}   -   {src}", 70),
                                command=lambda s=src: self.open_recent(s))
            rec.add_separator()
            rec.add_command(label="Clear recent list", command=self.clear_recent)
        else:
            rec.add_command(label="(nothing yet)", state="disabled")
        menu.add_cascade(label="Recent", menu=rec)
        menu.add_separator()
        menu.add_command(label="Manage saved packs...", command=self.manage_saved)

    def save_pack(self, tab: Optional[Tab] = None) -> None:
        """Remember a pack (and the options it was opened with) under a name you choose."""
        tab = tab or self.tab
        if tab is self.tab:
            self._capture_active()
        if not tab.source.strip():
            messagebox.showinfo("Save pack", "Choose a folder or pack file first, then save it.")
            return
        name = simpledialog.askstring("Save pack", "Name for this saved pack:", initialvalue=tab.title(), parent=self)
        if not name or not name.strip():
            return
        saved = vm.add_saved(self.library, name, tab.spec())
        vm.save_library(self.library)
        self._render_tabbar()
        self.status_var.set(f"Saved \"{saved.name}\". Find it under Saved packs.")

    def open_saved(self, pack: vm.SavedPack) -> None:
        spec = vm.TabSpec.from_dict(pack.spec.to_dict()) or vm.TabSpec(source=pack.spec.source)
        spec.title = pack.name
        self._open_and_scan(spec.source, spec)

    def open_recent(self, source: str) -> None:
        self._open_and_scan(source)

    def clear_recent(self) -> None:
        self.library.recent.clear()
        vm.save_library(self.library)

    def rename_saved_pack(self, pack: vm.SavedPack, parent=None) -> bool:
        new = simpledialog.askstring("Rename saved pack", "New name:", initialvalue=pack.name, parent=parent or self)
        if new is None:
            return False
        if not vm.rename_saved(self.library, pack, new):
            messagebox.showinfo("Modpack Doctor", "That name is empty or already used by another saved pack.",
                                parent=parent or self)
            return False
        vm.save_library(self.library)
        self._render_tabbar()
        return True

    def remove_saved_pack(self, pack: vm.SavedPack, parent=None) -> bool:
        if not messagebox.askyesno("Remove saved pack", f"Remove \"{pack.name}\" from your saved packs?\n\n"
                                   "The pack itself is not touched.", parent=parent or self):
            return False
        vm.remove_saved(self.library, pack)
        vm.save_library(self.library)
        self._render_tabbar()
        return True

    def manage_saved(self) -> None:
        if self._saved_win is not None and self._saved_win.winfo_exists():
            self._saved_win.lift()
            return
        p = self.pal
        win = tk.Toplevel(self)
        self._saved_win = win
        win.title("Saved packs")
        win.geometry("840x380")
        win.minsize(560, 260)
        win.transient(self)
        win.configure(background=p["bg"])
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(10, 0))
        box = ttk.Frame(frame)
        box.pack(side="top", fill="both", expand=True)
        tree = ttk.Treeview(box, columns=("name", "source", "status"), show="headings", selectmode="browse")
        for col, text, w, stretch in (("name", "Name", 190, False), ("source", "Folder or file", 470, True),
                                      ("status", "Status", 80, False)):
            tree.heading(col, text=text)
            tree.column(col, width=w, minwidth=60, stretch=stretch)
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        win.tree = tree  # type: ignore[attr-defined]

        def refresh() -> None:
            tree.delete(*tree.get_children())
            for i, pk in enumerate(self.library.saved):
                tree.insert("", "end", iid=str(i), values=(pk.name, pk.spec.source,
                                                           "Found" if self._exists(pk.spec.source) else "Not found"))
            if not self.library.saved:
                tree.insert("", "end", iid="none", values=("(nothing saved yet)", "Use Saved packs > Save this pack...", ""))

        def current() -> Optional[vm.SavedPack]:
            sel = tree.selection()
            return self.library.saved[int(sel[0])] if sel and sel[0].isdigit() else None

        def do_open() -> None:
            pk = current()
            if pk is not None:
                win.destroy()
                self.open_saved(pk)

        def do_rename() -> None:
            pk = current()
            if pk is not None and self.rename_saved_pack(pk, parent=win):
                refresh()

        def do_remove() -> None:
            pk = current()
            if pk is not None and self.remove_saved_pack(pk, parent=win):
                refresh()

        ttk.Button(buttons, text="Open", style="Accent.TButton", command=do_open).pack(side="left")
        ttk.Button(buttons, text="Rename...", command=do_rename).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Remove", command=do_remove).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")
        tree.bind("<Double-1>", lambda e: do_open())
        win.bind("<Escape>", lambda e: win.destroy())
        win.refresh = refresh  # type: ignore[attr-defined]
        win.open_selected, win.rename_selected, win.remove_selected = do_open, do_rename, do_remove  # type: ignore[attr-defined]
        refresh()

    # ------------------------------------------------------------------ choosing sources
    def pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Choose the instance folder (the one that contains 'mods')",
                                    initialdir=self.settings.get("last_dir") or str(Path.home()))
        if d:
            self.settings["last_dir"] = d
            self.open_in_tab(d)
            self.analyze()

    def pick_file(self) -> None:
        f = filedialog.askopenfilename(
            title="Choose a modpack file", initialdir=self.settings.get("last_dir") or str(Path.home()),
            filetypes=[("Modpack files", "*.mrpack *.zip"), ("Modrinth pack", "*.mrpack"), ("CurseForge pack", "*.zip"),
                       ("All files", "*.*")])
        if f:
            self.settings["last_dir"] = str(Path(f).parent)
            self.open_in_tab(f)
            self.analyze()

    def pick_rules(self) -> None:
        f = filedialog.askopenfilename(title="Choose a rules file", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if f:
            self.rules_var.set(f)

    # ------------------------------------------------------------------ background work
    def _set_busy(self, busy: bool, message: Optional[str] = None) -> None:
        self.busy = busy
        self.config(cursor="watch" if busy else "")
        if message:
            self.status_var.set(message)
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
        self._refresh_buttons()

    def _start(self, work: Callable, on_done: Callable, message: str, tab: Optional[Tab] = None) -> None:
        if self.busy:
            return
        task_tab = tab or self.tab  # the result belongs to this tab even if the user switches away meanwhile
        self._busy_tab = task_tab
        self._set_busy(True, message)

        def progress(msg, frac=None):
            self._queue.put(("progress", msg, frac))

        def run():
            try:
                self._queue.put(("done", on_done, work(progress), None, task_tab))
            except Exception as e:  # noqa: BLE001 - reported to the user on the main thread
                self._queue.put(("done", on_done, None, (e, traceback.format_exc()), task_tab))

        threading.Thread(target=run, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                if item[0] == "progress":
                    _, msg, frac = item
                    self.status_var.set(msg)
                    if frac is not None:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=max(0.0, min(1.0, frac)) * 100)
                    elif self.busy and str(self.progress.cget("mode")) != "indeterminate":
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(12)
                else:
                    _, on_done, res, err, task_tab = item
                    self._set_busy(False)
                    self._busy_tab = None
                    self._task_tab = task_tab
                    try:
                        on_done(res, err)
                    finally:
                        self._task_tab = None
        except queue.Empty:
            pass
        self._poll_id = self.after(80, self._poll)

    def _report_error(self, err, what: str) -> None:
        exc, tb = err
        if isinstance(exc, (SourceError, fixer.FixError, net.NetError)):
            messagebox.showerror("Modpack Doctor", str(exc))
        else:
            print(tb, file=sys.stderr)
            messagebox.showerror("Modpack Doctor", f"Something unexpected went wrong while {what}:\n\n"
                                 f"{type(exc).__name__}: {exc}\n\nDetails were printed to the console.")
        self.status_var.set(f"Failed: {exc}")

    # ------------------------------------------------------------------ analysis
    def _build_options(self) -> Options:
        loader = self.loader_var.get()
        side = self.side_var.get()
        return Options(mc=self.mc_var.get().strip() or None, loader=None if loader == "auto" else loader,
                       side=None if side == "auto" else side, cf_key=self.cf_var.get().strip() or None)

    def _save_settings(self) -> None:
        for old in ("source", "mc", "loader", "side", "online"):  # per-tab now (older versions stored them here)
            self.settings.pop(old, None)
        self.settings.update(rules=self.rules_var.get().strip(), theme=self.theme_var.get(),
                             restore_tabs=bool(self.restore_var.get()))
        vm.save_settings(self.settings)

    def analyze(self, then_autofix: bool = False) -> None:
        if self.busy:
            return
        source = self.src_var.get().strip().strip('"')
        if not source:
            messagebox.showinfo("Modpack Doctor", "Choose an instance folder or a pack file first.")
            return
        rules_path = self.rules_var.get().strip() or None
        if rules_path and not Path(rules_path).is_file():
            messagebox.showerror("Modpack Doctor", f"The rules file was not found:\n{rules_path}")
            return
        self._save_settings()
        self._capture_active()
        tab = self.tab
        self._save_session()
        opts, online = self._build_options(), self.online_var.get()
        self._start(lambda prog: pipeline.run_analysis(source, opts, online=online, rules_path=rules_path, progress=prog),
                    lambda res, err: self._on_analyzed(res, err, then_autofix), "Analysing...", tab=tab)

    def _reanalyze(self, tab: Optional[Tab] = None) -> None:
        tab = tab or self._task_tab or self.tab
        prev, online = tab.result, self.online_var.get()
        rules_path = self.rules_var.get().strip() or None
        self._start(lambda prog: pipeline.reanalyze(prev, online=online, rules_path=rules_path, progress=prog),
                    self._on_analyzed, "Re-checking the pack...", tab=tab)

    def _on_analyzed(self, res, err, then_autofix: bool = False) -> None:
        tab = self._task_tab or self.tab
        if err:
            self._report_error(err, "analysing the pack")
            return
        tab.result = res
        if tab.source.strip():
            vm.add_recent(self.library, tab.source)
            vm.save_library(self.library)
        try:
            rp = self.rules_var.get().strip() or None
            self.rules = load_rules(rp) if rp else self.base_rules
        except Exception:  # noqa: BLE001
            self.rules = self.base_rules
        c = res.counts()
        if tab is self.tab:
            self._refresh_all()
            self.status_var.set(f"Done: {res.pack.mod_count} mods checked - {c['error']} error(s), "
                                f"{c['warning']} warning(s), {c['info']} tip(s).")
        else:  # finished in the background: leave what the user is looking at alone, mark the tab instead
            tab.attention = True
            self.status_var.set(f"Finished checking \"{tab.title()}\": {c['error']} error(s), {c['warning']} warning(s).")
        self._render_tabbar()
        self._update_window_title()
        self._save_session()
        if then_autofix:
            self.after(50, lambda t=tab: self.auto_fix(t))

    # ------------------------------------------------------------------ refreshing the views
    def _refresh_all(self) -> None:
        res = self.result
        self.welcome_box.grid_remove()
        self._draw_gauge(res.score)
        self.verdict_lbl.configure(text=res.verdict)
        c = res.counts()
        self.count_lbls["error"].configure(text=f"{c['error']} errors")
        self.count_lbls["warning"].configure(text=f"{c['warning']} warnings")
        self.count_lbls["info"].configure(text=f"{c['info']} tips")
        for sev in self.sev_checks:
            self.sev_checks[sev].configure(text=f"{SEV_TITLES[sev]} ({c[sev]})")
        for w in self.facts_frame.winfo_children():
            w.destroy()
        for i, (k, v) in enumerate(vm.pack_facts(res, self.rules)):
            ttk.Label(self.facts_frame, text=k, style="Muted.TLabel").grid(row=i, column=0, sticky="nw", pady=1)
            ttk.Label(self.facts_frame, text=v, wraplength=150, justify="left").grid(row=i, column=1, sticky="nw",
                                                                                      padx=(10, 0), pady=1)
        cats = vm.categories(res.findings)
        self.cat_combo.configure(values=cats)
        if self.cat_var.get() not in cats:
            self.cat_var.set(vm.ALL_CATEGORIES)
        self._refresh_findings()
        self._mod_rows = vm.mod_rows(res)
        self._refresh_mods()
        self._set_text(self.pack_text, [(vm.details_text(res),)])
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        res = self.result
        idle = not self.busy
        has = res is not None
        can_fix = has and idle and res.pack.root is not None
        n = len(vm.recommended_fixes(res)) if has else 0
        self.analyze_btn.state(["!disabled"] if idle else ["disabled"])
        self.autofix_btn.configure(text=f"Auto-fix pack ({n})" if (has and n) else "Auto-fix pack")
        self.autofix_btn.state(["!disabled"] if idle else ["disabled"])
        has_undo = bool(can_fix and self._undoable())
        self.undo_btn.state(["!disabled"] if has_undo else ["disabled"])
        self.export_btn.state(["!disabled"] if (has and idle) else ["disabled"])
        self.folder_btn.state(["!disabled"] if (has and res.pack.root is not None) else ["disabled"])
        self._set_menu(self.menu_tools, "Auto-fix pack", idle)
        self._set_menu(self.menu_tools, "Undo last fix", has_undo)
        self._set_menu(self.menu_tools, "Open analysed folder", has)
        folders = vm.pack_folders(res, self.src_var.get())
        self.mods_btn.state(["!disabled"] if "mods" in folders else ["disabled"])
        self.config_btn.state(["!disabled"] if "config" in folders else ["disabled"])
        self._set_menu(self.menu_tools, "Open mods folder", "mods" in folders)
        self._set_menu(self.menu_tools, "Open config folder", "config" in folders)
        self._set_menu(self.menu_file, "Export report...", has and idle)
        self._set_menu(self.menu_file, "Copy report as Markdown", has)
        self._set_menu(self.menu_file, "Export mod list...", has)
        self.mods_export_btn.state(["!disabled"] if has else ["disabled"])
        self.mods_txt_btn.state(["!disabled"] if has else ["disabled"])
        self._set_menu(self.menu_file, "Save mod list as .txt...", has)
        can_root = bool(folders.get("pack"))
        self._set_menu(self.menu_tools, "Analyze latest crash report", can_root)
        self._set_menu(self.menu_tools, "Manage disabled mods...", bool(has and can_fix))
        self.crash_btn.state(["!disabled"] if can_root else ["disabled"])
        for b in self.fix_frame.winfo_children():
            b.state(["!disabled"] if can_fix else ["disabled"])

    @staticmethod
    def _set_menu(menu: tk.Menu, label: str, enabled: bool) -> None:
        try:
            menu.entryconfigure(label, state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def _undoable(self) -> bool:
        try:
            return any(not c.get("undone") and c.get("entries") for c in fixer.list_changesets(self.result.pack.root))
        except Exception:  # noqa: BLE001
            return False

    def _refresh_findings(self) -> None:
        if self._loading_tab:
            return
        self.tree.delete(*self.tree.get_children())
        self._finding_by_iid.clear()
        if self.result is None:
            return
        sev = [s for s, v in self.sev_vars.items() if v.get()]
        shown = vm.filter_findings(self.result.findings, sev, self.cat_var.get(), self.search_var.get())
        for i, f in enumerate(shown):
            iid = f"f{i}"
            self._finding_by_iid[iid] = f
            self.tree.insert("", "end", iid=iid, values=(vm.SEVERITY_LABEL[f.severity], f.category, f.title), tags=(f.severity,))
        total = len(self.result.findings)
        self.shown_lbl.configure(text=f"{len(shown)} of {total}")
        if shown:
            self.tree.selection_set("f0")
            self.tree.focus("f0")
        elif total == 0:
            self._set_text(self.detail, [("No problems found", "h1"), ("\n\nNothing in this pack looks wrong to Modpack Doctor. "
                                         "That is not a guarantee that it will launch (it cannot detect every crash), "
                                         "but no known compatibility problems, missing dependencies or broken configs were found.",)])
            self._clear_fix_buttons()
        else:
            self._set_text(self.detail, [("No findings match the current filters.", "muted")])
            self._clear_fix_buttons()

    def _on_select(self) -> None:
        sel = self.tree.selection()
        f = self._finding_by_iid.get(sel[0]) if sel else None
        if f is not None:
            self._show_finding(f)

    def _clear_fix_buttons(self) -> None:
        for w in self.fix_frame.winfo_children():
            w.destroy()
        self._current_finding = None
        for b in getattr(self, "dev_btns", []):
            b.state(["disabled"])

    def _show_finding(self, f) -> None:
        parts = [(f.title + "\n", "h1"), (f"{vm.SEVERITY_LABEL[f.severity]}", "sev_" + f.severity),
                 (f"  -  {f.category}\n\n", "muted")]
        if f.detail:
            parts.append((f.detail + "\n",))
        if f.files:
            parts.append(("\nAffected files\n", "h2"))
            parts.append(("\n".join("  " + x for x in f.files[:30]) + (f"\n  ... and {len(f.files) - 30} more" if len(f.files) > 30 else "") + "\n", "mono"))
        if f.trace:
            parts.append(("\nWhere this comes from (for developers)\n", "h2"))
            parts.append(("\n".join("  " + t for t in f.trace[:40]) + "\n", "mono"))
        if f.fixes:
            parts.append(("\nWhat you can do\n", "h2"))
            parts.append(("Use the buttons below. Mods are disabled (renamed), never deleted, and every change can be undone.\n", "muted"))
        else:
            parts.append(("\nThis one needs a manual decision; there is no one-click fix.\n", "muted"))
        self._set_text(self.detail, parts)
        self._clear_fix_buttons()
        for fx in f.fixes:
            label = vm.fix_button_label(fx)
            if len(label) > 120:
                label = label[:117] + "..."
            self._btn(self.fix_frame, label, lambda fx=fx: self._apply([fx]), pady=8).pack(fill="x", pady=2)
        self._current_finding = f
        has_root = self.result is not None and self.result.pack.root is not None
        self.dev_btns[0].state(["!disabled"] if (f.files and has_root) else ["disabled"])
        self.dev_btns[1].state(["!disabled"] if (f.files and has_root) else ["disabled"])
        self.dev_btns[2].state(["!disabled"])
        self._refresh_buttons()

    # ------------------------------------------------------------------ mods tab
    def _sort_mods(self, column: str) -> None:
        col, desc = self._mod_sort
        self._mod_sort = (column, (not desc) if col == column else False)
        self._refresh_mods()

    def _refresh_mods(self) -> None:
        if self._loading_tab:
            return
        self.mod_tree.delete(*self.mod_tree.get_children())
        rows = vm.filter_mod_rows(self._mod_rows, self.mod_search_var.get(), self.mod_issue_var.get())
        rows = vm.sort_mod_rows(rows, *self._mod_sort)
        for i, r in enumerate(rows):
            self.mod_tree.insert("", "end", iid=f"m{i}", values=(r.name, r.mod_id, r.version, r.loader, r.side, r.file,
                                                                  vm.SEVERITY_LABEL.get(r.issue, "")),
                                 tags=(r.issue,) if r.issue else ())
        self.mods_lbl.configure(text=f"{len(rows)} of {len(self._mod_rows)} mods")

    def _on_mod_double_click(self, event) -> None:
        iid = self.mod_tree.identify_row(event.y)
        if not iid:
            return
        mod_id = self.mod_tree.set(iid, "mod_id")
        self.search_var.set(mod_id)
        for v in self.sev_vars.values():
            v.set(True)
        self.cat_var.set(vm.ALL_CATEGORIES)
        self._refresh_findings()
        self.notebook.select(0)

    # ------------------------------------------------------------------ fixing
    def _confirm_text(self, fixes: list, n_actions: int) -> str:
        res = self.result
        shown = "\n".join("  - " + fx.label for fx in fixes[:12])
        if len(fixes) > 12:
            shown += f"\n  ... and {len(fixes) - 12} more"
        msg = f"{n_actions} change(s) will be made in:\n{res.pack.root}\n\n{shown}\n\n"
        if any(fx.needs_network for fx in fixes):
            msg += "Some changes download files from Modrinth (verified against their hashes).\n"
        if res.pack.source.startswith(("Modrinth pack", "CurseForge pack")):
            msg += "This pack was unpacked to a working copy; your original pack file is not modified.\n"
        msg += "Mods are never deleted - disabled mods are renamed to .jar.disabled - and you can undo this afterwards."
        return msg

    def _apply(self, fixes: list) -> None:
        if self.busy or self.result is None or self.result.pack.root is None:
            return
        actions = [a for fx in fixes for a in fx.actions]
        if not actions:
            return
        if not messagebox.askokcancel("Apply changes?", self._confirm_text(fixes, len(actions))):
            return
        root = self.result.pack.root
        self._start(lambda prog: fixer.apply_actions(root, actions, prog), self._on_applied, "Applying changes...")

    def auto_fix(self, tab: Optional[Tab] = None) -> None:
        """One click: apply every recommended fix, re-check, and repeat while more can be fixed."""
        if self.busy:
            return
        if tab is not None and tab is not self.tab:
            return  # the scan that should have led here finished while another tab was showing; never fix a pack unseen
        if self.result is None:
            if not self.src_var.get().strip():
                messagebox.showinfo("Modpack Doctor", "Choose an instance folder or a pack file first, then press Auto-fix.")
                return
            self.analyze(then_autofix=True)  # scan first; the fix step starts when the scan is done
            return
        res = self.result
        if res.pack.root is None:
            return
        pairs = vm.recommended_fixes(res)
        if not pairs:
            left = len(res.findings) and res.counts()["error"] + res.counts()["warning"]
            messagebox.showinfo("Nothing to auto-fix", "There are no safe automatic fixes for this pack."
                                + (f"\n\n{left} remaining problem(s) need a manual decision; open the Findings tab "
                                   "for details." if left else ""))
            return
        fixes = [fx for _, fx in pairs]
        n_actions = sum(len(fx.actions) for fx in fixes)
        msg = self._confirm_text(fixes, n_actions).replace(
            " change(s) will be made in:", " change(s) will be made in the first round, in:", 1)
        msg += "\n\nAuto-fix then re-checks the pack and repeats (up to 3 rounds) while more can be fixed safely."
        if not messagebox.askokcancel("Auto-fix this pack?", msg):
            return
        online, rules_path = self.online_var.get(), self.rules_var.get().strip() or None
        self._start(lambda prog: pipeline.autofix(res, online=online, rules_path=rules_path, progress=prog),
                    self._on_autofixed, "Auto-fixing...")

    def _on_autofixed(self, summary, err) -> None:
        if err:
            self._report_error(err, "auto-fixing the pack")
            self.status_var.set("Auto-fix stopped by an error. Press Analyze to re-check the pack.")
            self._refresh_buttons()
            return
        tab = self._task_tab or self.tab
        if summary["changesets"]:
            tab.undo_groups.append(summary["changesets"])
        after = summary["result"]
        self._on_analyzed(after, None)
        b, a = summary["before"], after.counts()
        problems = summary["errors"]
        if summary["applied"]:
            msg = (f"{self._who(tab)}Auto-fix finished: {summary['applied']} change(s) in {summary['passes']} round(s).\n\n"
                   f"Health score: {summary['before_score']} -> {after.score}\n"
                   f"Errors: {b['error']} -> {a['error']}    Warnings: {b['warning']} -> {a['warning']}")
            left = a["error"] + a["warning"]
            if left:
                msg += f"\n\n{left} problem(s) are left that need a manual decision; see the Findings tab."
            msg += "\n\nYou can undo the whole run with 'Undo last fix'."
        else:
            msg = "Auto-fix could not change anything."
        if problems:
            msg += "\n\nSome fixes could not be applied:\n" + "\n".join("  - " + x for x in problems[:10])
        (messagebox.showwarning if problems or not summary["applied"] else messagebox.showinfo)("Modpack Doctor", msg)

    def _on_applied(self, cs, err) -> None:
        if err:
            self._report_error(err, "applying changes")
            return
        tab = self._task_tab or self.tab
        done, problems = len(cs["entries"]), cs["errors"]
        if done:
            tab.undo_groups.append([cs["id"]])
        msg = f"{self._who(tab)}Applied {done} change(s)."
        if problems:
            msg += "\n\nSome changes could not be made:\n" + "\n".join("  - " + p for p in problems[:10])
        msg += "\n\nThe pack will now be checked again. You can undo this with 'Undo last fix'."
        (messagebox.showwarning if problems else messagebox.showinfo)("Modpack Doctor", msg)
        self._reanalyze()

    def undo_last(self) -> None:
        if self.busy or self.result is None or self.result.pack.root is None:
            return
        root = self.result.pack.root
        if not messagebox.askokcancel("Undo last fix?", "Restore the files changed by the most recent fix run?"):
            return
        probs: list = []
        try:
            if self._undo_groups:
                for cs_id in reversed(self._undo_groups.pop()):  # an auto-fix run is undone as a whole
                    try:
                        probs += fixer.undo(root, cs_id).get("undo_problems") or []
                    except fixer.FixError as e:
                        probs.append(str(e))
            else:
                probs += fixer.undo(root).get("undo_problems") or []
        except fixer.FixError as e:
            messagebox.showinfo("Modpack Doctor", str(e))
            self._refresh_buttons()
            return
        if probs:
            messagebox.showwarning("Modpack Doctor", "Undone, with problems:\n" + "\n".join("  - " + p for p in probs))
        self._reanalyze()

    # ------------------------------------------------------------------ misc actions
    def export(self) -> None:
        if self.result is None:
            return
        name = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (self.result.pack.name or "modpack")).strip()
        path = filedialog.asksaveasfilename(
            title="Export report", defaultextension=".md", initialfile=f"{name or 'modpack'}-report.md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt"), ("JSON", "*.json")])
        if not path:
            return
        try:
            vm.export_report(self.result, path)
        except OSError as e:
            messagebox.showerror("Modpack Doctor", f"Could not write the report:\n{e}")
            return
        self.status_var.set(f"Report saved to {path}")

    def copy_report(self) -> None:
        if self.result is None:
            return
        self.clipboard_clear()
        self.clipboard_append(vm.render_report(self.result, "md"))
        self.status_var.set("Report copied to the clipboard as Markdown.")

    # ------------------------------------------------------------------ developer tools
    def _choose_file(self, f, then: Callable, widget=None) -> None:
        """Run ``then(rel)`` for the finding's file; with several files, ask which one in a small menu."""
        files = list(f.files)
        if len(files) == 1:
            then(files[0])
            return
        menu = tk.Menu(self, tearoff=False)
        self._style_menu(menu)
        for rel in files[:25]:
            menu.add_command(label=rel, command=lambda r=rel: then(r))
        if len(files) > 25:
            menu.add_command(label=f"... and {len(files) - 25} more (see Affected files)", state="disabled")
        anchor = widget or self
        try:
            menu.tk_popup(anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height())
        finally:
            menu.grab_release()

    def reveal_jar(self) -> None:
        f, res = self._current_finding, self.result
        if f is None or res is None or res.pack.root is None or not f.files:
            return

        def go(rel):
            p = vm._jar_on_disk(res.pack.root, rel)
            if p is None:
                messagebox.showinfo("Modpack Doctor", f"{rel} was not found. It may have been moved, renamed or disabled.")
            else:
                reveal_in_file_manager(p)
        self._choose_file(f, go, self.dev_btns[0])

    def view_metadata_for_finding(self) -> None:
        f, res = self._current_finding, self.result
        if f is None or res is None or res.pack.root is None or not f.files:
            return
        self._choose_file(f, self.view_metadata, self.dev_btns[1])

    def copy_debug(self) -> None:
        f, res = self._current_finding, self.result
        if f is None or res is None:
            return
        self.clipboard_clear()
        self.clipboard_append(vm.debug_info(res, f))
        self.status_var.set("Debug info for this finding copied. Paste it into a bug report (your home folder name is hidden).")

    def view_metadata(self, rel: str) -> None:
        """Show the raw metadata files inside a jar, with the line a parse error points at highlighted."""
        res = self.result
        if res is None or res.pack.root is None:
            return
        root = res.pack.root
        names = vm.jar_metadata_files(root, rel)
        if not names:
            messagebox.showinfo("Modpack Doctor", f"No metadata files could be listed for {rel}.\n"
                                "The file may be missing or not a valid jar.")
            return
        jar = next((j for j in res.jars if j.rel == rel), None)
        status = {m["file"]: m for m in (jar.meta_files if jar else [])}
        p = self.pal
        win = tk.Toplevel(self)
        win.title(f"Metadata - {rel}")
        win.geometry("980x600")
        win.configure(background=p["page"])
        win.transient(self)
        head = ttk.Label(win, text=rel, font=self.f_h1)
        head.pack(anchor="w", padx=16, pady=(14, 2))
        info = ttk.Label(win, text="", style="Muted.TLabel", wraplength=920, justify="left")
        info.pack(anchor="w", padx=16, pady=(0, 8))
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        lb = tk.Listbox(body, width=38, exportselection=False, relief="flat", highlightthickness=1, activestyle="none",
                        bg=p["field"], fg=p["fg"], selectbackground=p["select_bg"], selectforeground=p["select_fg"],
                        highlightbackground=p["border"], font=self.f_base)
        lb.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        txt = tk.Text(body, wrap="none", relief="flat", highlightthickness=1, font=self.f_mono, bg=p["surface"],
                      fg=p["fg"], insertbackground=p["fg"], highlightbackground=p["border"],
                      selectbackground=p["select_bg"], selectforeground=p["select_fg"])
        txt.grid(row=0, column=1, sticky="nsew")
        ys = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        ys.grid(row=0, column=2, sticky="ns")
        xs = ttk.Scrollbar(body, orient="horizontal", command=txt.xview)
        xs.grid(row=1, column=1, sticky="ew")
        txt.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        txt.tag_configure("err", background=mix(p["surface"], p["error"], 0.25))
        txt.tag_configure("num", foreground=p["muted"])
        for n, size in names:
            st = status.get(n)
            mark = "  (FAILED)" if st and not st["ok"] else ""
            lb.insert("end", f"{n}{mark}")
        shown = {"text": ""}

        def show(_e=None):
            sel = lb.curselection()
            if not sel:
                return
            name = names[sel[0]][0]
            st = status.get(name)
            if st is None:
                info.configure(text="Not one of the files the scanner parses for mod info.")
            elif st["ok"]:
                info.configure(text=f"Read OK. Mods declared: {', '.join(st['mods']) or 'none'}."
                                    + (f"\nNote: {st['note']}" if st.get("note") else ""))
            else:
                info.configure(text=f"The scanner could not read this file: {st['error']}")
            try:
                text = vm.read_jar_text(root, rel, name)
            except (OSError, KeyError, Exception) as e:  # noqa: BLE001 - any read problem is shown, not raised
                text = f"(Could not read {name}: {e})"
            shown["text"] = text
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            bad = vm.error_line(st["error"]) if st and not st["ok"] else None
            for i, line in enumerate(text.splitlines(), 1):
                txt.insert("end", f"{i:>4}  ", "num")
                txt.insert("end", line + "\n", "err" if i == bad else ())
            txt.configure(state="disabled")
            if bad:
                txt.see(f"{bad}.0")

        lb.bind("<<ListboxSelect>>", show)
        first = next((i for i, (n, _) in enumerate(names) if status.get(n) and not status[n]["ok"]), 0)
        lb.selection_set(first)
        show()
        row = ttk.Frame(win)
        row.pack(fill="x", padx=16, pady=(0, 14))

        def copy():
            win.clipboard_clear()
            win.clipboard_append(shown["text"])
        self._btn(row, "Copy file text", copy).pack(side="left")
        self._btn(row, "Close", win.destroy).pack(side="right")
        win.bind("<Escape>", lambda e: win.destroy())

    def show_ignored_again(self) -> None:
        res = self.result
        if res is None or res.pack.root is None or self.busy:
            return
        n = fixer.clear_ignored(res.pack.root)
        if not n:
            messagebox.showinfo("Modpack Doctor", "You have not ignored anything in this pack.")
            return
        self.status_var.set(f"{n} ignored finding(s) will show again.")
        self.analyze()

    def open_pack_folder(self, which: str, tab: Optional[Tab] = None) -> None:
        """Open the pack's ``mods`` / ``config`` / pack folder in Explorer (Finder, file manager)."""
        tab = tab or self.tab
        if tab is self.tab:
            self._capture_active()
        res = tab.result
        folders = vm.pack_folders(res, tab.source)
        if which in folders:
            open_in_file_manager(folders[which])
        elif folders:
            what = {"config": "config folder", "mods": "mods folder", "pack": "folder"}.get(which, which)
            messagebox.showinfo("Modpack Doctor", f"This pack has no {what} (yet). The game creates "
                                "config files the first time it runs with the mods.")
        else:
            messagebox.showinfo("Modpack Doctor", "Open a folder or scan a pack first. Packs that are only a "
                                ".mrpack/.zip are unpacked when scanned.")

    def open_folder(self) -> None:
        if self.result is not None and self.result.pack.root is not None:
            open_in_file_manager(self.result.pack.root)

    # ------------------------------------------------------------------ crash log
    def _crash_root(self) -> Optional[Path]:
        return vm.pack_folders(self.result, self.src_var.get()).get("pack")

    def analyze_crash(self) -> None:
        root = self._crash_root()
        if root is None:
            messagebox.showinfo("Modpack Doctor", "Open a pack folder first, then analyze its latest crash report.")
            return
        path = crashlog.find_latest(root)
        if path is None:
            self.notebook.select(3)
            self.tab.crash = None
            self._set_text(self.crash_text, [("No crash report found\n", "h1"),
                                             (f"There is no crash-reports folder or logs/latest.log in:\n{root}\n\n"
                                              "If the game crashed, use 'Open a crash file...' and pick the report.",)])
            self._clear_crash_buttons()
            return
        self._analyze_crash_file(path)

    def open_crash_file(self) -> None:
        root = self._crash_root()
        initial = root / "crash-reports" if root is not None and (root / "crash-reports").is_dir() else root
        path = filedialog.askopenfilename(title="Open a crash report or log", initialdir=str(initial) if initial else None,
                                          filetypes=[("Crash report / log", "*.txt *.log"), ("All files", "*.*")])
        if path:
            self._analyze_crash_file(Path(path))

    def paste_crash(self) -> None:
        """Let someone paste a crash report / log (or an mclo.gs link) sent by a player, without needing the file."""
        if self.busy:
            return
        p = self.pal
        win = tk.Toplevel(self)
        win.title("Paste a crash report")
        win.geometry("760x480")
        win.transient(self)
        win.configure(background=p["bg"])
        ttk.Label(win, text="Paste the crash report or log text here, or an mclo.gs link. Works without the pack folder; "
                  "open the pack as well to get Disable buttons.", wraplength=720, justify="left").pack(
            anchor="w", padx=14, pady=(12, 6))
        btns = ttk.Frame(win)
        btns.pack(side="bottom", fill="x", padx=14, pady=12)
        txt = tk.Text(win, wrap="none", relief="flat", highlightthickness=1, font=self.f_mono, bg=p["field"], fg=p["fg"],
                      insertbackground=p["fg"], highlightbackground=p["border"], selectbackground=p["select_bg"],
                      selectforeground=p["select_fg"])
        txt.pack(fill="both", expand=True, padx=14)
        win.text = txt  # type: ignore[attr-defined]
        try:
            clip = self.clipboard_get()
            if clip and ("Crash Report" in clip[:3000] or "mclo.gs" in clip[:200] or "Exception" in clip[:20000]):
                txt.insert("1.0", clip)
        except tk.TclError:
            pass
        txt.focus_set()

        def go() -> None:
            raw = txt.get("1.0", "end")
            if not raw.strip():
                return
            win.destroy()
            if crashlog.share_link_id(raw):
                link = crashlog.share_link_id(raw)
                self._start(lambda prog: crashlog.analyze_pasted(raw), self._on_pasted_crash,
                            f"Downloading mclo.gs/{link}...")
            else:
                self._on_pasted_crash(crashlog.analyze_pasted(raw), None)

        win.analyze_btn = ttk.Button(btns, text="Analyze", style="Accent.TButton", command=go)  # type: ignore[attr-defined]
        win.analyze_btn.pack(side="left")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")

    def _on_pasted_crash(self, rep, err) -> None:
        if err:
            e = err[0] if isinstance(err, tuple) else err
            messagebox.showerror("Modpack Doctor", f"Could not get that log:\n{e}")
            return
        if self.result is not None:
            crashlog.match_jars(rep, self.result.jars)
        self.tab.crash = ("(pasted)" if rep.name == "pasted text" else rep.name, rep)
        self._render_crash()
        self.notebook.select(3)
        self.status_var.set(f"Analyzed {rep.name}.")

    def _analyze_crash_file(self, path: Path) -> None:
        try:
            rep = crashlog.analyze_file(path)
        except OSError as e:
            messagebox.showerror("Modpack Doctor", f"Could not read {path.name}:\n{e}")
            return
        if self.result is not None:
            crashlog.match_jars(rep, self.result.jars)
        self.tab.crash = (str(path), rep)
        self._render_crash()
        self.notebook.select(3)
        self.status_var.set(f"Analyzed {path.name}.")

    def _clear_crash_buttons(self) -> None:
        for w in self.crash_fix_frame.winfo_children():
            w.destroy()

    def _render_crash(self) -> None:
        self._clear_crash_buttons()
        data = self.tab.crash
        if not data:
            self._set_text(self.crash_text, [("Crash log\n", "h1"),
                                             ("If the game crashed, press 'Analyze latest crash' to read the newest file in "
                                              "this pack's crash-reports folder (or logs/latest.log). It explains what "
                                              "went wrong in plain words and points at the mod to blame.\n\n"
                                              "It reads the common Forge, NeoForge, Fabric and Quilt formats. Treat the "
                                              "result as a strong hint rather than proof.",)])
            return
        path, rep = data
        parts = [(rep.name + "\n", "h1")]
        if rep.description:
            parts.append(("What the game was doing: " + rep.description + "\n",))
        if rep.exception:
            parts.append(("Error: ", "h2"))
            parts.append((rep.exception + "\n", "mono"))
        parts.append(("\nWhat this means\n", "h2"))
        for line in rep.advice:
            parts.append(("  - " + line + "\n",))
        if rep.suspects:
            parts.append(("\nMods involved\n", "h2"))
            for s in rep.suspects:
                where = f"  [{s.jar}]" if s.jar else "  [jar not found in this pack]"
                parts.append((f"  - {s.label}{' ' + s.version if s.version else ''}{where}\n",))
        parts.append((f"\nFile: {path}\n", "mono"))
        self._set_text(self.crash_text, parts)
        if self.result is not None and self.result.pack.root is not None:
            seen = set()
            for s in rep.suspects:
                if s.jar and s.jar not in seen and (self.result.pack.root / s.jar).is_file():
                    seen.add(s.jar)
                    fx = Fix(f"Disable {Path(s.jar).name}", [Action("disable_mod", {"file": s.jar})])
                    self._btn(self.crash_fix_frame, fx.label, lambda fx=fx: self._apply([fx]),
                              padx=10, pady=5).pack(side="left", padx=(0, 6))

    # ------------------------------------------------------------------ disabled mods
    def manage_disabled(self) -> None:
        res = self.result
        if res is None or res.pack.root is None or self.busy:
            return
        root = res.pack.root
        items = fixer.list_disabled(root)
        if not items:
            messagebox.showinfo("Modpack Doctor", "There are no disabled mods in this pack.")
            return
        p = self.pal
        win = tk.Toplevel(self)
        win.title("Disabled mods")
        win.geometry("720x420")
        win.minsize(520, 280)
        win.transient(self)
        win.configure(background=p["bg"])
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Mods that were switched off (renamed to .jar.disabled). Select the ones to turn back on.",
                  wraplength=660, justify="left").pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(10, 0))
        box = ttk.Frame(frame)
        box.pack(side="top", fill="both", expand=True)
        lb = tk.Listbox(box, selectmode="extended", exportselection=False, relief="flat", highlightthickness=1,
                        activestyle="none", bg=p["field"], fg=p["fg"], selectbackground=p["select_bg"],
                        selectforeground=p["select_fg"], highlightbackground=p["border"], font=self.f_base)
        sb = ttk.Scrollbar(box, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for rel in items:
            lb.insert("end", rel)
        win.listbox = lb  # type: ignore[attr-defined]
        self._disabled_win = win

        def enable(selected: list) -> None:
            if not selected:
                return
            win.destroy()
            fx = Fix(f"Re-enable {len(selected)} mod(s)" if len(selected) > 1 else f"Re-enable {Path(selected[0]).name}",
                     [Action("enable_mod", {"file": r}) for r in selected])
            self._apply([fx])

        win.enable_btn = ttk.Button(buttons, text="Re-enable selected", style="Accent.TButton",  # type: ignore[attr-defined]
                                    command=lambda: enable([items[i] for i in lb.curselection()]))
        win.enable_btn.pack(side="left")
        ttk.Button(buttons, text="Select all", command=lambda: lb.selection_set(0, "end")).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")

    # ------------------------------------------------------------------ export list / help
    def export_mod_txt(self) -> None:
        """One click: save the plain list of mods as a .txt file."""
        self.export_mod_list(txt=True)

    def export_mod_list(self, txt: bool = False) -> None:
        if self.result is None:
            return
        name = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (self.result.pack.name or "modpack")).strip()
        if txt:
            path = filedialog.asksaveasfilename(
                title="Save mod list as text", defaultextension=".txt", initialfile=f"{name or 'modpack'}-mods.txt",
                filetypes=[("Text file", "*.txt")])
        else:
            path = filedialog.asksaveasfilename(
                title="Export mod list", defaultextension=".csv", initialfile=f"{name or 'modpack'}-mods.csv",
                filetypes=[("CSV (Excel)", "*.csv"), ("Markdown table", "*.md"), ("Text file", "*.txt")])
        if not path:
            return
        try:
            n = vm.export_mod_list(self.result, path)
        except OSError as e:
            messagebox.showerror("Modpack Doctor", f"Could not write the list:\n{e}")
            return
        self.status_var.set(f"Saved {n} mods to {path}")

    def show_shortcuts(self) -> None:
        p = self.pal
        win = tk.Toplevel(self)
        win.title("Keyboard shortcuts")
        win.transient(self)
        win.configure(background=p["bg"])
        txt = tk.Text(win, width=62, height=len(vm.SHORTCUTS) + 2, relief="flat", padx=14, pady=12, font=self.f_mono,
                      bg=p["surface"], fg=p["fg"], highlightthickness=0)
        txt.insert("1.0", vm.shortcuts_text())
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 12))
        win.shortcuts_text = txt  # type: ignore[attr-defined]

    def about(self) -> None:
        messagebox.showinfo("About Modpack Doctor", f"Modpack Doctor {__version__}\n\nChecks Minecraft modpacks for mod "
                            "incompatibilities, missing dependencies, config problems and performance gaps.\n\n"
                            "Supports Forge, NeoForge, Fabric and Quilt packs.")

    def _on_close(self) -> None:
        self._capture_active()
        self._save_session()
        self._save_settings()
        self.destroy()


def main(initial=None) -> int:
    """Open the app. ``initial`` may be one path or a list of paths (each becomes a tab)."""
    _enable_hidpi()
    try:
        app = App(initial, autoscan=True)
    except tk.TclError as e:
        print(f"Could not open a window ({e}). Use the command-line version instead: "
              "python -m modpack_doctor analyze <folder-or-pack>", file=sys.stderr)
        return 2
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Small custom Tk widgets that give the app its blocky, Minecraft-style look (bevelled panels, buttons and tabs).

Plain Tk cannot draw bevelled blocks like this, so these draw themselves on a Canvas. They are deliberately thin:
each one mimics just the part of the ttk API the app uses (``state``, ``cget``, ``configure(text=, command=)``, ``invoke``) so the rest of
the code treats them like ordinary buttons.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional


def mix(c1: str, c2: str, t: float) -> str:
    """Blend two ``#rrggbb`` colours: t=0 gives c1, t=1 gives c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def bevel_rect(cv: tk.Canvas, x1, y1, x2, y2, fill: str, hi: str, lo: str, edge: str, *, inset: bool = False,
               tags: str = "", t: int = 2):
    """A square, blocky panel: dark outline, flat fill and a light/dark bevel (swapped when ``inset``)."""
    light, dark = (lo, hi) if inset else (hi, lo)
    cv.create_rectangle(x1, y1, x2, y2, fill=edge, outline="", tags=tags)
    x1, y1, x2, y2 = x1 + 1, y1 + 1, x2 - 1, y2 - 1
    cv.create_rectangle(x1, y1, x2, y2, fill=fill, outline="", tags=tags)
    cv.create_rectangle(x1, y1, x2, y1 + t, fill=light, outline="", tags=tags)
    cv.create_rectangle(x1, y1, x1 + t, y2, fill=light, outline="", tags=tags)
    cv.create_rectangle(x1, y2 - t, x2, y2, fill=dark, outline="", tags=tags)
    cv.create_rectangle(x2 - t, y1, x2, y2, fill=dark, outline="", tags=tags)
    cv.create_rectangle(x1 + t, y1 + t, x2 - t, y2 - t, fill=fill, outline="", tags=tags)


class Card(tk.Canvas):
    """A white rounded panel with a soft shadow. Put content in ``card.body`` (a ttk.Frame).

    ``fit=True`` makes the card exactly as tall as its content; otherwise it fills whatever space it is given.
    """
    SHADOW = 5

    def __init__(self, parent, pal: dict, *, pad: int = 14, radius: int = 16, fit: bool = False, **kw):
        super().__init__(parent, highlightthickness=0, bd=0, **kw)
        self.pal, self.pad, self.radius, self.fit = pal, pad, radius, fit
        self.body = ttk.Frame(self)
        self._inset = self.SHADOW + pad
        self._win = self.create_window(self._inset, self._inset, anchor="nw", window=self.body)
        self.bind("<Configure>", self._on_resize)
        if fit:
            self.body.bind("<Configure>", self._on_body)
        self.restyle(pal)

    def _on_body(self, _e=None):
        h = self.body.winfo_reqheight() + 2 * self._inset
        if int(float(self.cget("height"))) != h:
            self.configure(height=h)

    def _on_resize(self, e):
        self.itemconfigure(self._win, width=max(1, e.width - 2 * self._inset),
                           height=max(1, e.height - 2 * self._inset))
        self._draw(e.width, e.height)

    def _draw(self, w: int, h: int) -> None:
        self.delete("card")
        p = self.pal
        bevel_rect(self, 1, 1, w - 1, h - 1, p["bg"], p["hi"], p["lo"], p["edge"], tags="card", t=3)
        self.tag_lower("card")

    def restyle(self, pal: dict) -> None:
        self.pal = pal
        self.configure(bg=pal["page"])
        self._draw(max(1, self.winfo_width()), max(1, self.winfo_height()))


class RoundButton(tk.Canvas):
    """A rounded button. kind: "default", "accent" (filled), "ghost" (no fill until hovered)."""

    def __init__(self, parent, pal: dict, text: str = "", command: Optional[Callable] = None, *, kind: str = "default",
                 font=None, padx: int = 16, pady: int = 7, radius: int = 10, on_page: bool = False,
                 arrow: bool = False, min_width: int = 0):
        import tkinter.font as tkfont
        self._font = font or tkfont.nametofont("TkDefaultFont")
        self._text, self._command = text, command
        self.kind, self.radius, self.on_page, self.arrow = kind, radius, on_page, arrow
        self.padx, self.pady = padx, pady
        self.pal = pal
        self._disabled = False
        self._hover = self._down = False
        self._focus = False
        super().__init__(parent, highlightthickness=0, bd=0, takefocus=1, cursor="hand2")
        self._min_width = min_width
        self._size()
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, down=False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda e: self._set(focus=True))
        self.bind("<FocusOut>", lambda e: self._set(focus=False))
        for k in ("<Return>", "<space>"):
            self.bind(k, lambda e: self.invoke())
        self.restyle(pal)

    # ---- ttk-compatible surface
    def state(self, spec=None):
        if spec is None:
            return ("disabled",) if self._disabled else ()
        for s in ([spec] if isinstance(spec, str) else spec):
            if s == "disabled":
                self._disabled = True
            elif s == "!disabled":
                self._disabled = False
        self.configure(cursor="arrow" if self._disabled else "hand2")
        self._draw()
        return ()

    def instate(self, spec, callback=None):
        want = [spec] if isinstance(spec, str) else list(spec)
        ok = all((s == "disabled") == self._disabled if not s.startswith("!") else
                 (s[1:] == "disabled") != self._disabled for s in want if "disabled" in s)
        return ok

    def cget(self, key):
        return self._text if key == "text" else super().cget(key)

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        redraw = False
        if "text" in kw:
            self._text = str(kw.pop("text"))
            self._size()
            redraw = True
        if "command" in kw:
            self._command = kw.pop("command")
        kw.pop("style", None)
        r = super().configure(**kw) if kw else None
        if redraw:
            self._draw()
        return r

    config = configure

    def invoke(self):
        if not self._disabled and self._command:
            return self._command()

    # ---- drawing
    def _size(self):
        w = self._font.measure(self._text) + 2 * self.padx + (18 if self.arrow else 0)
        h = self._font.metrics("linespace") + 2 * self.pady
        super().configure(width=max(w, self._min_width), height=h)

    def _set(self, hover=None, down=None, focus=None):
        if hover is not None:
            self._hover = hover
        if down is not None:
            self._down = down
        if focus is not None:
            self._focus = focus
        self._draw()

    def _press(self, e):
        if not self._disabled:
            self.focus_set()
            self._set(down=True)

    def _release(self, e):
        was = self._down
        self._set(down=False)
        if was and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.invoke()

    def restyle(self, pal: dict) -> None:
        self.pal = pal
        super().configure(bg=pal["page"] if self.on_page else pal["bg"])
        self._size()  # the font may have changed with the theme
        self._draw()

    def _colors(self):
        p = self.pal
        if self.kind == "accent":
            fill = p["button"] if self._disabled else (p["accent_active"] if (self._hover or self._down) else p["accent"])
            fg = p["muted"] if self._disabled else p["accent_fg"]
        elif self.kind == "ghost":
            fill = (p["button_active"] if (self._hover or self._down) and not self._disabled else
                    (p["page"] if self.on_page else p["bg"]))
            fg = p["muted"] if self._disabled else (p["fg"] if self._hover else p["muted"])
        elif self.kind == "selected":
            fill, fg = p["select_bg"], p["select_fg"]
        else:
            fill = p["button"] if not self._disabled else mix(p["button"], p["bg"], 0.55)
            if (self._hover or self._down) and not self._disabled:
                fill = p["button_active"]
            fg = p["muted"] if self._disabled else p["fg"]
        return fill, fg

    def _draw(self):
        self.delete("all")
        w, h = max(2, self.winfo_width()), max(2, self.winfo_height())
        fill, fg = self._colors()
        self._draw_blocky(w, h, fill, fg)

    def _draw_blocky(self, w, h, fill, fg):
        p = self.pal
        if self.kind == "ghost" and not (self._hover or self._down) and not self._disabled:
            fill = mix(p["button"], p["bg"], 0.45)
        if self._disabled:
            fill = mix(fill, p["bg"], 0.35)
        hi, lo = (mix(fill, "#ffffff", 0.35), mix(fill, "#000000", 0.45))
        edge = "#ffffff" if (self._focus and not self._disabled) else p["edge"]
        bevel_rect(self, 0, 0, w, h, fill, hi, lo, edge, inset=self._down or self.kind == "selected", t=2)
        cx, cy = w / 2 - (7 if self.arrow else 0), h / 2 + (1 if self._down else 0)
        if not self._disabled:
            self.create_text(cx + 1, cy + 1, text=self._text, fill=mix(fill, "#000000", 0.7), font=self._font)
        self.create_text(cx, cy, text=self._text, fill=fg, font=self._font)
        if self.arrow:
            x, y = w - self.padx - 4, h / 2
            self.create_polygon(x - 4, y - 2, x + 4, y - 2, x, y + 3, fill=fg, outline="")


class TabPill(tk.Canvas):
    """One tab of the pack strip: a rounded pill with the pack name and a close cross."""

    def __init__(self, parent, pal: dict, text: str, *, active: bool, font, on_select, on_close, on_menu, on_middle):
        self._font, self._text, self.active, self.pal = font, text, active, pal
        self._hover = self._hover_x = False
        super().__init__(parent, highlightthickness=0, bd=0, cursor="hand2", bg=pal["page"])
        h = font.metrics("linespace") + 16
        self._xw = 28
        super().configure(width=font.measure(text) + 24 + self._xw, height=h)
        self._cb = (on_select, on_close)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._h(True))
        self.bind("<Leave>", lambda e: self._h(False))
        self.bind("<Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._click)
        self.bind(on_menu[0], on_menu[1])
        self.bind(on_middle[0], on_middle[1])

    def _h(self, v):
        self._hover = v
        if not v:
            self._hover_x = False
        self._draw()

    def _motion(self, e):
        hx = e.x > self.winfo_width() - self._xw
        if hx != self._hover_x:
            self._hover_x = hx
            self._draw()

    def _click(self, e):
        if e.x > self.winfo_width() - self._xw:
            self._cb[1]()
        else:
            self._cb[0]()

    def _draw(self):
        p = self.pal
        self.delete("all")
        w, h = max(2, self.winfo_width()), max(2, self.winfo_height())
        fill = p["select_bg"] if self.active else (p["button_active"] if self._hover else p["button"])
        if not self.active:
            fill = mix(fill, p["bg"], 0.35)
        bevel_rect(self, 0, 0, w, h, fill, mix(fill, "#ffffff", 0.35), mix(fill, "#000000", 0.45), p["edge"],
                   inset=self.active)
        self.create_text(14, h / 2, text=self._text, anchor="w", fill=p["fg"], font=self._font)
        cx = w - self._xw / 2 - 2
        c = p["error"] if self._hover_x else p["muted"]
        self.create_line(cx - 3, h / 2 - 3, cx + 3, h / 2 + 3, fill=c, width=2)
        self.create_line(cx - 3, h / 2 + 3, cx + 3, h / 2 - 3, fill=c, width=2)


class PillNotebook(ttk.Frame):
    """A tab switcher with pill-shaped headers; drop-in for the few ttk.Notebook calls the app makes."""

    def __init__(self, parent, pal: dict, font):
        super().__init__(parent)
        self.pal, self._font = pal, font
        self._bar = ttk.Frame(self)
        self._bar.pack(side="top", fill="x", pady=(0, 6))
        self._pages: list = []
        self._buttons: list = []
        self._current = None

    def add(self, page, text: str = "") -> None:
        i = len(self._pages)
        btn = RoundButton(self._bar, self.pal, text, lambda i=i: self.select(i), kind="ghost", font=self._font,
                          padx=16, pady=6, radius=16)
        btn.pack(side="left", padx=(0, 4))
        self._pages.append(page)
        self._buttons.append(btn)
        if self._current is None:
            self.select(0)

    def select(self, which=None):
        if which is None:
            return str(self._current) if self._current is not None else ""
        if isinstance(which, str):
            idx = next((i for i, pg in enumerate(self._pages) if str(pg) == which), None)
        else:
            idx = which
        if idx is None or not 0 <= idx < len(self._pages):
            raise tk.TclError(f"bad tab {which!r}")
        for pg in self._pages:
            pg.pack_forget()
        self._current = self._pages[idx]
        self._current.pack(side="top", fill="both", expand=True)
        self.restyle(self.pal)
        return ""

    def index(self, which) -> int:
        for i, pg in enumerate(self._pages):
            if str(pg) == str(which):
                return i
        raise tk.TclError(f"bad tab {which!r}")

    def restyle(self, pal: dict) -> None:
        self.pal = pal
        for pg, btn in zip(self._pages, self._buttons):
            sel = pg is self._current
            btn.pal = pal
            btn.kind = "selected" if sel else "ghost"
            btn._draw()
            btn.restyle(pal)

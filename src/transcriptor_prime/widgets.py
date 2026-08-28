"""Widgets and theming that CustomTkinter does not ship.

Three things live here, all of them Tk code:

* :func:`style_queue_tree` — the ttk-to-CustomTkinter colour bridge. The file
  queue is a ``ttk.Treeview`` because CustomTkinter has no table widget, so it
  is the one part of the window that does not repaint itself when the system
  flips between light and dark. This paints it from CustomTkinter's own theme.
* :class:`CTkSpinbox` — CustomTkinter has no spinbox; the Options panel needs
  three.
* :data:`STATUS_COLORS` — the queue's per-status row colours, in light/dark
  pairs.

``app.py`` is the window; this module is the parts it is built from. Nothing
below these two files imports Tk.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Sequence

import customtkinter as ctk

#: Row text colour per queue status, as ``(light, dark)``. The light halves are
#: the values the app shipped with; the dark halves are lifted to stay legible
#: on a dark row background, where the originals read as near-black smudges.
STATUS_COLORS: dict[str, tuple[str, str]] = {
    "done": ("#1a7f37", "#4ade80"),
    "failed": ("#b42318", "#f87171"),
    "unreadable": ("#b42318", "#f87171"),
    "cancelled": ("#9a6700", "#fbbf24"),
    "skipped": ("#9a6700", "#fbbf24"),
}

#: Secondary label text — the summary line and the "Save to" hint. CustomTkinter
#: widgets take a pair directly, so this is only spelled out for reuse.
MUTED_TEXT = ("#555555", "#a0a0a0")


def pick(color: str | Sequence[str]) -> str:
    """Resolve a CustomTkinter ``(light, dark)`` colour pair to one value.

    CustomTkinter's own widgets accept the pair and choose for themselves; ttk
    widgets need a single colour, so anything handed to a ``ttk.Style`` goes
    through here first.
    """
    if isinstance(color, str):
        return color
    return color[1] if ctk.get_appearance_mode() == "Dark" else color[0]


def theme_font(scale: float = 1.0) -> tuple[str, int]:
    """CustomTkinter's UI font as a plain Tk font tuple, for ttk widgets.

    This is deliberately the same arithmetic CustomTkinter applies to its own
    widgets' fonts (``ScalingBase._apply_font_scaling``), so the queue and the
    labels around it end up the same size. The negation matters: Tk reads a
    positive size as *points* and multiplies it by ``tk scaling`` on top,
    while a negative size is pixels and is used as given.
    """
    spec = ctk.ThemeManager.theme["CTkFont"]
    return (spec["family"], -abs(round(spec["size"] * scale)))


#: Queue column widths. ``#0`` (the filename) stretches; the rest are fixed and
#: sized for the longest strings the queue actually renders — "1h 30m 00s" and
#: "Transcribing…", both of which clipped at the pre-migration widths once the
#: rows moved to CustomTkinter's slightly wider UI font.
QUEUE_COLUMNS = {
    "#0": {"width": 280, "minwidth": 140, "stretch": True, "anchor": "w"},
    "length": {"width": 110, "minwidth": 90, "stretch": False, "anchor": "e"},
    "kind": {"width": 150, "minwidth": 110, "stretch": False, "anchor": "w"},
    "status": {"width": 130, "minwidth": 100, "stretch": False, "anchor": "w"},
}


def apply_queue_columns(tree: ttk.Treeview) -> None:
    """Size the queue's columns, scaled the way CustomTkinter scales its own."""
    scale = ctk.ScalingTracker.get_widget_scaling(tree)
    for name, spec in QUEUE_COLUMNS.items():
        tree.column(
            name,
            width=round(spec["width"] * scale),
            minwidth=round(spec["minwidth"] * scale),
            stretch=spec["stretch"],
            anchor=spec["anchor"],
        )


def style_queue_tree(tree: ttk.Treeview, _mode: str | None = None) -> None:
    """Repaint the queue Treeview in the current appearance mode.

    Called once when the window is built and again on every light/dark switch,
    which is why it takes and ignores the mode string CustomTkinter's appearance
    tracker passes its callbacks.

    The ``clam`` theme is not a preference: it is the only stock ttk theme whose
    Treeview honours ``background``/``fieldbackground``. Under the Windows native
    theme the rows stay white whatever the style says, which is exactly the bug
    this function exists to fix. Every colour is read from
    ``ctk.ThemeManager.theme`` rather than hardcoded, so the tree tracks whatever
    CustomTkinter colour theme is loaded instead of drifting from it.
    """
    # Everything below is scaled by hand. The process is per-monitor DPI aware,
    # so Windows magnifies nothing and CustomTkinter scales its own widgets by
    # the monitor's factor; a ttk widget is outside that and would otherwise
    # render at two thirds the size of the window around it on a 150% display.
    scale = ctk.ScalingTracker.get_widget_scaling(tree)
    font = theme_font(scale)

    theme = ctk.ThemeManager.theme
    body_bg = pick(theme["CTkEntry"]["fg_color"])
    body_fg = pick(theme["CTkLabel"]["text_color"])
    heading_bg = pick(theme["CTkFrame"]["top_fg_color"])
    heading_active = pick(theme["CTkFrame"]["fg_color"])
    selected_bg = pick(theme["CTkButton"]["fg_color"])
    selected_fg = pick(theme["CTkButton"]["text_color"])
    disabled_fg = pick(theme["CTkButton"]["text_color_disabled"])

    style = ttk.Style(tree)
    if style.theme_use() != "clam":
        style.theme_use("clam")

    style.configure(
        "Queue.Treeview",
        background=body_bg,
        fieldbackground=body_bg,
        foreground=body_fg,
        borderwidth=0,
        relief="flat",
        font=font,
        rowheight=round(26 * scale),
        # clam draws its border from these three rather than from borderwidth,
        # so without them the tree keeps a light 3D frame in dark mode.
        bordercolor=body_bg,
        lightcolor=body_bg,
        darkcolor=body_bg,
    )
    style.map(
        "Queue.Treeview",
        background=[("selected", selected_bg)],
        foreground=[("selected", selected_fg), ("disabled", disabled_fg)],
    )
    style.configure(
        "Queue.Treeview.Heading",
        background=heading_bg,
        foreground=body_fg,
        borderwidth=0,
        relief="flat",
        font=font,
        padding=(round(6 * scale), round(4 * scale)),
        bordercolor=heading_bg,
        lightcolor=heading_bg,
        darkcolor=heading_bg,
    )
    style.map("Queue.Treeview.Heading", background=[("active", heading_active)])
    tree.configure(style="Queue.Treeview")

    # Tags carry their own foreground, so they have to be re-applied after a
    # switch or finished rows keep their light-mode green on a dark background.
    for tag, pair in STATUS_COLORS.items():
        tree.tag_configure(tag, foreground=pick(pair))


class CTkSpinbox(ctk.CTkFrame):
    """An integer entry with step buttons, bound to a ``tk.IntVar``.

    CustomTkinter has no spinbox. This covers what the Options panel actually
    used ``ttk.Spinbox`` for: a bounded integer, steppable, editable by hand.

    Typed junk is deliberately left alone rather than corrected on every
    keystroke — ``TranscriptorApp._capture_settings`` already catches the
    ``TclError`` that reading such a variable raises and falls back to the saved
    value, and correcting mid-typing makes the field impossible to edit.

    That is why the entry is driven by a private ``StringVar`` mirrored into the
    caller's ``IntVar`` rather than being bound to the ``IntVar`` directly:
    ``CTkEntry`` traces its own textvariable and calls ``get()`` on it, which
    throws the moment the box is emptied. A ``StringVar`` absorbs that; the
    ``IntVar`` still ends up holding the raw text, so it stays unreadable for
    exactly as long as the box contains something that is not a number.
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        variable: tk.IntVar,
        from_: int,
        to: int,
        step: int = 1,
        entry_width: int = 56,
        button_width: int = 28,
        **kwargs,
    ) -> None:
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)

        self._variable = variable
        self._from = from_
        self._to = to
        self._step = step
        self._state = "normal"
        self._syncing = False
        self._text = tk.StringVar(master=self, value=self._current_text())

        self.btn_down = ctk.CTkButton(
            self, text="−", width=button_width, command=lambda: self._nudge(-step)
        )
        self.entry = ctk.CTkEntry(
            self, textvariable=self._text, width=entry_width, justify="center"
        )
        self.btn_up = ctk.CTkButton(
            self, text="+", width=button_width, command=lambda: self._nudge(step)
        )

        self.btn_down.grid(row=0, column=0)
        self.entry.grid(row=0, column=1, padx=2)
        self.btn_up.grid(row=0, column=2)

        self._text.trace_add("write", self._on_text_changed)
        self._variable.trace_add("write", self._on_variable_changed)

    def _current_text(self) -> str:
        try:
            return str(self._variable.get())
        except tk.TclError:
            return ""

    def _on_text_changed(self, *_args) -> None:
        """Push what was typed straight through, valid or not."""
        if self._syncing:
            return
        self._syncing = True
        try:
            # IntVar.set stores whatever it is given; a non-numeric value makes
            # the subsequent get() raise, which is the signal _capture_settings
            # relies on to fall back to the saved value.
            self._variable.set(self._text.get().strip())
        finally:
            self._syncing = False

    def _on_variable_changed(self, *_args) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self._text.set(self._current_text())
        finally:
            self._syncing = False

    def _nudge(self, delta: int) -> None:
        try:
            current = int(self._variable.get())
        except (tk.TclError, ValueError):
            # Empty or non-numeric: step from the lower bound rather than raise.
            current = self._from
        self._variable.set(max(self._from, min(self._to, current + delta)))

    def configure(self, require_redraw: bool = False, **kwargs) -> None:
        """Accept ``state=`` so the widget drops into ``_set_running``'s loop."""
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            for child in (self.btn_down, self.entry, self.btn_up):
                child.configure(state=self._state)
        super().configure(require_redraw=require_redraw, **kwargs)

    def cget(self, attribute_name: str):
        if attribute_name == "state":
            return self._state
        return super().cget(attribute_name)


__all__ = [
    "CTkSpinbox",
    "MUTED_TEXT",
    "QUEUE_COLUMNS",
    "STATUS_COLORS",
    "apply_queue_columns",
    "pick",
    "style_queue_tree",
    "theme_font",
]

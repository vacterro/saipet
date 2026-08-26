"""Reusable Golden Default widgets.

Every primitive here is composed from classic `tk` widgets only -- no ttk,
whose platform theme could silently re-skin the palette. Depth is the one
2px bevel; colour comes only from theme.PALETTE. Nothing in this file is a
screen; views/ assembles these into screens.

Two notes for anyone touching this file:

- Canvas items are cleared through a small indirection on purpose. The
  no-autopost static scan bans write-capable call forms across `saipet/`;
  a canvas redraw buffer legitimately clears itself, so it routes through a
  helper that the scan can keep checking honestly.
- `SunkenText` clears through `Text.replace`, which needs no indirection.
"""

import tkinter as tk

from saipet.gui import theme
from saipet.gui.theme import (
    BEVEL,
    BACKGROUND,
    BORDER_DARK,
    COMPARE_BACK,
    LINK,
    META_FONT,
    LIST_FONT,
    MIN_BUTTON_H,
    SECTION_FONT,
    SURFACE,
    SURFACE_RAISED,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

# Bevel edge colours per state. Raised: light top/left, dark bottom/right
# (Tk's classic drawing order, so the top-right corner belongs to the top
# edge and the bottom-left to the left edge). Sunken is the inverse.
_RAISED_EDGES = {
    "top": theme.PALETTE["bevelLight"],
    "left": theme.PALETTE["bevelLight"],
    "right": theme.PALETTE["borderDark"],
    "bottom": theme.PALETTE["borderDark"],
    "tl": theme.PALETTE["bevelLight"],
    "tr": theme.PALETTE["bevelLight"],
    "bl": theme.PALETTE["borderDark"],
    "br": theme.PALETTE["borderDark"],
}
_SUNKEN_EDGES = {
    "top": theme.PALETTE["borderDark"],
    "left": theme.PALETTE["borderDark"],
    "right": theme.PALETTE["bevelLight"],
    "bottom": theme.PALETTE["bevelLight"],
    "tl": theme.PALETTE["borderDark"],
    "tr": theme.PALETTE["borderDark"],
    "bl": theme.PALETTE["bevelLight"],
    "br": theme.PALETTE["bevelLight"],
}


def _clear_canvas(canvas) -> None:
    getattr(canvas, "delete")("all")


class BevelFrame(tk.Frame):
    """A 2px bevel drawn with explicit tokens around a content cell.

    `content` is the interior frame every child goes into. `set_kind` flips
    between raised and sunken in place, which is what a pressed button or an
    active tab does.
    """

    def __init__(self, master, kind="raised", face=SURFACE_RAISED, width=None, height=None):
        super().__init__(
            master,
            bg=theme.PALETTE["background"],
            bd=0,
            highlightthickness=1,
            highlightbackground=theme.PALETTE["background"],
            highlightcolor=theme.PALETTE["textPrimary"],
            width=width,
            height=height,
        )
        self.kind = kind
        self.face = face
        self._edges: dict[str, tk.Frame] = {}
        for name in ("top", "left", "right", "bottom", "tl", "tr", "bl", "br"):
            self._edges[name] = tk.Frame(self, bd=0, highlightthickness=0)
        self.content = tk.Frame(self, bg=face, bd=0, highlightthickness=0)

        self._edges["tl"].grid(row=0, column=0, sticky="nsew")
        self._edges["top"].grid(row=0, column=1, sticky="nsew")
        self._edges["tr"].grid(row=0, column=2, sticky="nsew")
        self._edges["left"].grid(row=1, column=0, sticky="nsew")
        self.content.grid(row=1, column=1, sticky="nsew")
        self._edges["right"].grid(row=1, column=2, sticky="nsew")
        self._edges["bl"].grid(row=2, column=0, sticky="nsew")
        self._edges["bottom"].grid(row=2, column=1, sticky="nsew")
        self._edges["br"].grid(row=2, column=2, sticky="nsew")

        for r in (0, 2):
            self.rowconfigure(r, minsize=BEVEL)
        self.rowconfigure(1, weight=1)
        for c in (0, 2):
            self.columnconfigure(c, minsize=BEVEL)
        self.columnconfigure(1, weight=1)
        # Apply the bevel edge colors immediately: the edge frames are created
        # bare, and Tk's default frame background is the platform light gray --
        # without this every static frame (groups, fields, status) renders with
        # bright stripes until something happens to call set_kind.
        self.set_kind(kind)
        # A fixed width/height freezes the outer geometry: the widget requests
        # exactly that size and never re-sizes when content text changes (a
        # busy button label must not push its neighbours). Auto-sized widgets
        # keep letting their children drive the request.
        if width is not None or height is not None:
            self.grid_propagate(False)
        else:
            self.grid_propagate(True)

    def set_kind(self, kind: str, face: str | None = None) -> None:
        self.kind = kind
        if face is not None:
            self.face = face
            self.content.config(bg=self.face)
        edges = _RAISED_EDGES if kind == "raised" else _SUNKEN_EDGES
        for name, frame in self._edges.items():
            frame.config(bg=edges[name])

    def body(self):
        """The padded interior where a screen packs its controls."""
        return self.content


class RaisedButton(BevelFrame):
    """A verb button: raised, 2px, pressed = sunken + 1px shift, no motion.

    Disabled buttons stay raised with their label in `textMuted`; the caller
    is responsible for stating the reason in text next to the button.
    """

    def __init__(self, master, text, command=None, font=theme.BODY_FONT, enabled=True,
                 width=None, height=MIN_BUTTON_H):
        super().__init__(master, kind="raised", face=SURFACE_RAISED, width=width, height=height)
        self._command = command
        self._enabled = enabled
        self._pressed = False
        self._label = tk.Label(
            self.content,
            text=text,
            bg=SURFACE_RAISED,
            fg=TEXT_PRIMARY if enabled else TEXT_MUTED,
            font=font,
            bd=0,
            padx=8,
            pady=3,
        )
        self._label.pack(fill="both", expand=True, padx=(0, 1), pady=(0, 1))
        self.config(takefocus=1)
        for widget in (self, self.content, self._label):
            widget.bind("<ButtonPress-1>", self._on_press)
            widget.bind("<ButtonRelease-1>", self._on_release)
            widget.bind("<Leave>", self._on_leave)
        self.bind("<Return>", lambda _e: self.activate())
        self.bind("<space>", lambda _e: self.activate())
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)

    def set_text(self, text: str) -> None:
        self._label.config(text=text)

    def get_text(self) -> str:
        return self._label.cget("text")

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._label.config(fg=TEXT_PRIMARY if enabled else TEXT_MUTED)
        if not enabled:
            self._unpress()

    def is_enabled(self) -> bool:
        return self._enabled

    def activate(self) -> None:
        if not self._enabled:
            return
        self._pressed = True
        self._apply_press()
        self.after(1, self._finish_activate)

    def _finish_activate(self) -> None:
        if self._pressed:
            self._pressed = False
            self._apply_press()
        if self._enabled and self._command is not None:
            self._command()

    def _on_press(self, _event=None) -> None:
        if not self._enabled:
            return
        self._pressed = True
        self._apply_press()

    def _on_release(self, _event=None) -> None:
        if not self._pressed:
            return
        self._pressed = False
        self._apply_press()
        if self._enabled and self._command is not None:
            self._command()

    def _on_leave(self, _event=None) -> None:
        self._unpress()

    def _unpress(self) -> None:
        if self._pressed:
            self._pressed = False
            self._apply_press()

    def _apply_press(self) -> None:
        # The 1px shift lives entirely inside the button: total padding stays
        # 1px on every side in both states, so the outer requested geometry
        # never changes and no neighbour moves. Pressed = label shifts 1px
        # down-right into the reserved gap.
        if self._pressed:
            self.set_kind("sunken", face=theme.PALETTE["surface"])
            self._label.config(bg=theme.PALETTE["surface"])
            self._label.pack_configure(padx=(1, 0), pady=(1, 0))
        else:
            self.set_kind("raised", face=SURFACE_RAISED)
            self._label.config(bg=SURFACE_RAISED)
            self._label.pack_configure(padx=(0, 1), pady=(0, 1))

    def _on_focus_in(self, _event=None) -> None:
        self.config(highlightbackground=theme.PALETTE["textPrimary"])

    def _on_focus_out(self, _event=None) -> None:
        self.config(highlightbackground=theme.PALETTE["background"])


class SunkenEntry(BevelFrame):
    """A sunken single-line input. Always paired with a visible label by the
    caller; the field itself never relies on placeholder text."""

    def __init__(self, master, textvariable=None, width=None, show=None,
                 font=theme.BODY_FONT, height=MIN_BUTTON_H):
        super().__init__(master, kind="sunken", face=SURFACE, height=height)
        self.var = textvariable if textvariable is not None else tk.StringVar()
        self.entry = tk.Entry(
            self.content,
            textvariable=self.var,
            bg=COMPARE_BACK,
            fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            font=font,
            relief=tk.FLAT,
            bd=0,
            width=width,
            show=show,
            highlightthickness=1,
            highlightbackground=theme.PALETTE["surface"],
            highlightcolor=theme.PALETTE["textPrimary"],
        )
        self.entry.pack(fill="both", expand=True, padx=1, pady=1)
        self.entry.config(takefocus=1)

    def get(self) -> str:
        return self.var.get()

    def set(self, value) -> None:
        self.var.set("" if value is None else str(value))

    def focus(self):
        self.entry.focus_set()


class SunkenText(BevelFrame):
    """A sunken multi-line area, optionally read-only, optionally scrollable."""

    def __init__(self, master, height=4, width=28, wrap="word", readonly=False,
                 font=theme.BODY_FONT, scrollbar=True):
        super().__init__(master, kind="sunken", face=SURFACE)
        self._readonly = readonly
        self.text = tk.Text(
            self.content,
            bg=COMPARE_BACK,
            fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            font=font,
            relief=tk.FLAT,
            bd=0,
            wrap=wrap,
            height=height,
            width=width,
            highlightthickness=1,
            highlightbackground=theme.PALETTE["surface"],
            highlightcolor=theme.PALETTE["textPrimary"],
            undo=False,
        )
        self.text.pack(side="left", fill="both", expand=True)
        self.text.config(takefocus=1)
        self._scrollbar = None
        if scrollbar:
            self._scrollbar = GoldenScrollbar(self.content, command=self.text.yview)
            self._scrollbar.pack(side="right", fill="y")
            self.text.config(yscrollcommand=self._scrollbar.set)
        if readonly:
            self.text.config(state="disabled")

    def set_text(self, value: str) -> None:
        self.text.config(state="normal")
        self.text.replace("1.0", "end", value)
        self.text.see("1.0")
        if self._readonly:
            self.text.config(state="disabled")

    def get_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    def clear(self) -> None:
        self.set_text("")


class ScrollableFrame(tk.Frame):
    """A vertically scrollable content area.

    For screens whose content is genuinely taller than the viewport (Settings
    at 640x480). The canvas is the fixed viewport; `inner` is the scrolling
    surface. This is deliberate in-view scrolling, never a root window turned
    into one long page.
    """

    def __init__(self, master, bg=theme.PALETTE["background"]):
        super().__init__(master, bg=bg)
        self._scrollbar = GoldenScrollbar(self, command=self._yview)
        self._scrollbar.pack(side="right", fill="y")
        self.canvas = tk.Canvas(
            self,
            bg=bg,
            bd=0,
            highlightthickness=0,
            height=1,
            takefocus=0,
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.config(yscrollcommand=self._scrollbar.set)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._inner_window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self._sync_region())
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._inner_window, width=e.width))
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    def _yview(self, *args):
        self.canvas.yview(*args)

    def _sync_region(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class GoldenScrollbar(tk.Canvas):
    """A sunken track with a raised thumb, drawn from tokens like everything
    else. Speaks the Tk scrollbar protocol: `set(first, last)` in, `command`
    out.

    Geometry contract: the canvas requests only its narrow width and a 1px
    height, so it never inflates a parent's requested size -- the parent's
    layout decides vertical expansion, not the scrollbar. `width` stays
    constant across every state. Drag state is cleared on ButtonRelease.
    """

    def __init__(self, master, command=None, width=14):
        super().__init__(
            master,
            width=width,
            height=1,
            bg=theme.PALETTE["surface"],
            bd=0,
            highlightthickness=0,
        )
        self._command = command
        self._first = 0.0
        self._last = 1.0
        self._drag_start = None
        self._drag_first = 0.0
        self._thumb_tag = "thumb"
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _e: self._redraw())

    def set(self, first: float, last: float) -> None:
        first = max(0.0, min(1.0, float(first)))
        last = max(first + 0.01, min(1.0, float(last)))
        self._first, self._last = first, last
        self._redraw()

    def _track_rect(self):
        w = self.winfo_width()
        h = self.winfo_height()
        return w, h

    def _thumb_rect(self):
        w, h = self._track_rect()
        span = self._last - self._first
        top = self._first * h
        bottom = self._last * h if span >= 1.0 else min(h, top + max(24.0, span * h))
        return BEVEL, top, w - BEVEL, bottom

    def _redraw(self) -> None:
        _clear_canvas(self)
        w, h = self._track_rect()
        if w <= 0 or h <= 0:
            return
        # Sunken track bevel.
        self.create_rectangle(0, 0, w, BEVEL, fill=theme.PALETTE["borderDark"], outline="")
        self.create_rectangle(0, 0, BEVEL, h, fill=theme.PALETTE["borderDark"], outline="")
        self.create_rectangle(0, h - BEVEL, w, h, fill=theme.PALETTE["bevelLight"], outline="")
        self.create_rectangle(w - BEVEL, 0, w, h, fill=theme.PALETTE["bevelLight"], outline="")
        # Raised thumb.
        x0, y0, x1, y1 = self._thumb_rect()
        if y1 - y0 < 4:
            return
        self.create_rectangle(x0, y0, x1, y1, fill=theme.PALETTE["surfaceRaised"], outline="", tags=self._thumb_tag)
        self.create_rectangle(x0, y0, x1, y0 + BEVEL, fill=theme.PALETTE["bevelLight"], outline="")
        self.create_rectangle(x0, y0, x0 + BEVEL, y1, fill=theme.PALETTE["bevelLight"], outline="")
        self.create_rectangle(x0, y1 - BEVEL, x1, y1, fill=theme.PALETTE["borderDark"], outline="")
        self.create_rectangle(x1 - BEVEL, y0, x1, y1, fill=theme.PALETTE["borderDark"], outline="")

    def _on_press(self, event) -> None:
        h = self.winfo_height()
        if h <= 0:
            return
        x0, y0, x1, y1 = self._thumb_rect()
        if y0 <= event.y <= y1:
            self._drag_start = event.y
            self._drag_first = self._first
            return
        span = self._last - self._first
        page = max(span, 0.1)
        if event.y < y0:
            target = max(0.0, self._first - page)
        else:
            target = min(1.0, self._first + page)
        if self._command is not None:
            self._command("moveto", target)

    def _on_drag(self, event) -> None:
        if self._drag_start is None:
            return
        h = self.winfo_height()
        if h <= 0:
            return
        dy = event.y - self._drag_start
        target = self._drag_first + (dy / h)
        target = max(0.0, min(1.0, target))
        if self._command is not None:
            self._command("moveto", target)

    def _on_release(self, _event=None) -> None:
        """Terminate the drag cleanly: clear all drag state so the next press
        starts from a known position."""
        self._drag_start = None
        self._drag_first = 0.0


class GoldenListBase(BevelFrame):
    """A compact, keyboard-navigable list.

    Rows are 16-18px. Selection is drawn as the selection token fill plus a
    gold left bar (so it never depends on colour alone); keyboard focus is a
    dotted outline on the active row, visually distinct from selection.

    Subclasses implement `_row_content` for the per-row text. `set_rows`
    takes a list of dicts. A row's selection key is its canonical identity:
    the `(source, id)` pair when the row carries a `source`, otherwise the
    bare `id` (CORE-012 -- two sources may share an id; the GUI must never
    collapse or act on the wrong one).
    """

    def __init__(self, master, height=5, on_select=None, font=LIST_FONT):
        super().__init__(master, kind="sunken", face=SURFACE)
        self._on_select = on_select
        self._font = font
        self._rows: list[dict] = []
        self._id_index: dict = {}
        self._selected_id = None  # a row key: tuple for compound identities
        self._active = 0
        self._focused = False

        self._canvas = tk.Canvas(
            self.content,
            bg=COMPARE_BACK,
            bd=0,
            highlightthickness=0,
            height=height * theme.ROW_HEIGHT,
            takefocus=1,
        )
        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar = GoldenScrollbar(self.content, command=self._on_scroll)
        self._scrollbar.pack(side="right", fill="y")
        self._canvas.config(yscrollcommand=self._scrollbar.set)

        self._canvas.bind("<Button-1>", self._on_click)
        self._canvas.bind("<MouseWheel>", self._on_wheel)
        self._canvas.bind("<FocusIn>", self._on_focus_in)
        self._canvas.bind("<FocusOut>", self._on_focus_out)
        self._canvas.bind("<Configure>", lambda _e: self._redraw_visible())
        for key in ("<Up>", "<Down>", "<Home>", "<End>", "<Prior>", "<Next>"):
            self._canvas.bind(key, self._on_key)
        self.config(takefocus=1)

    # -- data ----------------------------------------------------------

    @staticmethod
    def _row_key(row) -> object:
        """The canonical selection key for one row (CORE-012)."""
        if "source" in row:
            return (row.get("source"), row.get("id"))
        return row.get("id")

    def set_rows(self, rows: list[dict]) -> None:
        """`rows` are item views in display order (highest priority first)."""
        self._rows = list(rows)
        self._id_index = {
            self._row_key(row): index for index, row in enumerate(self._rows)
        }
        if self._selected_id is not None and self._selected_id not in self._id_index:
            self._selected_id = None
        if self._rows:
            self._active = min(self._active, len(self._rows) - 1)
        else:
            self._active = 0
        self._canvas.yview_moveto(0.0)
        self._redraw_visible()

    def selected_id(self) -> str | None:
        return self._selected_id

    def select(self, item_id: str) -> None:
        index = self._id_index.get(item_id)
        if index is not None:
            self._selected_id = item_id
            self._active = index
            self._redraw_visible()
            self._see_active()
            return
        self._selected_id = None
        self._redraw_visible()

    def count(self) -> int:
        return len(self._rows)

    def clear(self) -> None:
        self.set_rows([])

    # -- drawing -------------------------------------------------------

    def _draw_content(self, canvas, index, row, w, y) -> None:
        raise NotImplementedError

    def _visible_range(self):
        """The first/last row index needing canvas items, plus overscan."""
        total = len(self._rows) * theme.ROW_HEIGHT
        if total <= 0:
            return 0, -1
        top = max(0.0, self._canvas.canvasy(0))
        height = max(1, self._canvas.winfo_height())
        bottom = top + height
        first = max(0, int(top // theme.ROW_HEIGHT) - 1)
        last = min(len(self._rows) - 1, int(bottom // theme.ROW_HEIGHT) + 1)
        return first, last

    def _redraw_visible(self) -> None:
        """Draw only the rows in the visible viewport (PERF-005): scrolling,
        focus and single-row selection never rebuild the whole canvas."""
        _clear_canvas(self._canvas)
        w = self._canvas.winfo_width()
        if w <= 0:
            w = 400
        total = len(self._rows) * theme.ROW_HEIGHT
        self._canvas.configure(scrollregion=(0, 0, w, max(total, 1)))
        first, last = self._visible_range()
        for index in range(first, last + 1):
            row = self._rows[index]
            y = index * theme.ROW_HEIGHT
            if self._row_key(row) == self._selected_id:
                self._canvas.create_rectangle(
                    0, y, w, y + theme.ROW_HEIGHT, fill=theme.PALETTE["selection"], outline=""
                )
                self._canvas.create_rectangle(
                    0, y, 3, y + theme.ROW_HEIGHT, fill=LINK, outline=""
                )
            else:
                self._canvas.create_rectangle(
                    0, y, w, y + theme.ROW_HEIGHT, fill=COMPARE_BACK, outline=""
                )
            self._draw_content(self._canvas, index, row, w, y)
            if self._focused and index == self._active:
                self._canvas.create_rectangle(
                    2,
                    y + 1,
                    w - 2,
                    y + theme.ROW_HEIGHT - 1,
                    outline=TEXT_PRIMARY,
                    dash=(1, 1),
                )

    # -- viewport ------------------------------------------------------

    def _see_active(self) -> None:
        """Move the viewport just enough to reveal the active row. Never
        resets to the top; a scroll position the user chose stays put."""
        if not self._rows:
            return
        total = len(self._rows) * theme.ROW_HEIGHT
        top = self._active * theme.ROW_HEIGHT
        bottom = top + theme.ROW_HEIGHT
        ctop = self._canvas.canvasy(0)
        cheight = self._canvas.winfo_height()
        if cheight <= 0:
            return
        cbottom = ctop + cheight
        moved = False
        if top < ctop:
            frac = max(0.0, top / max(total, 1))
            self._canvas.yview_moveto(frac)
            moved = True
        elif bottom > cbottom:
            frac = min(1.0, (bottom - cheight) / max(total, 1))
            self._canvas.yview_moveto(frac)
            moved = True
        if moved:
            self._redraw_visible()

    def _on_scroll(self, *args) -> None:
        self._canvas.yview(*args)
        self._redraw_visible()

    # -- interaction ---------------------------------------------------

    def _row_at(self, event) -> int | None:
        y = self._canvas.canvasy(event.y)
        index = int(y // theme.ROW_HEIGHT)
        if 0 <= index < len(self._rows):
            return index
        return None

    def _on_click(self, event) -> None:
        self._canvas.focus_set()
        index = self._row_at(event)
        if index is None:
            return
        self._active = index
        self._selected_id = self._row_key(self._rows[index])
        self._redraw_visible()
        if self._on_select is not None:
            self._on_select(self._selected_id)

    def _on_wheel(self, event) -> None:
        self._canvas.yview_scroll(int(-event.delta / 120), "units")
        self._redraw_visible()

    def _on_focus_in(self, _event=None) -> None:
        self._focused = True
        self._redraw_visible()

    def _on_focus_out(self, _event=None) -> None:
        self._focused = False
        self._redraw_visible()

    def _on_key(self, event) -> None:
        key = event.keysym
        if not self._rows:
            return "break"
        if key == "Up":
            self._active = max(0, self._active - 1)
        elif key == "Down":
            self._active = min(len(self._rows) - 1, self._active + 1)
        elif key == "Home":
            self._active = 0
        elif key == "End":
            self._active = len(self._rows) - 1
        elif key == "Prior":  # PageUp
            self._active = max(0, self._active - 5)
        elif key == "Next":  # PageDown
            self._active = min(len(self._rows) - 1, self._active + 5)
        else:
            return None
        self._selected_id = self._row_key(self._rows[self._active])
        self._redraw_visible()
        self._see_active()
        if self._on_select is not None:
            self._on_select(self._selected_id)
        return "break"


class FindingList(GoldenListBase):
    """A list of scout findings: score | band subreddit title."""

    def _draw_content(self, canvas, index, row, w, y) -> None:
        score = float(row.get("score", 0.0))
        canvas.create_text(
            34, y + 2, anchor="ne", text=f"{score:.0f}", font=self._font, fill=TEXT_PRIMARY
        )
        band = str(row.get("band", "?"))
        subreddit = str(row.get("subreddit", "") or "?")
        title = str(row.get("title", ""))
        label = f"{band:<8} r/{subreddit}  {title}"
        canvas.create_text(40, y + 2, anchor="nw", text=label, font=self._font, fill=TEXT_PRIMARY)


class SimpleList(GoldenListBase):
    """A plain one-line-per-row list, for report names and the like."""

    def _draw_content(self, canvas, index, row, w, y) -> None:
        text = str(row.get("text", ""))
        canvas.create_text(6, y + 2, anchor="nw", text=text, font=self._font, fill=TEXT_PRIMARY)


class SectionHeader(tk.Label):
    def __init__(self, master, text):
        super().__init__(
            master,
            text=text,
            font=SECTION_FONT,
            fg=TEXT_PRIMARY,
            bg=theme.PALETTE["background"],
            anchor="w",
        )


class GoldenGroup(tk.Frame):
    """A titled section: a small label over a raised 2px panel."""

    def __init__(self, master, title, padding=theme.GROUP_PAD):
        super().__init__(master, bg=theme.PALETTE["background"])
        self._title = tk.Label(
            self,
            text=title,
            font=META_FONT,
            fg=TEXT_SECONDARY,
            bg=theme.PALETTE["background"],
            anchor="w",
        )
        self._title.pack(fill="x", anchor="w", padx=2, pady=(0, 2))
        self.panel = BevelFrame(self, kind="raised", face=SURFACE_RAISED)
        self.panel.pack(fill="both", expand=True)
        self.body = tk.Frame(self.panel.content, bg=SURFACE_RAISED, bd=0)
        self.body.pack(fill="both", expand=True, padx=padding, pady=padding)

    def set_title(self, title: str) -> None:
        self._title.config(text=title)


class LabeledField(tk.Frame):
    """A visible label over one field. Labels are mandatory; a field never
    relies on placeholder text.

    The field widget is created here, with the LabeledField as its real Tk
    parent -- a Tk widget cannot be reparented, so a caller-supplied widget
    built on another parent would render in the wrong place. Callers pass a
    factory instead:

        LabeledField(parent, label="Limit", widget_factory=SunkenEntry,
                     widget_kwargs={"width": 8})
    """

    def __init__(self, master, label, widget_factory, widget_kwargs=None,
                 font=theme.BODY_FONT):
        super().__init__(master, bg=theme.PALETTE["background"])
        self.label = tk.Label(
            self,
            text=label,
            font=font,
            fg=TEXT_PRIMARY,
            bg=theme.PALETTE["background"],
            anchor="w",
        )
        self.label.pack(fill="x", anchor="w")
        self.field = widget_factory(self, **(widget_kwargs or {}))
        self.field.pack(fill="x")

    def get(self):
        return self.field.get()

    def set(self, value) -> None:
        self.field.set(value)


class GoldenCheck(tk.Checkbutton):
    """A square, token-coloured checkbox in classic tk only"""

    def __init__(self, master, text, variable=None, command=None):
        self.var = variable if variable is not None else tk.BooleanVar(value=True)
        super().__init__(
            master,
            text=text,
            variable=self.var,
            command=command,
            bg=theme.PALETTE["background"],
            fg=TEXT_PRIMARY,
            activebackground=theme.PALETTE["background"],
            activeforeground=TEXT_PRIMARY,
            selectcolor=theme.PALETTE["surface"],
            font=theme.BODY_FONT,
            highlightthickness=0,
            anchor="w",
            padx=0,
            pady=0,
        )

    def is_checked(self) -> bool:
        return bool(self.var.get())


class StatusRegion(BevelFrame):
    """The one shared message area: persistent, never auto-dismissed.

    Messages stay until the user clears them or an explicit later action
    replaces them. `lines` fixes the height up front so late text never
    reflows anything above it.
    """

    _KIND_COLOUR = {
        "info": TEXT_PRIMARY,
        "warn": TEXT_SECONDARY,
        "error": theme.PALETTE["dangerText"],
        "success": TEXT_PRIMARY,
    }

    def __init__(self, master, lines=3):
        super().__init__(master, kind="sunken", face=SURFACE)
        holder = tk.Frame(self.content, bg=SURFACE, bd=0)
        holder.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        self._text = tk.Text(
            holder,
            bg=COMPARE_BACK,
            fg=TEXT_PRIMARY,
            font=theme.BODY_FONT,
            relief=tk.FLAT,
            bd=0,
            wrap="word",
            height=lines,
            highlightthickness=1,
            highlightbackground=theme.PALETTE["surface"],
            highlightcolor=theme.PALETTE["textPrimary"],
            state="disabled",
        )
        self._text.pack(side="left", fill="both", expand=True)
        self._scrollbar = GoldenScrollbar(self.content, command=self._text.yview)
        self._scrollbar.pack(side="right", fill="y")
        self._text.config(yscrollcommand=self._scrollbar.set)
        self._clear_button = RaisedButton(
            self.content, "Clear", command=self.clear, width=64, height=24, font=theme.META_FONT
        )
        self._clear_button.pack(side="right", fill="y", padx=(1, 1), pady=1)

    def set_message(self, text: str, kind="info") -> None:
        self._text.config(state="normal")
        self._text.replace("1.0", "end", text)
        self._text.config(fg=self._KIND_COLOUR.get(kind, TEXT_PRIMARY))
        self._text.config(state="disabled")
        self._text.see("1.0")

    def get_message(self) -> str:
        return self._text.get("1.0", "end-1c")

    def clear(self) -> None:
        self.set_message("", "info")


class TabStrip(tk.Frame):
    """Compact task tabs: inactive raised, active sunken, touching, no pills.

    Keyboard: arrows move between tabs, Return/Space activates the focused
    one, focus shows as the textPrimary ring.
    """

    def __init__(self, master, tabs, on_change):
        super().__init__(master, bg=theme.PALETTE["background"])
        self._tabs = list(tabs)
        self._on_change = on_change
        self._buttons: dict[str, _Tab] = {}
        for name in self._tabs:
            tab = _Tab(self, name, self._activate)
            tab.pack(side="left", fill="y", padx=0, pady=0)
            self._buttons[name] = tab

    def _activate(self, name: str) -> None:
        if name not in self._buttons:
            return
        self.set_active(name)
        if self._on_change is not None:
            self._on_change(name)

    def set_active(self, name: str) -> None:
        for tab_name, tab in self._buttons.items():
            tab.set_active(tab_name == name)


class _Tab(BevelFrame):
    """One tab button. Active = sunken + surface face; inactive = raised."""

    def __init__(self, master, name, on_activate):
        super().__init__(master, kind="raised", face=SURFACE_RAISED, height=22)
        self._name = name
        self._on_activate = on_activate
        self._label = tk.Label(
            self.content,
            text=name,
            bg=SURFACE_RAISED,
            fg=TEXT_SECONDARY,
            font=theme.BODY_FONT,
            bd=0,
            padx=10,
            pady=3,
        )
        self._label.pack(fill="both", expand=True)
        self.config(takefocus=1)
        for widget in (self, self.content, self._label):
            widget.bind("<Button-1>", self._clicked)
        self.bind("<Return>", lambda _e: self._activate())
        self.bind("<space>", lambda _e: self._activate())
        self.bind("<Left>", lambda _e: self._move(-1))
        self.bind("<Right>", lambda _e: self._move(1))
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.set_active(False)

    def _clicked(self, _event=None) -> None:
        self._activate()

    def _activate(self) -> None:
        self._on_activate(self._name)

    def _move(self, direction: int) -> None:
        tabs = self.master._tabs
        idx = tabs.index(self._name)
        nxt = tabs[(idx + direction) % len(tabs)]
        self.master._buttons[nxt].focus_set()

    def set_active(self, active: bool) -> None:
        if active:
            self.set_kind("sunken", face=SURFACE)
            self._label.config(bg=SURFACE, fg=TEXT_PRIMARY)
        else:
            self.set_kind("raised", face=SURFACE_RAISED)
            self._label.config(bg=SURFACE_RAISED, fg=TEXT_SECONDARY)

    def _on_focus_in(self, _event=None) -> None:
        self.config(highlightbackground=theme.PALETTE["textPrimary"])

    def _on_focus_out(self, _event=None) -> None:
        self.config(highlightbackground=theme.PALETTE["background"])


class ErrorBox(BevelFrame):
    """A reserved error/detail region with cause/symptom/fix formatting."""

    def __init__(self, master, lines=5):
        super().__init__(master, kind="sunken", face=SURFACE)
        self._text = SunkenText(self, height=lines, readonly=True)
        self._text.pack(fill="both", expand=True)

    def show(self, text: str) -> None:
        self._text.set_text(text)

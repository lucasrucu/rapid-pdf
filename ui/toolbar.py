from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton,
    QLabel, QColorDialog, QFrame, QComboBox, QToolButton, QMenu,
    QWidgetAction, QSizePolicy,
)
from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtGui import QColor, QPixmap, QIcon, QPainter, QPen, QIntValidator

from ui.theme import themed_icon


PRESETS = [
    QColor(255, 255, 0),    # Yellow
    QColor(0, 230, 0),      # Green
    QColor(0, 200, 255),    # Cyan
    QColor(255, 130, 0),    # Orange
    QColor(255, 80, 80),    # Red
    QColor(180, 90, 255),   # Purple
    QColor(255, 255, 255),  # White
    QColor(0, 0, 0),        # Black
]

# qtawesome glyph ids for each tool (graceful empty-icon fallback if not installed).
_TOOL_ICONS = {
    "select": "mdi6.cursor-default-outline",
    "rect":   "mdi6.rectangle-outline",
    "line":   "mdi6.vector-line",
    "text":   "mdi6.format-text",
}


def _swatch_pixmap(color: QColor, size: int = 16, none: bool = False,
                   border: str = "#55555a") -> QPixmap:
    """A small rounded chip used as the face icon of a color dropdown."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if none:
        # White chip with a red diagonal = "no color".
        p.setBrush(QColor(255, 255, 255))
        p.setPen(QPen(QColor(border)))
        p.drawRoundedRect(0, 0, size - 1, size - 1, 3, 3)
        p.setPen(QPen(QColor(0xd0, 0x40, 0x40), 2))
        p.drawLine(2, size - 3, size - 3, 2)
    else:
        p.setBrush(QColor(color))
        p.setPen(QPen(QColor(border)))
        p.drawRoundedRect(0, 0, size - 1, size - 1, 3, 3)
    p.end()
    return pm


class ColorToolButton(QToolButton):
    """An Office-style dropdown: a labeled button whose menu offers a color grid,
    a recent-colors row, an optional 'None' choice, and (for lines) stroke widths."""

    color_changed = Signal(QColor)
    cleared = Signal()              # the 'None' choice (fill removed)
    width_changed = Signal(float)

    def __init__(self, label: str, *, allow_none: bool = False, none_label: str = "None",
                 show_width: bool = False, recents_getter=None, parent=None):
        super().__init__(parent)
        self._label = label
        self._allow_none = allow_none
        self._none_label = none_label
        self._show_width = show_width
        self._recents_getter = recents_getter or (lambda: [])
        self._color = QColor(0, 0, 0)
        self._is_none = False
        self._width = 2.0
        self._chip_border = "#9a8f78"

        self.setText(f"  {label}")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._menu = QMenu(self)
        self._menu.aboutToShow.connect(self._rebuild_menu)
        self.setMenu(self._menu)
        self._refresh_face()

    # -- public display setters (no signals) ---------------------------------

    def set_current_color(self, color: QColor):
        self._color = QColor(color)
        self._is_none = False
        self._refresh_face()

    def set_none(self):
        self._is_none = True
        self._refresh_face()

    def set_width(self, w: float):
        self._width = float(w)

    def set_chip_border(self, color: str):
        self._chip_border = color
        self._refresh_face()

    def _refresh_face(self):
        self.setIcon(QIcon(_swatch_pixmap(
            self._color, 16, none=self._is_none, border=self._chip_border)))

    # -- menu ----------------------------------------------------------------

    def _rebuild_menu(self):
        m = self._menu
        m.clear()

        if self._allow_none:
            act = m.addAction(QIcon(_swatch_pixmap(QColor(255, 255, 255), 16, none=True)),
                              self._none_label)
            act.triggered.connect(self._on_none)
            m.addSeparator()

        grid_action = QWidgetAction(m)
        grid_action.setDefaultWidget(self._build_grid(PRESETS))
        m.addAction(grid_action)

        recents = list(self._recents_getter())
        if recents:
            lbl = QWidgetAction(m)
            lbl.setDefaultWidget(self._build_caption("Recent"))
            m.addAction(lbl)
            rec_action = QWidgetAction(m)
            rec_action.setDefaultWidget(self._build_grid(recents[:8]))
            m.addAction(rec_action)

        m.addSeparator()
        more = m.addAction("More colors…")
        more.triggered.connect(self._on_more)

        if self._show_width:
            m.addSeparator()
            cap = QWidgetAction(m)
            cap.setDefaultWidget(self._build_caption("Weight"))
            m.addAction(cap)
            w_action = QWidgetAction(m)
            w_action.setDefaultWidget(self._build_widths())
            m.addAction(w_action)

    def _build_caption(self, text: str) -> QWidget:
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#888; font-size:9px; font-weight:bold; padding:2px 8px;")
        return lbl

    def _build_grid(self, colors) -> QWidget:
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(6, 4, 6, 4)
        g.setSpacing(3)
        cols = 4
        for i, c in enumerate(colors):
            b = QPushButton()
            b.setFixedSize(22, 22)
            b.setToolTip(c.name())
            b.setStyleSheet(
                f"QPushButton{{background:{c.name()};border:1px solid #9a8f78;border-radius:4px;}}"
                f"QPushButton:hover{{border:2px solid #F1AE04;}}"
            )
            b.clicked.connect(lambda _, col=QColor(c): self._choose_color(col))
            g.addWidget(b, i // cols, i % cols)
        return w

    def _build_widths(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 2, 8, 6)
        v.setSpacing(3)
        for wd in (1, 2, 3, 5, 8):
            b = QPushButton(f"{wd} px")
            b.setFixedHeight(22)
            b.clicked.connect(lambda _, ww=float(wd): self._choose_width(ww))
            v.addWidget(b)
        return w

    def _choose_color(self, color: QColor):
        self.set_current_color(color)
        self._menu.close()
        self.color_changed.emit(QColor(color))

    def _choose_width(self, w: float):
        self._width = w
        self._is_none = False
        self._refresh_face()
        self._menu.close()
        self.width_changed.emit(float(w))

    def _on_none(self):
        self.set_none()
        if self._show_width:
            # "No line" on the stroke control = a borderless rectangle (width 0).
            self.width_changed.emit(0.0)
        else:
            self.cleared.emit()

    def _on_more(self):
        color = QColorDialog.getColor(self._color, self, f"Pick {self._label} Color")
        if color.isValid():
            self._choose_color(color)


class ToolBar(QWidget):
    tool_changed = Signal(str)
    line_color_changed = Signal(QColor)
    fill_color_changed = Signal(QColor)
    fill_cleared = Signal()
    line_width_changed = Signal(float)
    opacity_changed = Signal(float)
    font_size_changed = Signal(int)
    font_color_changed = Signal(QColor)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ToolBar")
        self._opacity = 0.5
        self._font_size = 12
        self._recents: list[QColor] = []
        self._tool_btns: dict[str, QPushButton] = {}
        self._current_tool = "select"
        self._selection_types: set[str] = set()
        # Icon tints for tool buttons; updated by apply_palette() on theme change.
        self._icon_color = "#2A2620"
        self._active_icon_color = "#2A2010"
        self._chip_border = "#D8BE72"
        self._setup_ui()
        self.setFixedWidth(180)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _section(self, *widgets, lead_divider: bool = False) -> QWidget:
        box = QWidget()
        vbox = QVBoxLayout(box)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(5)
        if lead_divider:
            vbox.addWidget(self._divider())
        for w in widgets:
            vbox.addWidget(w)
        return box

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 10, 6, 10)
        layout.setSpacing(5)

        # --- Tools (always) — icon-led, accent-on-active rail buttons ---
        layout.addWidget(self._section_label("TOOLS"))
        for tid, label in [
            ("select",    "Select  V"),
            ("rect",      "Rectangle  R"),
            ("line",      "Line  L"),
            ("text",      "Text  T"),
        ]:
            btn = QPushButton("  " + label)
            btn.setObjectName("tool")
            btn.setCheckable(True)
            btn.setFixedHeight(36)
            btn.setIcon(themed_icon(_TOOL_ICONS[tid], self._icon_color))
            btn.setIconSize(QSize(18, 18))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _, t=tid: self._on_tool(t))
            self._tool_btns[tid] = btn
            layout.addWidget(btn)
        self._tool_btns["select"].setChecked(True)

        # --- Appearance: Fill + Line dropdowns (contextual) ---
        self._appearance_label = self._section_label("APPEARANCE")
        layout.addWidget(self._divider())
        layout.addWidget(self._appearance_label)

        self._fill_btn = ColorToolButton(
            "Fill", allow_none=True, none_label="No Fill",
            recents_getter=lambda: self._recents,
        )
        self._fill_btn.setToolTip("Fill color for rectangles")
        self._fill_btn.set_none()
        self._fill_btn.color_changed.connect(self._on_fill_color)
        self._fill_btn.cleared.connect(self.fill_cleared)
        layout.addWidget(self._fill_btn)

        self._line_btn = ColorToolButton(
            "Line", allow_none=True, none_label="No Line", show_width=True,
            recents_getter=lambda: self._recents,
        )
        self._line_btn.setToolTip("Outline color and thickness for rectangles and lines")
        self._line_btn.set_current_color(PRESETS[0])
        self._line_btn.color_changed.connect(self._on_line_color)
        self._line_btn.width_changed.connect(self.line_width_changed)
        layout.addWidget(self._line_btn)

        # --- Text: font color + size (contextual) ---
        self._font_color_btn = ColorToolButton(
            "Text Color", recents_getter=lambda: self._recents,
        )
        self._font_color_btn.setToolTip("Color of text labels and text inside shapes")
        self._font_color_btn.set_current_color(QColor(0, 0, 0))
        self._font_color_btn.color_changed.connect(self._on_font_color)

        size_row = QWidget()
        size_layout = QHBoxLayout(size_row)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(4)
        size_layout.addWidget(QLabel("Size"))
        self._font_combo = QComboBox()
        self._font_combo.setEditable(True)
        self._font_combo.setValidator(QIntValidator(4, 144, self))
        self._font_combo.addItems(["8", "10", "12", "14", "18", "24", "36", "48", "72"])
        self._font_combo.setCurrentText("12")
        self._font_combo.setFixedHeight(26)
        self._font_combo.activated.connect(
            lambda _: self._on_font_size(self._font_combo.currentText()))
        self._font_combo.lineEdit().editingFinished.connect(
            lambda: self._on_font_size(self._font_combo.currentText()))
        size_layout.addWidget(self._font_combo, stretch=1)

        self._text_section = self._section(
            self._section_label("TEXT"), self._font_color_btn, size_row, lead_divider=True
        )
        layout.addWidget(self._text_section)

        # --- Opacity (always) — a preset dropdown, not a slider ---
        layout.addWidget(self._divider())
        layout.addWidget(self._section_label("OPACITY"))
        self._opacity_btn = QToolButton()
        self._opacity_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._opacity_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._opacity_btn.setFixedHeight(28)
        self._opacity_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        op_menu = QMenu(self._opacity_btn)
        for pct in (100, 75, 50, 25, 10):
            act = op_menu.addAction(f"{pct}%")
            act.triggered.connect(lambda _, p=pct: self._on_opacity(p))
        self._opacity_btn.setMenu(op_menu)
        self._set_opacity_display(self._opacity)
        layout.addWidget(self._opacity_btn)

        layout.addStretch()

        # --- Keyboard hints ---
        layout.addWidget(self._divider())
        hints = QLabel(
            "V/R/L/T — tools\n"
            "Del — delete\n"
            "Dbl-click — text\n"
            "Ctrl+drag — copy\n"
            "Shift+drag — straight\n"
            "Ctrl+scroll — zoom\n"
            "Shift+draw — constrain"
        )
        hints.setWordWrap(True)
        hints.setObjectName("section")
        hints.setStyleSheet("font-size: 9px; line-height: 1.4; letter-spacing: 0;")
        layout.addWidget(hints)

        self._update_context()

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("section")
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        return lbl

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        return line

    # ------------------------------------------------------------------
    # Recent colors
    # ------------------------------------------------------------------

    def _push_recent(self, color: QColor):
        name = color.name()
        self._recents = [c for c in self._recents if c.name() != name]
        self._recents.insert(0, QColor(color))
        del self._recents[8:]

    # ------------------------------------------------------------------
    # Signal slots
    # ------------------------------------------------------------------

    def _on_tool(self, tool: str):
        self._current_tool = tool
        for tid, btn in self._tool_btns.items():
            btn.setChecked(tid == tool)
        self._retint_tool_icons()
        self._update_context()
        self.tool_changed.emit(tool)

    def _retint_tool_icons(self):
        """The active tool's icon sits on a gold fill → tint it dark; the rest use
        the normal text color. (qtawesome glyphs recolor per state, but Qt won't
        swap a QIcon by QSS state, so we re-set them here.)"""
        for tid, btn in self._tool_btns.items():
            color = self._active_icon_color if btn.isChecked() else self._icon_color
            btn.setIcon(themed_icon(_TOOL_ICONS[tid], color))

    def apply_palette(self, palette):
        """Re-tint tool-button icons + color-chip borders for the new theme."""
        self._icon_color = palette.text
        self._active_icon_color = palette.accent_text
        self._chip_border = palette.border_strong
        self._retint_tool_icons()
        # Refresh the color dropdown faces so their chip borders match the theme.
        for btn in (getattr(self, "_fill_btn", None), getattr(self, "_line_btn", None),
                    getattr(self, "_font_color_btn", None)):
            if btn is not None:
                btn.set_chip_border(palette.border_strong)

    def _on_line_color(self, color: QColor):
        self._push_recent(color)
        self.line_color_changed.emit(color)

    def _on_fill_color(self, color: QColor):
        self._push_recent(color)
        self.fill_color_changed.emit(color)

    def _on_font_color(self, color: QColor):
        self._push_recent(color)
        self.font_color_changed.emit(color)

    def _on_font_size(self, text: str):
        try:
            v = int(text)
        except (ValueError, TypeError):
            return
        v = max(4, min(144, v))
        if v == self._font_size:
            self._font_combo.setCurrentText(str(v))
            return
        self._font_size = v
        self._font_combo.setCurrentText(str(v))
        self.font_size_changed.emit(v)

    def _on_opacity(self, pct: int):
        self._opacity = pct / 100.0
        self._opacity_btn.setText(f"Opacity: {pct}%")
        self.opacity_changed.emit(self._opacity)

    def _set_opacity_display(self, opacity: float):
        self._opacity = opacity
        self._opacity_btn.setText(f"Opacity: {int(round(opacity * 100))}%")

    # ------------------------------------------------------------------
    # Selection → panel sync (no signals fired)
    # ------------------------------------------------------------------

    def show_selection(self, summary: dict):
        """Reflect the selected object(s) in the controls without emitting signals."""
        self._selection_types = set(summary.get("types", set())) if summary else set()

        stroke = summary.get("stroke_color") if summary else None
        if stroke is not None:
            self._line_btn.set_current_color(stroke)

        line_width = summary.get("line_width") if summary else None
        if line_width is not None:
            self._line_btn.set_width(line_width)
            if line_width <= 0:
                # A 0-width border IS "No Line" — show that, not a color chip.
                self._line_btn.set_none()

        fill = summary.get("fill") if summary else None
        fill_color = summary.get("fill_color") if summary else None
        if fill is False:
            self._fill_btn.set_none()
        elif fill_color is not None:
            self._fill_btn.set_current_color(fill_color)

        opacity = summary.get("opacity") if summary else None
        if opacity is not None:
            self._set_opacity_display(opacity)

        font_size = summary.get("font_size") if summary else None
        if font_size is not None:
            self._font_size = int(font_size)
            self._font_combo.blockSignals(True)
            self._font_combo.setCurrentText(str(int(font_size)))
            self._font_combo.blockSignals(False)

        font_color = summary.get("font_color") if summary else None
        if font_color is not None:
            self._font_color_btn.set_current_color(font_color)

        self._update_context()

    def _update_context(self):
        """Show only the property controls relevant to the current tool/selection."""
        tool = self._current_tool
        types = self._selection_types
        text_ctx = (tool in ("text", "rect")
                    or bool(types & {"text", "rect", "highlight"}))
        line_ctx = (tool in ("rect", "line")
                    or bool(types & {"rect", "line", "highlight"}))
        fill_ctx = tool == "rect" or "rect" in types

        self._appearance_label.setVisible(fill_ctx or line_ctx)
        self._fill_btn.setVisible(fill_ctx)
        self._line_btn.setVisible(line_ctx)
        self._text_section.setVisible(text_ctx)

    def trigger_tool(self, tool: str):
        if tool in self._tool_btns:
            self._on_tool(tool)

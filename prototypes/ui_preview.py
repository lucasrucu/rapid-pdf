"""
rapid-pdf UI preview: a standalone proof-of-concept for the proposed restyle.

WHAT THIS IS
------------
A small, throwaway PySide6 window that shows rapid-pdf's actual toolbar controls
re-skinned in the RECOMMENDED direction: "Refined custom QSS + soft drop shadows
+ icon-led buttons" (the cheapest, lowest-risk path to a glassy, future-tech look
that keeps full control of the existing hand-rolled dark theme).

It does NOT touch the real app. Nothing here imports rapid-pdf code. It mirrors the
controls in ui/toolbar.py so Lucas can put the OLD look next to the NEW look and
pick a direction before any real-app change happens.

WHAT IT DEMONSTRATES (and how it maps to the real toolbar)
----------------------------------------------------------
Left column  = the CURRENT look (flat #2d2d2d buttons, hard 1px #444 borders,
               3px radius, flat #0078d4 checked state, the "Windows XP" feel).
Right column = the PROPOSED look. Same widgets, restyled:
  - Tool buttons (Select / Rectangle / Line / Text)  -> ui/toolbar.py _tool_btns
      Icon + label, taller hit target, soft vertical gradient, 8px radius,
      a glowing accent bar + drop shadow on the CHECKED (active) tool.
  - Section labels (TOOLS / APPEARANCE)              -> ui/toolbar.py _section_label
  - Fill / Line color dropdowns with swatch chips    -> ui/toolbar.py ColorToolButton
  - A small color-swatch grid                        -> ColorToolButton._build_grid
  - A panel card with a drop shadow                  -> the toolbar / page panel frame

GLASSY NOTE
-----------
The real "Windows 11 Mica / acrylic" backdrop needs a tiny extra library
(pywinstyles or win32mica) + a frameless window. This prototype keeps to
pure PySide6 so it always runs. To preview the true Mica blur, install
pywinstyles and set MICA = True below; the code will try to apply it and
fall back silently if the lib or Windows version doesn't support it.
See docs/ui.md for the full options report.

RUN
---
    .venv\\Scripts\\python.exe prototypes\\ui_preview.py
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QGraphicsDropShadowEffect, QSizePolicy,
)

# Flip to True AND `pip install pywinstyles` to preview real Win11 Mica/acrylic.
MICA = False

# ---------------------------------------------------------------------------
# Palette: extends the app's existing tokens with an accent + surface ramp.
# ---------------------------------------------------------------------------
BG          = "#16181d"   # window base (slightly cooler than the current #1a1a1a)
SURFACE     = "#1f2228"   # panel / toolbar surface
SURFACE_HI  = "#262a32"   # raised control top
SURFACE_LO  = "#1c1f25"   # raised control bottom (gradient end)
BORDER      = "#33384a"   # soft border, lower contrast than the old #444
TEXT        = "#e6e8ee"
TEXT_DIM    = "#8b90a0"
ACCENT      = "#3d8bff"   # clearer, cooler blue than #0078d4
ACCENT_HI   = "#5fa0ff"
ACCENT_DEEP = "#2563d6"

PRESETS = [
    "#ffff00", "#00e600", "#00c8ff", "#ff8200",
    "#ff5050", "#b45aff", "#ffffff", "#000000",
]


# ---------------------------------------------------------------------------
# Icons: drawn with QPainter so the prototype needs no icon files / fonts.
# In the real app these would be qtawesome / Lucide SVGs (see docs/ui.md).
# ---------------------------------------------------------------------------
def _icon(kind: str, color: str = TEXT, size: int = 18) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    m = 3
    if kind == "select":  # cursor arrow
        pts = [(m, m), (m, size - m - 2), (size * 0.42, size * 0.62),
               (size * 0.6, size - m), (size * 0.72, size * 0.86),
               (size * 0.5, size * 0.5), (size - m - 1, size * 0.5)]
        p.setBrush(QColor(color))
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
    elif kind == "rect":
        p.drawRoundedRect(m, m + 1, size - 2 * m, size - 2 * m - 2, 2, 2)
    elif kind == "line":
        p.drawLine(m, size - m, size - m, m)
    elif kind == "text":
        p.drawLine(m, m + 1, size - m, m + 1)          # top bar of a T
        p.drawLine(size / 2, m + 1, size / 2, size - m)  # stem
    p.end()
    return QIcon(pm)


def _swatch(color: str, size: int = 16) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(QColor(color))
    p.setPen(QPen(QColor(0x55, 0x5a, 0x6a)))
    p.drawRoundedRect(0, 0, size - 1, size - 1, 3, 3)
    p.end()
    return pm


def _shadow(widget: QWidget, blur: int = 22, alpha: int = 150, dy: int = 4,
            color: QColor | None = None) -> None:
    """Soft drop shadow: the one thing flat QSS can't express on its own."""
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(color or QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)


# ---------------------------------------------------------------------------
# Stylesheets
# ---------------------------------------------------------------------------
OLD_QSS = """
QWidget#oldPanel { background-color: #1e1e1e; border-left: 1px solid #333; }
QLabel#section { color:#888; font-size:9px; font-weight:bold; letter-spacing:1px; }
QPushButton {
    background-color:#2d2d2d; border:1px solid #444; border-radius:3px;
    color:#d4d4d4; padding:3px 6px; text-align:left;
}
QPushButton:hover  { background-color:#3a3a3a; border-color:#666; color:#fff; }
QPushButton:checked{ background-color:#0078d4; border-color:#005fa8; color:#fff;
                     font-weight:bold; }
QLabel { color:#aaa; background:transparent; }
"""

# NEW look. Qt QSS has NO transitions/animations, so hover is an instant state
# swap; depth comes from the gradient + the QGraphicsDropShadowEffect applied in
# code, not from CSS shadows (which QSS doesn't support on QPushButton).
NEW_QSS = f"""
QWidget#newPanel {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QLabel#section {{
    color: {TEXT_DIM}; font-size: 9px; font-weight: 700;
    letter-spacing: 2px; padding-left: 2px;
}}
QLabel {{ color: {TEXT}; background: transparent; }}

QPushButton#tool {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {SURFACE_HI}, stop:1 {SURFACE_LO});
    border: 1px solid {BORDER};
    border-radius: 8px;
    color: {TEXT};
    padding: 7px 10px;
    text-align: left;
    font-size: 12px;
}}
QPushButton#tool:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2c313b, stop:1 #232730);
    border: 1px solid #44506e;
    color: #ffffff;
}}
QPushButton#tool:checked {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {ACCENT}, stop:1 {ACCENT_DEEP});
    border: 1px solid {ACCENT_HI};
    color: #ffffff;
    font-weight: 600;
}}

QPushButton#dropdown {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {SURFACE_HI}, stop:1 {SURFACE_LO});
    border: 1px solid {BORDER};
    border-radius: 8px;
    color: {TEXT};
    padding: 7px 10px;
    text-align: left;
    font-size: 12px;
}}
QPushButton#dropdown:hover {{ border: 1px solid #44506e; color:#fff; }}
"""


class ToolButton(QPushButton):
    """Mirrors a rapid-pdf tool button: icon + label, checkable, with an accent
    glow + drop shadow on the active state (set up once, toggled cheaply)."""

    def __init__(self, label: str, kind: str):
        super().__init__("  " + label)
        self.setObjectName("tool")
        self.setCheckable(True)
        self.setIcon(_icon(kind))
        self.setIconSize(QSize(18, 18))
        self.setMinimumHeight(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggled.connect(self._sync_glow)

    def _sync_glow(self, on: bool):
        # Re-tint the icon for the checked state and add a coloured glow.
        kind = {"  Select": "select", "  Rectangle": "rect",
                "  Line": "line", "  Text": "text"}.get(self.text(), "rect")
        self.setIcon(_icon(kind, "#ffffff" if on else TEXT))
        if on:
            _shadow(self, blur=20, dy=2, color=QColor(61, 139, 255, 150))
        else:
            self.setGraphicsEffect(None)


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("section")
    return lbl


def _dropdown(label: str, swatch_color: str) -> QPushButton:
    btn = QPushButton("  " + label)
    btn.setObjectName("dropdown")
    btn.setIcon(QIcon(_swatch(swatch_color)))
    btn.setIconSize(QSize(16, 16))
    btn.setMinimumHeight(34)
    btn.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def _swatch_grid() -> QWidget:
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(0, 2, 0, 2)
    g.setSpacing(6)
    for i, c in enumerate(PRESETS):
        b = QPushButton()
        b.setFixedSize(26, 26)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:{c};border:1px solid {BORDER};border-radius:6px;}}"
            f"QPushButton:hover{{border:2px solid {ACCENT_HI};}}"
        )
        g.addWidget(b, i // 4, i % 4)
    return w


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------
def _old_column() -> QWidget:
    panel = QWidget()
    panel.setObjectName("oldPanel")
    panel.setStyleSheet(OLD_QSS)
    panel.setFixedWidth(190)
    v = QVBoxLayout(panel)
    v.setContentsMargins(12, 14, 12, 14)
    v.setSpacing(6)

    v.addWidget(_section_label("TOOLS"))
    for lab in ("Select  V", "Rectangle  R", "Line  L", "Text  T"):
        b = QPushButton(lab)
        b.setCheckable(True)
        b.setMinimumHeight(28)
        if lab.startswith("Rectangle"):
            b.setChecked(True)
        v.addWidget(b)

    v.addSpacing(6)
    v.addWidget(_section_label("APPEARANCE"))
    for lab in ("  Fill", "  Line"):
        b = QPushButton(lab)
        b.setMinimumHeight(26)
        v.addWidget(b)
    v.addStretch()
    return panel


def _new_column() -> QWidget:
    panel = QWidget()
    panel.setObjectName("newPanel")
    panel.setStyleSheet(NEW_QSS)
    panel.setFixedWidth(200)
    _shadow(panel, blur=34, alpha=170, dy=8)

    v = QVBoxLayout(panel)
    v.setContentsMargins(14, 16, 14, 16)
    v.setSpacing(7)

    v.addWidget(_section_label("TOOLS"))
    tools = [("Select", "select"), ("Rectangle", "rect"),
             ("Line", "line"), ("Text", "text")]
    btns = []
    for lab, kind in tools:
        b = ToolButton(lab, kind)
        btns.append(b)
        v.addWidget(b)
    btns[1].setChecked(True)  # Rectangle active, to show the accent state

    v.addSpacing(8)
    v.addWidget(_section_label("APPEARANCE"))
    v.addWidget(_dropdown("Fill", "#00c8ff"))
    v.addWidget(_dropdown("Line", "#ffff00"))

    v.addSpacing(10)
    v.addWidget(_section_label("PRESETS"))
    v.addWidget(_swatch_grid())
    v.addStretch()
    return panel


def _labeled(title: str, col: QWidget) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    cap = QLabel(title)
    cap.setStyleSheet(f"color:{TEXT_DIM}; font-size:11px; font-weight:600;")
    cap.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    lay.addWidget(cap)
    row = QHBoxLayout()
    row.addStretch()
    row.addWidget(col)
    row.addStretch()
    lay.addLayout(row)
    return box


class Preview(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("rapid-pdf UI direction preview")
        self.setMinimumSize(620, 520)
        self.setStyleSheet(f"QWidget {{ background-color: {BG}; color: {TEXT}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(18)

        title = QLabel("rapid-pdf toolbar: current vs proposed")
        title.setStyleSheet(f"color:{TEXT}; font-size:16px; font-weight:700;")
        root.addWidget(title)

        sub = QLabel(
            "Same controls, re-skinned: gradient surfaces, softer borders, 8px "
            "radius, icon-led buttons, and a glowing accent on the active tool."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{TEXT_DIM}; font-size:11px;")
        root.addWidget(sub)

        cols = QHBoxLayout()
        cols.setSpacing(40)
        cols.addStretch()
        cols.addWidget(_labeled("BEFORE  (current)", _old_column()))
        cols.addWidget(_labeled("AFTER  (proposed)", _new_column()))
        cols.addStretch()
        root.addLayout(cols)
        root.addStretch()


def _maybe_apply_mica(win: QWidget) -> None:
    if not MICA:
        return
    try:
        import pywinstyles  # optional; only if Lucas opts in
        pywinstyles.apply_style(win, "mica")
        pywinstyles.change_header_color(win, BG)
    except Exception as exc:  # lib missing / not Win11 / unsupported
        print(f"[ui_preview] Mica not applied ({exc}); showing pure-QSS look.")


def main() -> int:
    app = QApplication(sys.argv)
    win = Preview()
    _maybe_apply_mica(win)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

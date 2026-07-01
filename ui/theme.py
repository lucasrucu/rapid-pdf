"""
Qori theme module — a reusable light/dark QSS theme for PySide6 desktop apps.

WHY THIS EXISTS
---------------
rapid-pdf is the first of Lucas's desktop apps to adopt the "Qori" design
language (his personal brand: Quechua for "gold" — cream + amber/gold). The same
look should carry into VideoOS and any future PySide6 tool, so this module is
written to be DROPPED IN WHOLE: copy `ui/theme.py`, call `apply_theme(app, ...)`,
and the whole app picks up the palette.

STRUCTURE (so it ports cleanly)
-------------------------------
- `Palette`        : a frozen dataclass = ALL the color tokens an app needs.
                     Two instances ship: LIGHT (default) and DARK.
- `build_qss()`    : turns a Palette into one global stylesheet string.
- `ThemeManager`   : holds the current mode, applies the QSS to a QApplication,
                     toggles light<->dark, persists the choice via QSettings, and
                     emits `theme_changed` so widgets can re-tint code-drawn bits
                     (icons, scene backgrounds) that QSS can't reach.
- `apply_theme()`  : one-line convenience for `main.py`.
- helpers          : `themed_icon()` (qtawesome with graceful fallback),
                     `accent_shadow()` (the glow QSS can't draw).

DESIGN NOTES
------------
- Qt QSS has NO transitions/animations and NO box-shadow on widgets. Depth comes
  from gradients (in QSS) plus QGraphicsDropShadowEffect (in code). This mirrors
  the approved prototype in prototypes/ui_preview.py.
- LIGHT is the default per Lucas. The Sovereign palette is warm: cream surfaces,
  amber/gold accent (#F1AE04), matching finance.qori.land.
- Tokens are named by ROLE (surface, surface_raised, border, accent…), never by
  literal color, so the dark variant is a drop-in swap and VideoOS can re-skin by
  editing one Palette.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QObject, Signal, QSettings
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QWidget, QGraphicsDropShadowEffect


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------
class ThemeMode(str, Enum):
    LIGHT = "light"
    DARK = "dark"


# ---------------------------------------------------------------------------
# Palette — every color token the app needs, named by role.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Palette:
    name: str

    # Surfaces (back to front)
    window: str          # app background, behind everything
    surface: str         # panels, toolbars, tab pane
    surface_raised: str  # raised control top (gradient start)
    surface_sunken: str  # raised control bottom (gradient end) / list backgrounds
    canvas: str          # the document/work area behind page content

    # Lines + text
    border: str          # soft borders / dividers
    border_strong: str   # hover/focus borders
    text: str            # primary text
    text_dim: str        # secondary / section labels
    text_faint: str      # hints, disabled

    # Accent (Qori gold)
    accent: str          # primary accent (gradient start on active)
    accent_hi: str       # lighter accent (active border, hover ring)
    accent_deep: str     # darker accent (gradient end on active)
    accent_text: str     # text/icon color ON an accent fill

    # Menus + selection
    menu_bg: str
    selection: str       # list/menu selection fill
    selection_text: str

    # Scrollbar
    scroll_track: str
    scroll_handle: str
    scroll_handle_hover: str

    @property
    def is_dark(self) -> bool:
        return QColor(self.window).lightnessF() < 0.5


# --- Sovereign LIGHT (default) — cream + amber/gold, matches finance.qori.land
LIGHT = Palette(
    name="light",
    window="#FAF7F0",          # warm cream (matches --background 48 33% 97%)
    surface="#FFFFFF",         # clean white panels
    surface_raised="#FFFFFF",
    surface_sunken="#F3EFE6",  # faint cream for control gradient end / list bg
    canvas="#E8E3D8",          # soft taupe behind the page (not flat grey)
    border="#E3DCCB",          # warm low-contrast border
    border_strong="#D8BE72",   # amber-tinted hover border
    text="#2A2620",            # near-black warm
    text_dim="#7A7264",        # muted brown-grey section labels
    text_faint="#A99F8C",
    accent="#F1AE04",          # Qori gold
    accent_hi="#FBC43A",       # lighter gold (hover ring / active border)
    accent_deep="#D6970A",     # deeper gold (active gradient end)
    accent_text="#2A2010",     # dark text on gold (gold is bright → dark reads best)
    menu_bg="#FFFFFF",
    selection="#F1AE04",
    selection_text="#2A2010",
    scroll_track="#F3EFE6",
    scroll_handle="#D9D1BF",
    scroll_handle_hover="#C7BCA3",
)

# --- Sovereign DARK — warm charcoal + the same amber accent (the "midnight" feel)
DARK = Palette(
    name="dark",
    window="#1A1814",          # warm near-black (not the old cold #1a1a1a)
    surface="#242019",         # warm charcoal panel
    surface_raised="#2C2820",  # raised control top
    surface_sunken="#201C16",  # gradient end / list bg
    canvas="#15130F",          # deep warm behind the page
    border="#3A352B",          # warm low-contrast border
    border_strong="#6E5A2E",   # amber-tinted hover border
    text="#EDE7D8",            # warm off-white
    text_dim="#A89E89",        # muted section labels
    text_faint="#6E6553",
    accent="#F1AE04",          # same Qori gold
    accent_hi="#FBC43A",
    accent_deep="#C28F0A",
    accent_text="#1A1408",     # dark text on gold
    menu_bg="#242019",
    selection="#F1AE04",
    selection_text="#1A1408",
    scroll_track="#201C16",
    scroll_handle="#3E382D",
    scroll_handle_hover="#574F3E",
)

_PALETTES = {ThemeMode.LIGHT: LIGHT, ThemeMode.DARK: DARK}


# ---------------------------------------------------------------------------
# QSS builder — one global stylesheet from a Palette.
# ---------------------------------------------------------------------------
def build_qss(p: Palette) -> str:
    """Return the full application stylesheet for the given palette.

    Selectors are GENERIC (QPushButton, QToolButton, QMenu…) so any app picks up
    the look without per-widget styling. App-specific object names referenced:
      #section  — small uppercase section labels (toolbar headers)
      #tool     — checkable tool buttons (the accent-on-active rail buttons)
    Both degrade gracefully if an app doesn't use them.
    """
    return f"""
/* ---- base ---------------------------------------------------------- */
QMainWindow, QWidget {{
    background-color: {p.window};
    color: {p.text};
}}
QToolTip {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    padding: 4px 6px;
    border-radius: 6px;
}}

/* ---- panels / toolbars --------------------------------------------- */
QWidget#ToolBar {{
    background-color: {p.surface};
    border-left: 1px solid {p.border};
}}

/* ---- buttons ------------------------------------------------------- */
QPushButton {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {p.surface_raised}, stop:1 {p.surface_sunken});
    border: 1px solid {p.border};
    border-radius: 8px;
    color: {p.text};
    padding: 5px 10px;
    text-align: left;
}}
QPushButton:hover {{
    border: 1px solid {p.border_strong};
    color: {p.text};
}}
QPushButton:pressed {{
    background-color: {p.surface_sunken};
}}
QPushButton:checked {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {p.accent_hi}, stop:1 {p.accent_deep});
    border: 1px solid {p.accent_hi};
    color: {p.accent_text};
    font-weight: 600;
}}
QPushButton:disabled {{
    color: {p.text_faint};
    border: 1px solid {p.border};
}}

/* tool-rail buttons: same gradient, taller, accent fill when active */
QPushButton#tool {{
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 12px;
}}
QPushButton#tool:checked {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {p.accent_hi}, stop:1 {p.accent_deep});
    border: 1px solid {p.accent_hi};
    color: {p.accent_text};
}}

/* ---- tool buttons (color dropdowns, opacity) ----------------------- */
QToolButton {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {p.surface_raised}, stop:1 {p.surface_sunken});
    border: 1px solid {p.border};
    border-radius: 8px;
    color: {p.text};
    padding: 5px 8px 5px 8px;
    text-align: left;
}}
QToolButton:hover {{
    border: 1px solid {p.border_strong};
    color: {p.text};
}}
QToolButton::menu-indicator {{
    subcontrol-origin: padding;
    subcontrol-position: right center;
    right: 6px;
}}

/* ---- combo boxes --------------------------------------------------- */
QComboBox {{
    background-color: {p.surface_raised};
    border: 1px solid {p.border};
    border-radius: 8px;
    color: {p.text};
    padding: 3px 6px;
}}
QComboBox:focus {{ border-color: {p.accent}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {p.menu_bg};
    color: {p.text};
    border: 1px solid {p.border};
    selection-background-color: {p.selection};
    selection-color: {p.selection_text};
}}

/* ---- labels -------------------------------------------------------- */
QLabel {{ color: {p.text}; background: transparent; }}
QLabel#section {{
    color: {p.text_dim};
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
}}

/* ---- menus / menubar ----------------------------------------------- */
QMenuBar {{
    background-color: {p.surface};
    color: {p.text};
    border-bottom: 1px solid {p.border};
}}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background-color: {p.selection}; color: {p.selection_text}; }}
QMenu {{
    background-color: {p.menu_bg};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {p.selection}; color: {p.selection_text}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 8px; }}

/* ---- status bar ---------------------------------------------------- */
QStatusBar {{
    background-color: {p.window};
    color: {p.text_dim};
    border-top: 1px solid {p.border};
}}
QStatusBar::item {{ border: none; }}

/* ---- tabs ---------------------------------------------------------- */
QTabWidget::pane {{ border: none; background-color: {p.window}; }}
QTabBar::tab {{
    background-color: {p.surface_sunken};
    color: {p.text_dim};
    border: 1px solid {p.border};
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 6px 18px;
    min-width: 80px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background-color: {p.window};
    color: {p.text};
    border-top: 2px solid {p.accent};
}}
QTabBar::tab:hover:!selected {{ background-color: {p.surface}; color: {p.text}; }}

/* ---- list widgets (organizer / page panel) ------------------------- */
QListWidget {{
    background-color: {p.surface_sunken};
    border: none;
    outline: none;
}}
QListWidget::item {{ border-radius: 6px; color: {p.text}; padding: 4px; }}
QListWidget::item:selected {{ background-color: {p.selection}; color: {p.selection_text}; }}
QListWidget::item:hover:!selected {{ background-color: {p.surface}; }}

/* ---- frames / dividers --------------------------------------------- */
QFrame[frameShape="4"] {{ color: {p.border}; }}

/* ---- scrollbars ---------------------------------------------------- */
QScrollBar:vertical {{ background: {p.scroll_track}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {p.scroll_handle}; border-radius: 5px; min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.scroll_handle_hover}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {p.scroll_track}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {p.scroll_handle}; border-radius: 5px; min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.scroll_handle_hover}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ---- dialogs / message boxes -------------------------------------- */
QDialog, QMessageBox {{ background-color: {p.window}; color: {p.text}; }}
QDialog QPushButton, QMessageBox QPushButton {{ min-width: 72px; text-align: center; }}
"""


# ---------------------------------------------------------------------------
# qtawesome-backed icons (graceful fallback so the app never hard-depends on it)
# ---------------------------------------------------------------------------
_HAS_QTAWESOME = None


def _qtawesome():
    global _HAS_QTAWESOME
    if _HAS_QTAWESOME is None:
        try:
            import qtawesome  # noqa: F401
            _HAS_QTAWESOME = True
        except Exception:
            _HAS_QTAWESOME = False
    if _HAS_QTAWESOME:
        import qtawesome
        return qtawesome
    return None


def themed_icon(name: str, color: str | QColor) -> QIcon:
    """A qtawesome icon tinted to `color`. Returns an empty QIcon if qtawesome
    isn't installed, so callers can still show a text-only button (the prototype
    drew icons by hand; the real app prefers qtawesome but must not crash without
    it). `name` is a qtawesome id, e.g. 'mdi6.cursor-default-outline'."""
    qta = _qtawesome()
    if qta is None:
        return QIcon()
    col = color.name() if isinstance(color, QColor) else color
    try:
        return qta.icon(name, color=col)
    except Exception:
        return QIcon()


def qtawesome_available() -> bool:
    return _qtawesome() is not None


# ---------------------------------------------------------------------------
# Drop-shadow glow (the depth QSS can't express)
# ---------------------------------------------------------------------------
def accent_shadow(widget: QWidget, color: QColor | str, blur: int = 20,
                  dy: int = 2) -> None:
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(color) if not isinstance(color, QColor) else color)
    widget.setGraphicsEffect(eff)


def soft_shadow(widget: QWidget, blur: int = 28, alpha: int = 60, dy: int = 6) -> None:
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)


# ---------------------------------------------------------------------------
# Optional Win11 Mica/acrylic backdrop (no-op if pywinstyles missing / not Win11)
# ---------------------------------------------------------------------------
def apply_mica(window: QWidget, dark: bool) -> bool:
    """Best-effort Win11 Mica backdrop. Returns True if applied. Silent no-op on
    non-Win11 / when pywinstyles isn't installed, so it's safe to always call."""
    try:
        import pywinstyles
        pywinstyles.apply_style(window, "mica")
        pywinstyles.change_header_color(window, "#1A1814" if dark else "#FAF7F0")
        try:
            pywinstyles.change_title_color(window, "#EDE7D8" if dark else "#2A2620")
        except Exception:
            pass
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ThemeManager — the public entry point.
# ---------------------------------------------------------------------------
class ThemeManager(QObject):
    """Applies the QSS to a QApplication, toggles light/dark, persists the choice,
    and signals when the mode changes so code-drawn surfaces can re-tint.

    Usage:
        theme = ThemeManager(app)          # reads saved mode, defaults LIGHT
        theme.apply()                      # paint the app
        theme.theme_changed.connect(...)   # re-tint icons / scene bg on toggle
        theme.toggle()                     # flip light<->dark, repaint, persist
    """

    theme_changed = Signal(object)  # emits the new Palette

    def __init__(self, app: QApplication | None = None,
                 settings_org: str = "Lucas", settings_app: str = "Rapid PDF",
                 default: ThemeMode = ThemeMode.LIGHT):
        super().__init__(app)
        self._app = app or QApplication.instance()
        self._settings = QSettings(settings_org, settings_app)
        saved = self._settings.value("theme/mode", default.value)
        try:
            self._mode = ThemeMode(saved)
        except ValueError:
            self._mode = default

    # -- state -----------------------------------------------------------
    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @property
    def palette(self) -> Palette:
        return _PALETTES[self._mode]

    @property
    def is_dark(self) -> bool:
        return self._mode is ThemeMode.DARK

    # -- actions ---------------------------------------------------------
    def apply(self) -> None:
        """Paint the whole application in the current mode."""
        p = self.palette
        if self._app is not None:
            # Fusion + a QPalette so native bits (focus rings, disabled text,
            # combo popups) read correctly under both modes; QSS layers on top.
            self._app.setStyle("Fusion")
            self._app.setPalette(self._qpalette(p))
            self._app.setStyleSheet(build_qss(p))

    def set_mode(self, mode: ThemeMode) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._settings.setValue("theme/mode", mode.value)
        self.apply()
        self.theme_changed.emit(self.palette)

    def toggle(self) -> None:
        self.set_mode(ThemeMode.DARK if self._mode is ThemeMode.LIGHT
                      else ThemeMode.LIGHT)

    @staticmethod
    def _qpalette(p: Palette) -> QPalette:
        qp = QPalette()
        qp.setColor(QPalette.ColorRole.Window, QColor(p.window))
        qp.setColor(QPalette.ColorRole.WindowText, QColor(p.text))
        qp.setColor(QPalette.ColorRole.Base, QColor(p.surface))
        qp.setColor(QPalette.ColorRole.AlternateBase, QColor(p.surface_sunken))
        qp.setColor(QPalette.ColorRole.Text, QColor(p.text))
        qp.setColor(QPalette.ColorRole.Button, QColor(p.surface_raised))
        qp.setColor(QPalette.ColorRole.ButtonText, QColor(p.text))
        qp.setColor(QPalette.ColorRole.Highlight, QColor(p.selection))
        qp.setColor(QPalette.ColorRole.HighlightedText, QColor(p.selection_text))
        qp.setColor(QPalette.ColorRole.ToolTipBase, QColor(p.surface))
        qp.setColor(QPalette.ColorRole.ToolTipText, QColor(p.text))
        qp.setColor(QPalette.ColorRole.PlaceholderText, QColor(p.text_faint))
        disabled = QColor(p.text_faint)
        qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
        qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
        qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)
        return qp


# ---------------------------------------------------------------------------
# One-line convenience for main.py
# ---------------------------------------------------------------------------
def apply_theme(app: QApplication, default: ThemeMode = ThemeMode.LIGHT) -> ThemeManager:
    """Create a ThemeManager, apply it, and return it (keep the reference so you
    can wire a toggle and connect to theme_changed)."""
    tm = ThemeManager(app, default=default)
    tm.apply()
    return tm

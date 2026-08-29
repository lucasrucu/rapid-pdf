"""
Qori theme module, a reusable light/dark QSS theme for PySide6 desktop apps.

WHY THIS EXISTS
---------------
rapid-pdf is the first of Lucas's desktop apps to adopt the "Qori" design
language, and the same look should carry into VideoOS and any future PySide6
tool, so this module is written to be DROPPED IN WHOLE: copy `ui/theme.py`,
call `apply_theme(app, ...)`, and the whole app picks up the palette.

STRUCTURE (so it ports cleanly)
-------------------------------
- `Palette`        : a frozen dataclass = ALL the color tokens an app needs.
                     Two instances ship: LIGHT (default) and DARK.
- `build_qss()`    : turns a Palette into one global stylesheet string.
- `ThemeManager`   : holds the current mode, applies the QSS to a QApplication,
                     toggles light<->dark, persists the choice through a settings
                     store, and emits `theme_changed` so widgets can re-tint
                     code-drawn bits
                     (icons, scene backgrounds, canvas selection chrome) that QSS
                     can't reach.
- `apply_theme()`  : one-line convenience for `main.py`.
- helpers          : `themed_icon()` (qtawesome with graceful fallback),
                     `accent_shadow()` (the glow QSS can't draw).

GRAPHITE, ONE ACCENT
--------------------
Near-monochrome. Nothing carries color except a single accent, and the accent
appears on very few things: the active tool, the current selection (pages,
thumbnails, canvas handles), a checked button, and the active tab's underline.
Menus and hover states move on surface value instead, so the accent keeps
meaning "this is the thing you are acting on".

The neutral ramp is Radix `slate`, the published sRGB values, each step used
for the role Radix defines for it:

    step 1  app background      -> window
    step 2  subtle background   -> surface (panels, toolbars, lists)
    step 3  component           -> surface_raised (control fill)
    step 4  component hover     -> surface_hover
    step 5  component active    -> surface_active (pressed)
    step 6  subtle border       -> border (also the scrollbar handle)
    step 7  interactive border  -> border_strong (hover/focus, handle hover)
    step 10 low-contrast text   -> text_faint
    step 11 secondary text      -> text_dim
    step 12 high-contrast text  -> text

`canvas` is the one neutral with no Radix role: it is the work area behind the
page, and its whole job is to make a white page the brightest thing on screen.
Dark sits below step 1; light sits at step 8, because "further from the content"
in a light theme means darker, not lighter.

SOLID COLORS ONLY
-----------------
No gradients anywhere, no shadow standing in for a border, no glass. Depth comes
from surface VALUE (each layer one step lighter), which is why the hover and
active steps exist as tokens instead of hover being a border swap. There is a
test that fails if a gradient function ever reappears in the stylesheet.

THE ACCENT IS THREE LINES
-------------------------
`_ACCENT`, `_ACCENT_PRESS` and `_ACCENT_TEXT` below are the whole accent. Change
those three to Patina teal ("#1FB8AD", "#14857C", "#04211F") and nothing else
moves.

Tokens are named by ROLE (surface, border, accent...), never by literal color,
so the dark variant is a drop-in swap and another app can re-skin by editing one
Palette. A token that is always equal to another token is not a role, it is a
copy, so there are none: every field holds a distinct value in both palettes and
`tests/test_theme.py` enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QWidget, QGraphicsDropShadowEffect


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------
class ThemeMode(str, Enum):
    LIGHT = "light"
    DARK = "dark"


# --- THE ACCENT. Qori gold. Three lines, shared by both palettes: swap them for
# --- Patina teal ("#1FB8AD", "#14857C", "#04211F") and the whole app follows.
_ACCENT = "#F1AE04"
_ACCENT_PRESS = "#C28F0A"
_ACCENT_TEXT = "#1A1408"


# ---------------------------------------------------------------------------
# Palette: every color token the app needs, named by role.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Palette:
    name: str

    # Surfaces (back to front). Depth is value, not gradient or shadow.
    window: str          # app background, behind everything
    surface: str         # panels, toolbars, lists, menus
    surface_raised: str  # control fill (buttons, combos, thumbnail placeholders)
    surface_hover: str   # control fill under the pointer
    surface_active: str  # control fill while pressed
    canvas: str          # the work area behind the page

    # Lines + text
    border: str          # soft borders, dividers, scrollbar handle
    border_strong: str   # hover/focus borders, scrollbar handle hover
    text: str            # primary text
    text_dim: str        # secondary / section labels
    text_faint: str      # hints, disabled

    # The one accent. Selection, the active tool, a checked button, the tab
    # underline, and the canvas selection chrome. Nothing else.
    accent: str
    accent_press: str    # accent while pressed
    accent_text: str     # text/icon color ON an accent fill

    @property
    def is_dark(self) -> bool:
        return QColor(self.window).lightnessF() < 0.5

    @property
    def color_fields(self) -> tuple[str, ...]:
        """Every field that holds a color (i.e. all of them except `name`)."""
        return tuple(f.name for f in fields(self) if f.name != "name")


# --- Graphite LIGHT (default): Radix slate light at the roles Radix specifies.
LIGHT = Palette(
    name="light",
    window="#FCFCFD",          # slate 1
    surface="#F9F9FB",         # slate 2
    surface_raised="#F0F0F3",  # slate 3
    surface_hover="#E8E8EC",   # slate 4
    surface_active="#E0E1E6",  # slate 5
    canvas="#B9BBC6",          # slate 8: dark enough that a white page separates
    border="#D9D9E0",          # slate 6
    border_strong="#CDCED6",   # slate 7
    text="#1C2024",            # slate 12
    text_dim="#60646C",        # slate 11
    text_faint="#80838D",      # slate 10
    accent=_ACCENT,
    accent_press=_ACCENT_PRESS,
    accent_text=_ACCENT_TEXT,
)

# --- Graphite DARK: Radix slate dark at the same roles.
DARK = Palette(
    name="dark",
    window="#111113",          # slate 1
    surface="#18191B",         # slate 2
    surface_raised="#212225",  # slate 3
    surface_hover="#272A2D",   # slate 4
    surface_active="#2E3135",  # slate 5
    canvas="#0B0B0D",          # below slate 1: the page is the brightest thing
    border="#363A3F",          # slate 6
    border_strong="#43484E",   # slate 7
    text="#EDEEF0",            # slate 12
    text_dim="#B0B4BA",        # slate 11
    text_faint="#777B84",      # slate 10
    accent=_ACCENT,
    accent_press=_ACCENT_PRESS,
    accent_text=_ACCENT_TEXT,
)

_PALETTES = {ThemeMode.LIGHT: LIGHT, ThemeMode.DARK: DARK}


# ---------------------------------------------------------------------------
# QSS builder: one global stylesheet from a Palette.
# ---------------------------------------------------------------------------
def build_qss(p: Palette) -> str:
    """Return the full application stylesheet for the given palette.

    Selectors are GENERIC (QPushButton, QToolButton, QMenu...) so any app picks
    up the look without per-widget styling. App-specific object names referenced:
      #section  : small uppercase section labels (toolbar headers)
      #tool     : checkable tool buttons (the accent-on-active rail buttons)
      #ToolBar  : the tool rail panel
    All degrade gracefully if an app doesn't use them.

    Solid fills only. No gradient function may appear here (tested).
    """
    return f"""
/* ---- base ---------------------------------------------------------- */
QMainWindow, QWidget {{
    background-color: {p.window};
    color: {p.text};
}}
QToolTip {{
    background-color: {p.surface_raised};
    color: {p.text};
    border: 1px solid {p.border};
    padding: 4px 6px;
    border-radius: 6px;
}}

/* ---- panels / toolbars ---------------------------------------------
   No border between the rail and the canvas: the value step separates them. */
QWidget#ToolBar {{
    background-color: {p.surface};
    border: none;
}}

/* ---- buttons -------------------------------------------------------
   One flat fill, one step per state. */
QPushButton {{
    background-color: {p.surface_raised};
    border: 1px solid {p.border};
    border-radius: 6px;
    color: {p.text};
    padding: 5px 10px;
    text-align: left;
}}
QPushButton:hover {{
    background-color: {p.surface_hover};
    border: 1px solid {p.border_strong};
    color: {p.text};
}}
QPushButton:pressed {{
    background-color: {p.surface_active};
}}
QPushButton:checked {{
    background-color: {p.accent};
    border: 1px solid {p.accent};
    color: {p.accent_text};
    font-weight: 600;
}}
QPushButton:checked:pressed {{
    background-color: {p.accent_press};
    border: 1px solid {p.accent_press};
}}
QPushButton:disabled {{
    background-color: {p.surface};
    color: {p.text_faint};
    border: 1px solid {p.border};
}}

/* tool-rail buttons: no fill at all until you point at one. Only the active
   tool is filled, which is the biggest single reduction in chrome here. */
QPushButton#tool {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    color: {p.text_dim};
    padding: 7px 10px;
    font-size: 12px;
}}
QPushButton#tool:hover {{
    background-color: {p.surface_hover};
    border: 1px solid transparent;
    color: {p.text};
}}
QPushButton#tool:checked {{
    background-color: {p.accent};
    border: 1px solid {p.accent};
    color: {p.accent_text};
    font-weight: 600;
}}
QPushButton#tool:checked:pressed {{
    background-color: {p.accent_press};
    border: 1px solid {p.accent_press};
}}

/* status-bar view-mode group: one segmented control, not four loose buttons.
   The container carries the outline; the buttons inside carry only their state,
   and the active mode is the accent (the same language as the tool rail). */
QWidget#fitGroup {{
    background-color: {p.surface_raised};
    border: 1px solid {p.border};
    border-radius: 6px;
}}
QToolButton#fitmode {{
    background-color: transparent;
    border: none;
    border-radius: 5px;
    color: {p.text_dim};
    padding: 3px 6px;
    font-size: 10px;
    text-align: center;
}}
QToolButton#fitmode:hover {{
    background-color: {p.surface_hover};
    color: {p.text};
}}
QToolButton#fitmode:checked {{
    background-color: {p.accent};
    color: {p.accent_text};
}}
QToolButton#fitmode:checked:pressed {{
    background-color: {p.accent_press};
}}

/* ---- tool buttons (color dropdowns, opacity) ----------------------- */
QToolButton {{
    background-color: {p.surface_raised};
    border: 1px solid {p.border};
    border-radius: 6px;
    color: {p.text};
    padding: 5px 8px 5px 8px;
    text-align: left;
}}
QToolButton:hover {{
    background-color: {p.surface_hover};
    border: 1px solid {p.border_strong};
    color: {p.text};
}}
QToolButton:pressed {{
    background-color: {p.surface_active};
}}
QToolButton::menu-indicator {{
    subcontrol-origin: padding;
    subcontrol-position: right center;
    right: 6px;
}}

/* ---- text inputs / combo boxes ------------------------------------- */
QLineEdit {{
    background-color: {p.window};
    border: 1px solid {p.border};
    border-radius: 6px;
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
    padding: 3px 8px;
}}
QLineEdit:focus {{ border-color: {p.border_strong}; }}
QLineEdit:disabled {{ color: {p.text_faint}; }}
QComboBox {{
    background-color: {p.surface_raised};
    border: 1px solid {p.border};
    border-radius: 6px;
    color: {p.text};
    padding: 3px 6px;
}}
QComboBox:hover {{ background-color: {p.surface_hover}; }}
QComboBox:focus {{ border-color: {p.border_strong}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    selection-background-color: {p.surface_hover};
    selection-color: {p.text};
}}

/* ---- labels -------------------------------------------------------- */
QLabel {{ color: {p.text}; background: transparent; }}
QLabel#section {{
    color: {p.text_faint};
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
}}

/* ---- menus / menubar ------------------------------------------------
   Highlight moves on surface value, not on the accent: the accent stays
   reserved for the thing you are acting on. */
QMenuBar {{
    background-color: {p.window};
    color: {p.text_dim};
    border: none;
}}
QMenuBar::item {{ padding: 4px 10px; background: transparent; border-radius: 4px; }}
QMenuBar::item:selected {{ background-color: {p.surface_raised}; color: {p.text}; }}
QMenu {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background-color: {p.surface_hover}; color: {p.text}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 8px; }}

/* ---- status bar ---------------------------------------------------- */
QStatusBar {{
    background-color: {p.window};
    color: {p.text_dim};
    border-top: 1px solid {p.border};
}}
QStatusBar::item {{ border: none; }}

/* ---- tabs -----------------------------------------------------------
   A label with an accent underline. No boxes, no border row. */
QTabWidget::pane {{ border: none; background-color: {p.window}; }}
QTabBar::tab {{
    background: transparent;
    color: {p.text_faint};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 7px 16px;
    min-width: 80px;
}}
QTabBar::tab:selected {{
    color: {p.text};
    border-bottom: 2px solid {p.accent};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{ color: {p.text_dim}; }}

/* ---- document tabs --------------------------------------------------
   The bar across the top of the window, one tab per open PDF. Same underline
   language as the Editor/Organizer switcher above, but it has to shrink: the
   min-width up there is a floor for two fixed labels, and here it would stop
   tabs narrowing before the bar starts scrolling. The real widths come from
   DocumentTabBar.tabSizeHint; this only takes the floor off. */
#documentTabHeader {{
    background-color: {p.window};
    border-bottom: 1px solid {p.border};
}}
QTabBar#documentTabBar::tab {{
    min-width: 0px;
    padding: 6px 6px 6px 12px;
    margin: 0px;
}}
QTabBar#documentTabBar::tab:selected {{
    background-color: {p.surface};
}}
QToolButton#documentTabChevron {{
    background: transparent;
    border: none;
    color: {p.text_dim};
    font-size: 13px;
}}
QToolButton#documentTabChevron:hover {{
    background-color: {p.surface_hover};
    color: {p.text};
}}
QToolButton#documentTabChevron::menu-indicator {{ image: none; }}

/* ---- list widgets (organizer / page panel) ------------------------- */
QListWidget {{
    background-color: {p.surface};
    border: none;
    outline: none;
}}
QListWidget::item {{ border-radius: 6px; color: {p.text}; padding: 4px; }}
QListWidget::item:selected {{ background-color: {p.accent}; color: {p.accent_text}; }}
QListWidget::item:hover:!selected {{ background-color: {p.surface_hover}; }}

/* ---- frames / dividers --------------------------------------------- */
QFrame[frameShape="4"] {{ color: {p.border}; }}

/* ---- scrollbars ---------------------------------------------------- */
QScrollBar:vertical {{ background: {p.surface}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {p.border}; border-radius: 5px; min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.border_strong}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {p.surface}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {p.border}; border-radius: 5px; min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.border_strong}; }}
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
    non-Win11 / when pywinstyles isn't installed, so it's safe to always call.

    Reads its colors from the palette, so the title bar follows a re-skin instead
    of keeping whatever four literals were pasted in here."""
    p = DARK if dark else LIGHT
    try:
        import pywinstyles
        pywinstyles.apply_style(window, "mica")
        pywinstyles.change_header_color(window, p.window)
        try:
            pywinstyles.change_title_color(window, p.text)
        except Exception:
            pass
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ThemeManager: the public entry point.
# ---------------------------------------------------------------------------
def _default_store():
    """rapid-pdf's settings store, imported here rather than at module scope.

    A late import keeps `ui/theme.py` copyable into another PySide6 app: the
    dependency on core/settings.py only materialises for callers that let the
    default apply, and passing your own `store=` never reaches this line.
    """
    from core.settings import settings
    return settings().appearance


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
                 store=None,
                 default: ThemeMode = ThemeMode.LIGHT):
        super().__init__(app)
        self._app = app or QApplication.instance()
        # `store` is anything carrying a readable/writable `theme` string, which
        # in rapid-pdf is core.settings.settings().appearance. Holding it to that
        # one attribute is what keeps this module droppable into another app:
        # pass a two-line shim rather than dragging core/settings.py along.
        self._store = store if store is not None else _default_store()
        try:
            self._mode = ThemeMode(self._store.theme)
        except (ValueError, AttributeError):
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
        try:
            self._store.theme = mode.value
        except (AttributeError, ValueError):
            pass          # a theme that cannot be remembered still applies
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
        qp.setColor(QPalette.ColorRole.AlternateBase, QColor(p.surface_raised))
        qp.setColor(QPalette.ColorRole.Text, QColor(p.text))
        qp.setColor(QPalette.ColorRole.Button, QColor(p.surface_raised))
        qp.setColor(QPalette.ColorRole.ButtonText, QColor(p.text))
        qp.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
        qp.setColor(QPalette.ColorRole.HighlightedText, QColor(p.accent_text))
        qp.setColor(QPalette.ColorRole.ToolTipBase, QColor(p.surface_raised))
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

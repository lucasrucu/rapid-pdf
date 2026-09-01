"""The top row of the window: the app icon, the document tabs, and the controls.

WHY THE TABS ARE UP HERE. Lucas's words, looking at the old window: "tabs should
be literal top of everything, you should drag a window by grabbing the top,
these icons we have right now for tabs are below, even below the edit file
options." The old stack was system title bar, then the menu bar, then the tab
strip, so the strip you grab to drag the window and the strip that holds the
tabs were two different strips with a menu bar in between. Chrome, Edge and
Firefox all made them one strip years ago and this is that: the row you drag the
window by IS the row the documents live on, and there is nothing above it.

WHAT THIS WIDGET IS NOT. It does not own the tab strip, it HOSTS it.
`DocumentArea` still builds the bar, the chevron and every gesture on them, and
still owns the invariant that ties the bar to the stack underneath; all that
happens here is that the header widget is reparented into this row instead of
sitting on top of the stack. That keeps the whole of ui/document_area.py, the
tear-off in ui/tab_tear_off.py and every test on them working against the same
objects they always did, and it means a DocumentArea built on its own (which is
what most of the tab tests do) still has its own header exactly as before.

THE THREE ZONES, WHICH ARE WHAT `hit_test` IS ABOUT. Windows asks this window
what every pixel of itself is (see ui/frameless.py), and the answer for the top
row is decided here:

  - the app icon is HTSYSMENU, so clicking it opens the system menu, which is
    what the icon in a system title bar has always done;
  - the maximise button is HTMAXBUTTON, which is the one thing that makes the
    Windows 11 Snap Layouts flyout appear when the pointer rests on it;
  - EMPTY SPACE IS HTCAPTION, and "empty" includes the empty part of the tab
    strip past the last tab. That is the Chrome rule and it is the one that
    makes the feature feel right: with two documents open there is half a
    window's width of tab strip to grab, and it drags the window and
    double-clicks to maximise like any other title bar.

Everything else is HTCLIENT and reaches Qt normally: the tabs themselves, their
close buttons, the chevron, the new-tab button, and the minimise and close
buttons.

THE BUTTONS ARE PAINTED, NOT SET IN A FONT. Segoe Fluent Icons is the obvious
way to draw a minimise glyph and it is one missing font away from a row of empty
boxes in the corner of the window. The four glyphs are a line, a rectangle, two
rectangles and a cross, so they are drawn with a pen at Windows' own metrics
(46x32 logical, a 10x10 glyph box) and come out right at every scale without
depending on anything being installed.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QWidget,
)

from ui.frameless import HTCAPTION, HTCLIENT, HTMAXBUTTON, HTSYSMENU

#: Windows' own caption button, in logical pixels. Everything on this row is
#: sized from it, because a title bar shorter than its own close button is the
#: first thing that reads as "not a real window".
CAPTION_BUTTON_WIDTH = 46
CAPTION_BUTTON_HEIGHT = 32

#: The row itself. Taller than the buttons so a tab has room to be a tab, which
#: is the whole reason the row exists.
TITLE_BAR_HEIGHT = 38

#: The glyph inside a caption button: a 10x10 box, centred. Windows draws its
#: own at 10 device pixels at 100% and scales from there, and Qt does that
#: scaling for us as long as the number stays logical.
GLYPH_BOX = 10

#: The app icon at the far left, and the room around it.
APP_ICON_SIZE = 16
APP_ICON_PADDING = 10

#: The strip kept clear between the new-tab button and the window controls.
#:
#: It is a FIXED width and the tab strip takes everything else, which is the
#: opposite of the obvious arrangement. Splitting the row evenly between the two
#: reads well with two documents open and is wrong with eight: the tab bar
#: starts scrolling at half the window width with half a window of nothing next
#: to it. So the tabs get the room, and this is the guarantee that there is
#: always somewhere to grab even when the bar is full. It is not the only such
#: place: empty bar past the last tab is draggable too, which is where most of
#: the room to grab actually comes from.
DRAG_GAP_WIDTH = 80

#: The close button's red. Windows 11's own, and the pressed state a shade under
#: it. The other two buttons move on surface value like everything else in this
#: app, because they are not destructive and colouring them would spend the
#: accent on a control nobody is acting on.
CLOSE_HOVER = "#C42B1C"
CLOSE_PRESSED = "#A82418"
CLOSE_GLYPH_ON_RED = "#FFFFFF"


class CaptionButton(QAbstractButton):
    """Minimise, maximise/restore, or close, drawn to Windows' metrics.

    `kind` is one of "minimise", "maximise" or "close", and the maximise one
    changes what it draws (a box, or two overlapping boxes) rather than being
    two different buttons: the tests ask it which glyph it is showing, and the
    window asks it to switch, and both of those are cheaper against one object.

    THE NATIVE HOVER AND PRESS ARE SEPARATE STATE, and only the maximise button
    ever uses them. Its pixels belong to Windows while Snap Layouts is in play
    (see ui/frameless.py), so Qt never delivers it an enter or a leave and the
    hover has to be pushed in from the native message instead.
    """

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._maximised = False
        self._native_hover = False
        self._native_pressed = False
        self._glyph = QColor("#1a1a1a")
        self._hover_bg = QColor(0, 0, 0, 18)
        self._press_bg = QColor(0, 0, 0, 32)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFixedSize(CAPTION_BUTTON_WIDTH, CAPTION_BUTTON_HEIGHT)
        self.setToolTip({"minimise": "Minimise", "maximise": "Maximise",
                         "close": "Close"}.get(kind, ""))

    # -- what it is ----------------------------------------------------

    def kind(self) -> str:
        return self._kind

    def glyph_name(self) -> str:
        """Which of the four glyphs is on screen right now.

        "restore" is a state of the maximise button, not a fourth button, and
        the tests cannot see pixels, so this is how they ask.
        """
        if self._kind == "maximise":
            return "restore" if self._maximised else "maximise"
        return self._kind

    def set_maximised(self, maximised: bool):
        if self._kind != "maximise" or maximised == self._maximised:
            return
        self._maximised = maximised
        self.setToolTip("Restore" if maximised else "Maximise")
        self.update()

    # -- hover and press pushed in from the native layer ----------------

    def set_native_hover(self, hot: bool):
        if hot == self._native_hover:
            return
        self._native_hover = hot
        self.update()

    def set_native_pressed(self, pressed: bool):
        if pressed == self._native_pressed:
            return
        self._native_pressed = pressed
        self.update()

    def is_hovered(self) -> bool:
        return self._native_hover or self.underMouse()

    def is_pressed(self) -> bool:
        return self._native_pressed or self.isDown()

    # -- colours -------------------------------------------------------

    def apply_palette(self, palette):
        self._glyph = QColor(palette.text)
        self._hover_bg = QColor(palette.surface_hover)
        self._press_bg = QColor(palette.surface_active)
        self.update()

    def background(self) -> QColor | None:
        """The fill behind the glyph, or None when the button is at rest.

        Split out of paintEvent because it is the whole of the hover and press
        behaviour and the tests have to be able to read it. Close is the one
        that goes red, on both states, because on Windows it always has.
        """
        pressed = self.is_pressed()
        hovered = self.is_hovered()
        if self._kind == "close":
            if pressed:
                return QColor(CLOSE_PRESSED)
            if hovered:
                return QColor(CLOSE_HOVER)
            return None
        if pressed:
            return QColor(self._press_bg)
        if hovered:
            return QColor(self._hover_bg)
        return None

    def glyph_colour(self) -> QColor:
        """White on the red, the theme's text colour otherwise."""
        if self._kind == "close" and (self.is_hovered() or self.is_pressed()):
            return QColor(CLOSE_GLYPH_ON_RED)
        return QColor(self._glyph)

    # -- painting ------------------------------------------------------

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(CAPTION_BUTTON_WIDTH, CAPTION_BUTTON_HEIGHT)

    def paintEvent(self, event):
        painter = QPainter(self)
        background = self.background()
        if background is not None:
            painter.fillRect(self.rect(), background)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(self.glyph_colour())
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        box = QRectF(0, 0, GLYPH_BOX, GLYPH_BOX)
        box.moveCenter(QRectF(self.rect()).center())
        # Half-pixel offsets: a 1px pen straddles the coordinate it is given, so
        # an integer coordinate paints two half-covered rows and the glyph looks
        # soft next to Windows' own.
        box.translate(0.5, 0.5)
        name = self.glyph_name()
        if name == "minimise":
            middle = box.center().y()
            painter.drawLine(QPoint(int(box.left()), int(middle)),
                             QPoint(int(box.right()), int(middle)))
        elif name == "maximise":
            painter.drawRect(box.adjusted(0, 0, -1, -1))
        elif name == "restore":
            # The front sheet, and the one behind it showing at the top right.
            front = box.adjusted(0, 2, -3, -1)
            painter.drawRect(front)
            back = box.adjusted(2, 0, -1, -3)
            painter.drawLine(back.topLeft(), back.topRight())
            painter.drawLine(back.topRight(), back.bottomRight())
            painter.drawLine(back.bottomRight(),
                             QPoint(int(front.right()), int(back.bottom())))
            painter.drawLine(back.topLeft(),
                             QPoint(int(back.left()), int(front.top())))
        else:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.drawLine(box.topLeft(), box.bottomRight())
            painter.drawLine(box.topRight(), box.bottomLeft())


class WindowControls(QWidget):
    """The three caption buttons, in Windows' order, flush to the right edge."""

    minimise_requested = Signal()
    maximise_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("windowControls")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._minimise = CaptionButton("minimise", self)
        self._maximise = CaptionButton("maximise", self)
        self._close = CaptionButton("close", self)
        self._close.setObjectName("captionClose")
        for button in (self._minimise, self._maximise, self._close):
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        self._minimise.clicked.connect(self.minimise_requested)
        self._maximise.clicked.connect(self.maximise_requested)
        self._close.clicked.connect(self.close_requested)
        self.setFixedHeight(CAPTION_BUTTON_HEIGHT)

    def minimise_button(self) -> CaptionButton:
        return self._minimise

    def maximise_button(self) -> CaptionButton:
        return self._maximise

    def close_button(self) -> CaptionButton:
        return self._close

    def buttons(self) -> list:
        return [self._minimise, self._maximise, self._close]

    def set_maximised(self, maximised: bool):
        self._maximise.set_maximised(maximised)

    def apply_palette(self, palette):
        for button in self.buttons():
            button.apply_palette(palette)


class TitleBar(QWidget):
    """The row itself. Icon, hosted tab strip, new-tab button, controls.

    The tab strip arrives from outside (`host_tabs`) rather than being built
    here, so that DocumentArea keeps owning it. See the module docstring.
    """

    new_tab_requested = Signal()
    system_menu_requested = Signal(QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("windowTitleBar")
        self.setFixedHeight(TITLE_BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._tabs = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self._icon = QLabel(self)
        self._icon.setObjectName("titleBarIcon")
        self._icon.setFixedWidth(APP_ICON_SIZE + APP_ICON_PADDING * 2)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self._icon)

        # Where the DocumentArea header lands. An empty holder rather than a
        # direct insert, so the row's shape does not change on a window whose
        # tab strip is hidden (one empty document) and the layout does not have
        # to be rebuilt when it comes back.
        self._tab_host = QWidget(self)
        self._tab_host.setObjectName("titleBarTabHost")
        host_row = QHBoxLayout(self._tab_host)
        host_row.setContentsMargins(0, 0, 0, 0)
        host_row.setSpacing(0)
        self._tab_host_row = host_row
        row.addWidget(self._tab_host, stretch=1)

        self._new_tab = QToolButton(self)
        self._new_tab.setObjectName("titleBarNewTab")
        self._new_tab.setText("+")
        self._new_tab.setToolTip("New tab  (Ctrl+T)")
        self._new_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_tab.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._new_tab.setFixedSize(30, 26)
        self._new_tab.clicked.connect(self.new_tab_requested)
        row.addWidget(self._new_tab, 0, Qt.AlignmentFlag.AlignVCenter)

        # The gap between the new-tab button and the controls. Nothing is drawn
        # in it and that is its job: it is the part of the strip that is always
        # there to grab, however many documents are open.
        self._drag_gap = QWidget(self)
        self._drag_gap.setObjectName("titleBarDragGap")
        self._drag_gap.setFixedWidth(DRAG_GAP_WIDTH)
        row.addWidget(self._drag_gap)

        self._controls = WindowControls(self)
        row.addWidget(self._controls, 0, Qt.AlignmentFlag.AlignTop)

    # ------------------------------------------------------------------
    # What it holds
    # ------------------------------------------------------------------

    def host_tabs(self, header: QWidget):
        """Take the DocumentArea's header strip into this row.

        Reparenting only. The bar, the chevron, the close buttons and every
        gesture on them are still DocumentArea's and still wired to it.
        """
        self._tabs = header
        self._tab_host_row.addWidget(header, stretch=1)

    def tab_strip(self):
        return self._tabs

    def controls(self) -> WindowControls:
        return self._controls

    def maximise_button(self) -> CaptionButton:
        return self._controls.maximise_button()

    def new_tab_button(self) -> QToolButton:
        return self._new_tab

    def set_maximised(self, maximised: bool):
        self._controls.set_maximised(maximised)

    def set_app_icon(self, icon: QIcon):
        if icon is not None and not icon.isNull():
            self._icon.setPixmap(icon.pixmap(APP_ICON_SIZE, APP_ICON_SIZE))

    def apply_palette(self, palette):
        self._controls.apply_palette(palette)

    # ------------------------------------------------------------------
    # Naming every pixel for Windows
    # ------------------------------------------------------------------

    def hit_test(self, local: QPoint) -> int:
        """The HT* code for a point inside this row. See the module docstring.

        Pure in the sense that matters: it reads child geometry and returns a
        number, so the tests can walk a row of points across the bar and check
        that the tabs are clickable and the space past them drags the window,
        without a native message anywhere.
        """
        if self._icon.geometry().contains(local):
            return HTSYSMENU
        maximise = self._controls.maximise_button()
        if maximise.isVisible():
            top_left = maximise.mapTo(self, QPoint(0, 0))
            if QRect(top_left, maximise.size()).contains(local):
                return HTMAXBUTTON
        if self.is_drag_area(local):
            return HTCAPTION
        return HTCLIENT

    def is_drag_area(self, local: QPoint) -> bool:
        """Whether this point is bare strip: drag the window, double-click to
        maximise, right-click for the system menu.

        Everything a person can press is subtracted, and the tab strip is
        subtracted only where there is actually a TAB. The empty part of the
        bar past the last tab is bare strip, which is the Chrome rule and the
        thing that makes a two-tab window still draggable by most of its width.
        """
        for child in self._interactive_children():
            if not child.isVisible():
                continue
            rect = QRect(child.mapTo(self, QPoint(0, 0)), child.size())
            if rect.contains(local):
                return False
        header = self._tabs
        if header is not None and header.isVisible():
            rect = QRect(header.mapTo(self, QPoint(0, 0)), header.size())
            if rect.contains(local):
                return self._past_the_last_tab(header, local)
        return True

    def _interactive_children(self) -> list:
        children = [self._new_tab]
        children += self._controls.buttons()
        children.append(self._icon)
        return children

    def _past_the_last_tab(self, header, local: QPoint) -> bool:
        """Whether a point inside the hosted header is empty bar.

        The chevron and the tabs are things to press; the rest of the bar is
        strip. Asked of the QTabBar directly (`tabAt`) rather than of the
        header, because the header also holds the chevron and a point on that
        is not empty bar.
        """
        bar = header.findChild(QWidget, "documentTabBar")
        if bar is None or not bar.isVisible():
            return True
        chevron = header.findChild(QWidget, "documentTabChevron")
        if chevron is not None and chevron.isVisible():
            rect = QRect(chevron.mapTo(self, QPoint(0, 0)), chevron.size())
            if rect.contains(local):
                return False
        in_bar = bar.mapFrom(self, local)
        if not bar.rect().contains(in_bar):
            return True
        return bar.tabAt(in_bar) < 0

    # ------------------------------------------------------------------
    # The gestures, for the platforms where Qt still sees the mouse
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        """Drag the window from bare strip.

        Never reached on the native path: a press on an HTCAPTION pixel is
        answered by Windows and never becomes a Qt event. This is the fallback,
        and it is also what the tests drive, because offscreen has no native
        path at all.
        """
        if event.button() == Qt.MouseButton.LeftButton \
                and self.is_drag_area(event.position().toPoint()):
            if self._start_move():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton \
                and self.is_drag_area(event.position().toPoint()):
            self._toggle_maximised()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        if self.is_drag_area(event.pos()):
            self.system_menu_requested.emit(event.globalPos())
            event.accept()
            return
        super().contextMenuEvent(event)

    def _helper(self):
        window = self.window()
        return getattr(window, "frameless_helper", lambda: None)()

    def _start_move(self) -> bool:
        helper = self._helper()
        return bool(helper is not None and helper.start_move())

    def _toggle_maximised(self):
        helper = self._helper()
        if helper is not None:
            helper.toggle_maximised()

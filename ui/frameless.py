"""A window with no system title bar that still behaves like a Windows window.

WHY THIS EXISTS. The document tabs had to become the top row of the window, and
the top row of a window is the title bar. There is no way to put a widget into
the system title bar, so the system title bar has to go and we draw our own
(ui/title_bar.py). Everything the system used to give us for free then has to be
given back by hand, and the list is longer than it looks: dragging the window,
double-click to maximise, Aero Snap, Snap Layouts, the system menu on right
click and on Alt+Space, resizing from eight directions, the drop shadow, the
minimise/restore animations, and correct maximised geometry on every monitor of
a multi-monitor desk at whatever scale each one is running.

THE APPROACH, AND WHY IT IS NOT `FramelessWindowHint` ON ITS OWN. A plain
frameless Qt window is a `WS_POPUP`, and a `WS_POPUP` is invisible to the parts
of Windows that make a window feel like a window. It does not snap, it has no
shadow, it does not animate when minimised, and Win+Arrow does nothing to it.
Every "frameless window" bug report is some version of that.

So the window keeps the styles that make Windows treat it as a real window
(`WS_THICKFRAME` for snap and resizing, `WS_CAPTION` for the shadow, the
animations and the Alt+Space menu, the two box styles for Win+Up and Win+Down)
and the frame is removed a different way: `WM_NCCALCSIZE` is answered with "the
client area is the whole window", so the non-client area exists as far as the
window manager is concerned and is zero pixels tall as far as the eye is
concerned. This is what Chrome, Edge, VS Code and Firefox all do on Windows, and
it is the only arrangement where the snap behaviours come back on their own
rather than being re-implemented badly.

`WM_NCHITTEST` is then ours, and it is the whole user interface of this module:
every pixel of the window has to be given back to Windows with a name.
`HTCAPTION` over empty title bar space is what makes dragging and
double-clicking work, `HTMAXBUTTON` over the maximise button is what makes the
Windows 11 Snap Layouts flyout appear, `HTSYSMENU` over the app icon is the
system menu, the eight `HTLEFT`/`HTTOPRIGHT`/... codes around the rim are the
resize borders, and `HTCLIENT` everywhere else is what keeps the tabs, the
buttons and the menu clickable. Get one of those wrong and the classic frameless
bug appears: a window you cannot resize, or a title bar you cannot click.

WHAT IS PURE, AND WHY. `resize_edges_at` and `hit_test_code` take numbers and
return an answer. No HWND, no Qt window, no screen. That is deliberate: the
headless suite cannot deliver a native message or grab a real pointer, so the
part that decides where the eight resize zones are is a function the tests can
call directly, for all eight directions, at any size and any scale.

THE FALLBACK PATH. Off Windows, and under the offscreen platform the tests run
on, there are no native messages at all. The helper then falls back to Qt's own
`startSystemMove()` / `startSystemResize()`, which hand the gesture to whatever
window manager is there. It is not as good (no Snap Layouts, and child widgets
swallow the mouse moves that would set the resize cursor), but it is honest
about what it is: the same decisions, taken one layer up, from the same pure
functions.
"""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QGuiApplication

# How wide the grab strip around the rim is, in LOGICAL pixels. Windows' own is
# about 4 device pixels plus the padded border, which at 100% is a strip most
# people miss on the first try; 6 is the number that reads as "the edge" without
# stealing clicks from anything drawn near it.
RESIZE_BORDER = 6

# Corners are a bigger target than the edges that make them. Aiming for a corner
# is aiming for a point, and a point the size of the border is a point nobody
# hits, so within this distance of both an horizontal and a vertical edge counts
# as the corner between them.
RESIZE_CORNER = 14

# ---------------------------------------------------------------------------
# Win32 constants. Spelled out rather than imported so this module reads the
# same on the machine it does nothing on.
# ---------------------------------------------------------------------------
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCMOUSEMOVE = 0x00A0
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_NCMOUSELEAVE = 0x02A2
WM_SYSCOMMAND = 0x0112

HTCLIENT = 1
HTCAPTION = 2
HTSYSMENU = 3
HTMAXBUTTON = 9
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

GWL_STYLE = -16
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000
WS_THICKFRAME = 0x00040000
WS_CAPTION = 0x00C00000

MONITOR_DEFAULTTONEAREST = 2
ABM_GETSTATE = 0x00000004
ABM_GETTASKBARPOS = 0x00000005
ABS_AUTOHIDE = 0x01

TPM_RETURNCMD = 0x0100
TPM_LEFTBUTTON = 0x0000

MF_GRAYED = 0x00000001
MF_ENABLED = 0x00000000
MF_BYCOMMAND = 0x00000000
SC_RESTORE = 0xF120
SC_MOVE = 0xF010
SC_SIZE = 0xF000
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
SC_CLOSE = 0xF060


# ---------------------------------------------------------------------------
# The pure part: where the eight resize zones are
# ---------------------------------------------------------------------------
def resize_edges_at(width: int, height: int, x: int, y: int,
                    border: int = RESIZE_BORDER,
                    corner: int = RESIZE_CORNER) -> Qt.Edges:
    """Which window edges the point (x, y) is grabbing, if any.

    All four combinations of one horizontal and one vertical edge are corners,
    and a corner is a wider target than the two edges that meet there: see
    RESIZE_CORNER. Returns a Qt.Edges with no bits set when the point is in the
    middle of the window, which is the answer for the overwhelming majority of
    points and therefore the one that has to be cheap.
    """
    left = x < border
    right = x >= width - border
    top = y < border
    bottom = y >= height - border
    if top or bottom:
        left = left or x < corner
        right = right or x >= width - corner
    if left or right:
        top = top or y < corner
        bottom = bottom or y >= height - corner
    edges = Qt.Edges()
    if left:
        edges |= Qt.Edge.LeftEdge
    elif right:
        edges |= Qt.Edge.RightEdge
    if top:
        edges |= Qt.Edge.TopEdge
    elif bottom:
        edges |= Qt.Edge.BottomEdge
    return edges


#: Every resize direction, as (Qt edges, the Win32 hit-test code, the cursor).
#: One table so the three answers can never disagree with each other, which is
#: the bug where the cursor says "resize sideways" and the drag resizes
#: diagonally.
_EDGE_TABLE = (
    (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, HTTOPLEFT, Qt.CursorShape.SizeFDiagCursor),
    (Qt.Edge.RightEdge | Qt.Edge.TopEdge, HTTOPRIGHT, Qt.CursorShape.SizeBDiagCursor),
    (Qt.Edge.LeftEdge | Qt.Edge.BottomEdge, HTBOTTOMLEFT,
     Qt.CursorShape.SizeBDiagCursor),
    (Qt.Edge.RightEdge | Qt.Edge.BottomEdge, HTBOTTOMRIGHT,
     Qt.CursorShape.SizeFDiagCursor),
    (Qt.Edge.LeftEdge, HTLEFT, Qt.CursorShape.SizeHorCursor),
    (Qt.Edge.RightEdge, HTRIGHT, Qt.CursorShape.SizeHorCursor),
    (Qt.Edge.TopEdge, HTTOP, Qt.CursorShape.SizeVerCursor),
    (Qt.Edge.BottomEdge, HTBOTTOM, Qt.CursorShape.SizeVerCursor),
)


def _edges_key(edges) -> int:
    """Qt.Edges as a plain int, so two of them can be compared for equality.

    Three shapes arrive here and they do not compare equal to each other: a bare
    `Qt.Edge` member, a combination of two of them, and a plain integer. PySide6
    builds these on Python's own `enum.Flag`, which is deliberately NOT an int,
    so `int(edges)` raises on the first shape and works on the third. One
    conversion in one place is cheaper than being careful at every call site.
    """
    value = getattr(edges, "value", edges)
    try:
        return int(value)
    except (TypeError, ValueError):              # pragma: no cover - defensive
        return 0


def hit_test_code(width: int, height: int, x: int, y: int,
                  border: int = RESIZE_BORDER,
                  corner: int = RESIZE_CORNER) -> int:
    """The Win32 HT* code for a point on the window rim, or HTCLIENT.

    The resize rim only. What the middle of the window is (caption, a button,
    the tabs) is the title bar's question and is asked separately, because the
    rim wins over all of it: the top few pixels of the title bar resize the
    window, exactly as they do on a system title bar.
    """
    edges = _edges_key(resize_edges_at(width, height, x, y, border, corner))
    for candidate, code, _cursor in _EDGE_TABLE:
        if _edges_key(candidate) == edges:
            return code
    return HTCLIENT


def cursor_for_edges(edges) -> Qt.CursorShape:
    """The pointer shape for a set of resize edges, or the plain arrow."""
    key = _edges_key(edges)
    for candidate, _code, cursor in _EDGE_TABLE:
        if _edges_key(candidate) == key:
            return cursor
    return Qt.CursorShape.ArrowCursor


def on_windows() -> bool:
    """Whether the REAL Windows platform plugin is behind this application.

    `sys.platform` is not the question. The test suite runs on Windows under the
    offscreen plugin, where there is no HWND to talk to and every ctypes call
    below would be handed a handle that means nothing.
    """
    if sys.platform != "win32":
        return False
    app = QGuiApplication.instance()
    if app is None:
        return False
    return app.platformName().lower().startswith("windows")


class _NcCalcSizeParams(ctypes.Structure):
    """The first of the three rectangles WM_NCCALCSIZE passes is the one to edit.

    Only the first is touched here, so the structure is declared as the flat
    array of longs it starts with rather than the full NCCALCSIZE_PARAMS: the
    first four entries are the left, top, right and bottom of rgrc[0].
    """
    _fields_ = [("rgrc", ctypes.c_long * 12)]


class _Rect(ctypes.Structure):
    """Win32 RECT. Physical pixels, always."""
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    """MONITORINFO, flattened for the same reason: only the numbers are wanted.

    `rcMonitor` is the whole screen and `rcWork` is what is left of it once the
    taskbar has had its share, which is the rectangle a maximised window is
    supposed to fill.
    """
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("mon_left", ctypes.c_long), ("mon_top", ctypes.c_long),
        ("mon_right", ctypes.c_long), ("mon_bottom", ctypes.c_long),
        ("work_left", ctypes.c_long), ("work_top", ctypes.c_long),
        ("work_right", ctypes.c_long), ("work_bottom", ctypes.c_long),
        ("flags", ctypes.c_ulong),
    ]


class _AppBarData(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("hWnd", ctypes.c_void_p),
        ("uCallbackMessage", ctypes.c_uint),
        ("uEdge", ctypes.c_uint),
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
        ("lParam", ctypes.c_ssize_t),
    ]


def _auto_hide_taskbar_edge(info):
    """Which edge of this monitor an AUTO-HIDDEN taskbar is on, or None.

    Only auto-hidden ones matter. A taskbar that is always visible has already
    been taken out of the work area, so there is nothing to keep clear of; an
    auto-hidden one leaves the work area covering the whole screen, and a window
    that fills the last pixel of the edge it hides on is a taskbar that never
    comes back out. Windows' own answer to that is to leave it one pixel, which
    is what the caller does with this.
    """
    try:
        shell32 = ctypes.windll.shell32
        data = _AppBarData()
        data.cbSize = ctypes.sizeof(_AppBarData)
        if not (shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(data))
                & ABS_AUTOHIDE):
            return None
        if not shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(data)):
            return None
        # The taskbar has to be on THIS monitor to be in this window's way.
        if not (data.left < info.mon_right and data.right > info.mon_left
                and data.top < info.mon_bottom and data.bottom > info.mon_top):
            return None
        return ("left", "top", "right", "bottom")[min(int(data.uEdge), 3)]
    except Exception:                            # pragma: no cover - defensive
        return None


class FramelessHelper(QObject):
    """Everything one frameless window needs, hung off that window.

    Built by MainWindow and handed the title bar, because the two questions
    this has to answer about any given point ("is that the drag strip", "is that
    the maximise button") are the title bar's to answer. Nothing here knows what
    a tab is.
    """

    def __init__(self, window, title_bar=None):
        super().__init__(window)
        self._window = window
        self._title_bar = title_bar
        self._native = False        # the WM_NCHITTEST path is live
        self._max_button_hot = False

    # ------------------------------------------------------------------
    # Setting it up
    # ------------------------------------------------------------------

    def set_title_bar(self, title_bar):
        self._title_bar = title_bar

    def title_bar(self):
        return self._title_bar

    def is_native(self) -> bool:
        """Whether the Win32 path is doing the work, rather than the fallback.
        Named for the tests, which run where it never is."""
        return self._native

    def apply(self):
        """Take the system title bar off, and give the styles back to Windows.

        The order matters. The window flag has to be set before the HWND is
        looked at, because Qt recreates the native window when the flags change
        and the style bits below would be set on a handle that is about to be
        thrown away.
        """
        self._window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        if not on_windows():
            return False
        try:
            hwnd = int(self._window.winId())
            user32 = ctypes.windll.user32
            get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_style.restype = ctypes.c_longlong
            set_style.restype = ctypes.c_longlong
            style = get_style(hwnd, GWL_STYLE)
            # WS_CAPTION is the surprising one and it is the load-bearing one:
            # it is what gives the window its drop shadow, its minimise and
            # restore animations, and the Alt+Space system menu. Nothing of it
            # is ever drawn, because WM_NCCALCSIZE below gives the whole window
            # to the client area.
            set_style(hwnd, GWL_STYLE,
                      ctypes.c_longlong(int(style) | WS_THICKFRAME | WS_CAPTION
                                        | WS_MAXIMIZEBOX | WS_MINIMIZEBOX))
            self._native = True
            return True
        except Exception:
            # A frameless window that has lost only its extras is still a
            # usable window: the Qt fallback below drives every gesture.
            self._native = False
            return False

    # ------------------------------------------------------------------
    # The native path
    # ------------------------------------------------------------------

    def native_event(self, event_type, message):
        """MainWindow.nativeEvent forwards here. None means "not ours".

        Returning None rather than (False, 0) lets the caller fall through to
        `super().nativeEvent`, which is what Qt needs for every message this
        does not name.
        """
        if not self._native:
            return None
        if event_type not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return None
        msg = _as_msg(message)
        if msg is None:
            return None
        if msg.hWnd != int(self._window.winId()):
            return None
        handler = {
            WM_NCCALCSIZE: self._on_nccalcsize,
            WM_NCHITTEST: self._on_nchittest,
            WM_NCMOUSEMOVE: self._on_ncmousemove,
            WM_NCMOUSELEAVE: self._on_ncmouseleave,
            WM_NCLBUTTONDOWN: self._on_nclbuttondown,
            WM_NCLBUTTONUP: self._on_nclbuttonup,
        }.get(msg.message)
        if handler is None:
            return None
        return handler(msg)

    def _on_nccalcsize(self, msg):
        """"The client area is the whole window." The frame is gone from here.

        The exception is a MAXIMISED window, and it is the one place this had to
        be measured rather than guessed. The usual recipe for a window like this
        is to take the frame thickness off all four sides, because Windows
        normally grows a maximised window's rect past the work area by exactly
        that much and expects the frame to swallow the difference. This window
        is a `WS_POPUP` as far as Qt is concerned, and Qt answers
        `WM_GETMINMAXINFO` for it with the available geometry, so the rect that
        arrives here is ALREADY the work area and taking the thickness off again
        leaves a maximised window sixteen pixels short on both axes. Measured on
        a 2560x1392 work area: 2544x1376.

        So it is a CLAMP, not a subtraction. Intersecting with the monitor's
        work area is right whichever of the two the rect turns out to be, which
        matters because the answer depends on the Qt version.
        """
        if not msg.wParam:
            return None
        if self._window.isMaximized() or self._window.isFullScreen():
            try:
                params = ctypes.cast(
                    msg.lParam, ctypes.POINTER(_NcCalcSizeParams)).contents
                self._clamp_to_work_area(params)
            except Exception:                    # pragma: no cover - defensive
                pass
        return True, 0

    def _clamp_to_work_area(self, params):
        """Hold a maximised window inside the work area of its own monitor.

        Its OWN monitor: the window is asked which one it is on rather than the
        primary being assumed, because a maximised window on the second screen
        of a two-screen desk is the normal case and the primary's work area is
        the wrong rectangle for it, sometimes by hundreds of pixels.
        """
        user32 = ctypes.windll.user32
        hwnd = int(self._window.winId())
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return
        work = [info.work_left, info.work_top, info.work_right, info.work_bottom]
        # An auto-hidden taskbar leaves no work area to be kept out of, and a
        # window covering the last pixel of its edge is a taskbar that never
        # slides back out. One pixel is all it needs.
        edge = _auto_hide_taskbar_edge(info)
        if edge is not None:
            if edge == "left":
                work[0] += 1
            elif edge == "top":
                work[1] += 1
            elif edge == "right":
                work[2] -= 1
            else:
                work[3] -= 1
        params.rgrc[0] = max(params.rgrc[0], work[0])
        params.rgrc[1] = max(params.rgrc[1], work[1])
        params.rgrc[2] = min(params.rgrc[2], work[2])
        params.rgrc[3] = min(params.rgrc[3], work[3])

    def _on_nchittest(self, msg):
        """Name every pixel of the window for Windows. The whole feature."""
        local = self._local_point(_loword_signed(msg.lParam),
                                  _hiword_signed(msg.lParam))
        if local is None:                        # pragma: no cover - defensive
            return None
        return True, self.hit_test(local)

    def _local_point(self, x: int, y: int):
        """A point in PHYSICAL screen pixels, as a point in this window.

        Measured against this window's own rectangle, and NOT by converting to
        Qt's global coordinates first. That was the first implementation and it
        is wrong on the desk this runs on: Qt's global space is logical, every
        monitor contributes its own stretch of it at its own scale, and there is
        no single divisor that turns a physical screen point into a logical one
        across two monitors at 100% and 150%. `GetWindowRect` is physical and is
        this window's, so the subtraction happens in one coordinate space and
        only then is anything divided. Per-monitor DPI awareness gives the whole
        window one scale at a time, so that one divisor is exact for every pixel
        of it.

        The window rect is also the client rect here, because `WM_NCCALCSIZE`
        above gave the whole window to the client area.
        """
        try:
            rect = _Rect()
            if not ctypes.windll.user32.GetWindowRect(
                    int(self._window.winId()), ctypes.byref(rect)):
                return None
            ratio = self._window.devicePixelRatioF() or 1.0
            return QPoint(int(round((x - rect.left) / ratio)),
                          int(round((y - rect.top) / ratio)))
        except Exception:                        # pragma: no cover - defensive
            return None

    def hit_test(self, local: QPoint) -> int:
        """The HT* code for a point in the window's own logical coordinates.

        Public and Qt-only, so the tests can ask the same question the native
        message asks without a native message to ask it with.
        """
        window = self._window
        if not (self._window.isMaximized() or self._window.isFullScreen()):
            code = hit_test_code(window.width(), window.height(),
                                 local.x(), local.y())
            if code != HTCLIENT:
                return code
        bar = self._title_bar
        if bar is None or not bar.isVisible():
            return HTCLIENT
        in_bar = bar.mapFrom(window, local)
        if not bar.rect().contains(in_bar):
            return HTCLIENT
        return bar.hit_test(in_bar)

    # -- the maximise button, which Windows owns while Snap Layouts is up --

    def _max_button(self):
        bar = self._title_bar
        return None if bar is None else bar.maximise_button()

    def _on_ncmousemove(self, msg):
        """Hover for the one button Windows took off us.

        `HTMAXBUTTON` is what makes the Windows 11 Snap Layouts flyout appear
        over the maximise button, and the price of it is that Qt never sees a
        mouse event there: no enter, no leave, no hover paint. So the hover is
        driven from here instead. The other two buttons stay `HTCLIENT` and keep
        Qt's own hover, which is why only this one needs the extra plumbing.
        """
        if msg.wParam != HTMAXBUTTON:
            self._set_max_hot(False)
            return None
        self._set_max_hot(True)
        return True, 0

    def _on_ncmouseleave(self, msg):
        self._set_max_hot(False)
        return None

    def _set_max_hot(self, hot: bool):
        if hot == self._max_button_hot:
            return
        self._max_button_hot = hot
        button = self._max_button()
        if button is not None:
            button.set_native_hover(hot)

    def _on_nclbuttondown(self, msg):
        if msg.wParam != HTMAXBUTTON:
            return None
        button = self._max_button()
        if button is not None:
            button.set_native_pressed(True)
        return True, 0

    def _on_nclbuttonup(self, msg):
        if msg.wParam != HTMAXBUTTON:
            return None
        button = self._max_button()
        if button is not None:
            button.set_native_pressed(False)
        self.toggle_maximised()
        return True, 0

    # ------------------------------------------------------------------
    # The gestures, driven from either path
    # ------------------------------------------------------------------

    def start_move(self) -> bool:
        """Hand the window drag to the window manager.

        The fallback for the platforms with no `HTCAPTION`. On Windows the
        native path never reaches this, because a press on an `HTCAPTION` pixel
        is answered by Windows itself and never becomes a Qt mouse event.
        """
        handle = self._window.windowHandle()
        if handle is None:
            return False
        return bool(handle.startSystemMove())

    def start_resize(self, edges) -> bool:
        """Same, for a drag that started on the rim."""
        if not _edges_key(edges):
            return False
        handle = self._window.windowHandle()
        if handle is None:
            return False
        return bool(handle.startSystemResize(edges))

    def toggle_maximised(self):
        """Double-click on the caption, and the maximise/restore button."""
        window = self._window
        if window.isMaximized() or window.isFullScreen():
            window.showNormal()
        else:
            window.showMaximized()

    def show_system_menu(self, global_pos: QPoint) -> bool:
        """Move / Size / Minimize / Maximize / Close, from the real system menu.

        Right-clicking the title bar and Alt+Space both land here. On the native
        path Windows would answer the right click on its own, because the strip
        is `HTCAPTION`; this is what covers Alt+Space, where the key has already
        been eaten by Qt before `DefWindowProc` could see it.
        """
        if not self._native:
            return False
        try:
            user32 = ctypes.windll.user32
            hwnd = int(self._window.winId())
            menu = user32.GetSystemMenu(hwnd, False)
            if not menu:
                return False
            maximised = self._window.isMaximized()
            for item, enabled in ((SC_RESTORE, maximised),
                                  (SC_MOVE, not maximised),
                                  (SC_SIZE, not maximised),
                                  (SC_MAXIMIZE, not maximised),
                                  (SC_MINIMIZE, True),
                                  (SC_CLOSE, True)):
                user32.EnableMenuItem(
                    menu, item,
                    MF_BYCOMMAND | (MF_ENABLED if enabled else MF_GRAYED))
            ratio = self._window.devicePixelRatioF() or 1.0
            command = user32.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_LEFTBUTTON,
                int(global_pos.x() * ratio), int(global_pos.y() * ratio),
                0, hwnd, None)
            if command:
                user32.PostMessageW(hwnd, WM_SYSCOMMAND, command, 0)
            return True
        except Exception:                        # pragma: no cover - defensive
            return False

    # ------------------------------------------------------------------
    # The Qt fallback for resizing
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        """Resize from the rim on the platforms with no WM_NCHITTEST.

        Installed on the window itself. It is a poorer thing than the native
        path and known to be: a child widget sitting on the rim eats the move
        before this sees it, so the cursor does not always change. What it does
        guarantee is that the gesture EXISTS everywhere, which is the difference
        between a window that cannot be resized and one that can.
        """
        try:
            if self._native or self._window.isMaximized():
                return False
        except RuntimeError:
            # The window has been torn down and Qt is still delivering to a
            # filter installed on it. Nothing to decide, and raising out of an
            # event filter takes the next unrelated event down with it.
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseMove and not event.buttons():
            edges = self.edges_at(event.position().toPoint())
            self._window.setCursor(cursor_for_edges(edges))
            return False
        if kind == QEvent.Type.MouseButtonPress \
                and event.button() == Qt.MouseButton.LeftButton:
            edges = self.edges_at(event.position().toPoint())
            if _edges_key(edges) and self.start_resize(edges):
                return True
        return False

    def edges_at(self, local: QPoint):
        """The resize edges under a point in window coordinates."""
        return resize_edges_at(self._window.width(), self._window.height(),
                               local.x(), local.y())


# ---------------------------------------------------------------------------
# Reading the MSG out of whatever PySide handed us
# ---------------------------------------------------------------------------
def _as_msg(message):
    """The MSG structure behind `nativeEvent`'s second argument.

    PySide has handed this out as a plain integer address and as a voidptr in
    different releases, and the difference is invisible until the day the
    application takes every mouse click and drops it. Both are accepted, and a
    third shape nobody has seen yet fails to None rather than raising, because
    the cost of getting this wrong is a window with no title bar behaviour at
    all and the cost of the guard is one try block.
    """
    if not on_windows():
        return None
    try:
        address = int(message)
    except (TypeError, ValueError):
        try:
            address = int(ctypes.cast(int(message.__int__()), ctypes.c_void_p).value)
        except Exception:
            return None
    if not address:
        return None
    try:
        return ctypes.cast(address, ctypes.POINTER(_MSG)).contents
    except Exception:                            # pragma: no cover - defensive
        return None


if sys.platform == "win32":
    from ctypes import wintypes

    class _MSG(ctypes.Structure):
        _fields_ = [
            ("hWnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", ctypes.c_size_t),
            ("lParam", ctypes.c_ssize_t),
            ("time", wintypes.DWORD),
            ("pt_x", ctypes.c_long),
            ("pt_y", ctypes.c_long),
        ]
else:                                            # pragma: no cover - not Windows
    class _MSG(ctypes.Structure):
        _fields_ = [("hWnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t)]


def _loword_signed(value: int) -> int:
    """The low 16 bits of an LPARAM, as a SIGNED coordinate.

    Signed is the point. A window straddling the left edge of the primary
    monitor gets negative screen x, and read unsigned that is 65000-odd pixels
    to the right, so every hit test on that window answers HTCLIENT and the
    title bar quietly stops working on one monitor of two.
    """
    low = value & 0xFFFF
    return low - 0x10000 if low > 0x7FFF else low


def _hiword_signed(value: int) -> int:
    high = (value >> 16) & 0xFFFF
    return high - 0x10000 if high > 0x7FFF else high

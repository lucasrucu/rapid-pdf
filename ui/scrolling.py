"""Trackpad-aware wheel scrolling.

Two devices arrive through the same QWheelEvent and they are not the same shape.
A mouse wheel reports angleDelta in 120-unit notches, a handful of events per
flick. A precision trackpad reports pixelDelta: dozens of small events, one per
few pixels of finger travel. Code written for notches treats each of those small
events as a whole notch, which is why two fingers used to fly through a document
while the mouse felt right.

Everything here leaves the mouse-wheel path alone. An angleDelta event falls
straight through to Qt's own handling; only pixelDelta events are handled here,
and they scroll by the pixels the trackpad actually reported.

Qt's own QAbstractScrollArea reads angleDelta and nothing else, so a pixel-only
event scrolls exactly zero without this. Verified against PySide6 6.11:
angleDelta -120 moves a QGraphicsView 117px, pixelDelta -40 with no angleDelta
moves it 0.
"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractScrollArea

# One mouse-wheel notch, in angleDelta units (eighths of a degree).
WHEEL_NOTCH = 120
# Finger travel, in trackpad pixels, that counts as one notch. Close to the
# ~117px a notch scrolls, so the two devices are measured on the same scale
# wherever a decision (turn the page, step the zoom) has to be per-notch.
TRACKPAD_NOTCH_PX = 120


def wheel_pixels(event):
    """The trackpad pixel delta for this wheel event, or None for a mouse wheel.

    Qt fills both deltas on some platforms. pixelDelta wins whenever it carries
    anything, because it is the precise one: the angleDelta on those events is a
    rounded-off estimate of the same movement.
    """
    px = event.pixelDelta()
    if px.x() == 0 and px.y() == 0:
        return None
    return px


def scroll_area_by_pixels(area, pixels) -> None:
    """Scroll a QAbstractScrollArea by an exact pixel offset.

    Qt's sign convention: a positive pixelDelta.y() means the content moved down
    under the finger, so the scrollbar value goes the other way.
    """
    if pixels.y():
        vbar = area.verticalScrollBar()
        vbar.setValue(vbar.value() - pixels.y())
    if pixels.x():
        hbar = area.horizontalScrollBar()
        hbar.setValue(hbar.value() - pixels.x())


class TrackpadScrollFilter(QObject):
    """Gives a scroll area real pixel scrolling on a trackpad.

    Installed on the area's viewport rather than written as a wheelEvent
    override, for two reasons. It is the idiom the combine dialog already uses
    for its own wheel handling, and a viewport filter sits in front of whatever
    wheelEvent the widget itself defines, so a list that grows a Ctrl+wheel zoom
    later does not have to know this exists.

    Ctrl+wheel is passed straight through: that modifier belongs to the zoom
    handlers, and eating it here would break them.
    """

    def __init__(self, area):
        super().__init__(area)
        area.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        # Event type first, and the scroll area read back off the viewport rather
        # than held in an attribute. Every other event this sees costs one
        # comparison, and a filter still installed while its widget is being torn
        # down never touches a reference that has already gone stale.
        if (event.type() == QEvent.Type.Wheel
                and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier)):
            pixels = wheel_pixels(event)
            area = obj.parent() if pixels is not None else None
            if isinstance(area, QAbstractScrollArea):
                scroll_area_by_pixels(area, pixels)
                event.accept()
                return True
        return super().eventFilter(obj, event)

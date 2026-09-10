"""Measure the ghost taken during a REAL gesture: press, slide sideways, tear.

Diagnostic only. `stripDx` is how far the strip is painting the tab away from
the rect `tabRect` reports, which is the defect the drawn ghost is immune to.
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz
from PySide6.QtCore import QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QMouseEvent, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QWidget


def make_pdf(dirname, name):
    path = os.path.join(dirname, name)
    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    page.insert_text((20, 100), name, fontsize=24)
    doc.save(path)
    doc.close()
    return path


def strip_image(bar):
    ratio = max(1.0, float(bar.devicePixelRatioF()))
    rect = bar.rect()
    pix = QPixmap(QSize(int(rect.width() * ratio), int(rect.height() * ratio)))
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    bar.render(pix, QPoint(0, 0), QRegion(rect),
               QWidget.RenderFlag.DrawWindowBackground
               | QWidget.RenderFlag.DrawChildren
               | QWidget.RenderFlag.IgnoreMask)
    return pix.toImage()


def mouse(kind, bar, gpos, buttons=Qt.MouseButton.LeftButton):
    local = QPointF(bar.mapFromGlobal(gpos))
    return QMouseEvent(kind, local, QPointF(gpos), Qt.MouseButton.LeftButton,
                       buttons, Qt.KeyboardModifier.NoModifier)


def main():
    from core.settings import Settings, set_settings
    from ui.document_area import TAB_SHAPE_MARGIN_X, TAB_SHAPE_MARGIN_Y
    from ui.theme import LIGHT, build_qss
    from ui.window_registry import WindowRegistry
    from ui.tab_tear_off import DETACH_MARGIN

    tmp = tempfile.mkdtemp()
    app = QApplication.instance() or QApplication([])
    store = Settings(os.path.join(tmp, "settings.json"), debounce_ms=0,
                     migrate_legacy=False)
    store.close.confirm_multiple_tabs = False
    set_settings(store)
    app.setStyleSheet(build_qss(LIGHT))
    paths = [make_pdf(tmp, f"doc{i}.pdf") for i in range(16)]

    print(f"{'n':>2} {'idx':>3} {'slide':>6} {'title':>7} {'stripDx':>8} "
          f"{'airL':>5} {'airR':>5} {'airT':>5} {'airB':>5} "
          f"{'w':>4} {'wantW':>6} {'h':>4} {'wantH':>6} {'ok':>4}")
    print("-" * 92)

    bad = 0
    for count in (2, 3, 4, 5):
        for index in range(count):
            for slide in (0, -30, 30, -90, 90):
                WindowRegistry.reset_instance()
                registry = WindowRegistry.instance()
                registry.quit_on_last_window = False
                window = registry.create_window(show=False)
                window.resize(1200, 800)
                window.move(100, 100)
                window.show()
                window.open_paths(paths[:count])
                bar = window.document_area().bar()
                app.processEvents()
                tear = bar.tear_off()

                rect0 = bar.tabRect(index)
                start = bar.mapToGlobal(rect0.center())
                bar.mousePressEvent(
                    mouse(QMouseEvent.Type.MouseButtonPress, bar, start))
                app.processEvents()
                if slide:
                    bar.mouseMoveEvent(mouse(
                        QMouseEvent.Type.MouseMove, bar,
                        start + QPoint(slide, 0)))
                    app.processEvents()
                here = (start + QPoint(slide, 0)
                        + QPoint(0, rect0.height() + DETACH_MARGIN + 6))
                bar.mouseMoveEvent(
                    mouse(QMouseEvent.Type.MouseMove, bar, here))
                app.processEvents()

                pixmap = tear._pixmap
                if pixmap is None:
                    print(f"{count:>2} {index:>3} {slide:>6}   no ghost")
                    window.close()
                    continue

                at = window.document_area().index_of(tear._view)
                rect = bar.tabRect(at)
                ghost = pixmap.toImage()
                strip = strip_image(bar)

                # Where the strip is really painting that tab's fill: the
                # longest solid run on the row through its vertical centre.
                y = rect.center().y()
                runs, run = [], None
                for x in range(strip.width()):
                    solid = ((strip.pixel(x, y) >> 24) & 0xFF) > 200
                    if solid and run is None:
                        run = x
                    elif not solid and run is not None:
                        runs.append((run, x - 1))
                        run = None
                if run is not None:
                    runs.append((run, strip.width() - 1))
                fill = max(runs, key=lambda r: r[1] - r[0]) if runs else (-1, -1)

                cols = [x for x in range(ghost.width())
                        if any((ghost.pixel(x, yy) >> 24) & 0xFF
                               for yy in range(ghost.height()))]
                rows = [yy for yy in range(ghost.height())
                        if any((ghost.pixel(x, yy) >> 24) & 0xFF
                               for x in range(ghost.width()))]
                if not cols or not rows:
                    print(f"{count:>2} {index:>3} {slide:>6}   EMPTY GHOST")
                    bad += 1
                    window.close()
                    continue

                air_l = cols[0]
                air_r = ghost.width() - 1 - cols[-1]
                air_t = rows[0]
                air_b = ghost.height() - 1 - rows[-1]
                got_w = cols[-1] - cols[0] + 1
                got_h = rows[-1] - rows[0] + 1
                # The QSS shape, plus the pixel the antialiased corner radius
                # covers outside it on each side.
                want_w = rect.width() - 2 * TAB_SHAPE_MARGIN_X + 2
                want_h = rect.height() - 2 * TAB_SHAPE_MARGIN_Y + 2
                ok = (got_w == want_w and got_h == want_h
                      and min(air_l, air_r, air_t, air_b) >= 1)
                bad += 0 if ok else 1
                print(f"{count:>2} {index:>3} {slide:>6} "
                      f"{tear.carried_title():>7} {fill[0] - rect.x():>8} "
                      f"{air_l:>5} {air_r:>5} {air_t:>5} {air_b:>5} "
                      f"{got_w:>4} {want_w:>6} {got_h:>4} {want_h:>6} "
                      f"{'ok' if ok else 'BAD':>4}")

                bar.mouseReleaseEvent(mouse(
                    QMouseEvent.Type.MouseButtonRelease, bar, here,
                    Qt.MouseButton.NoButton))
                app.processEvents()
                window.close()
        print()
    print(f"{bad} bad")


if __name__ == "__main__":
    main()

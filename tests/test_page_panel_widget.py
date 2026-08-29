"""The page strip's own behaviour: selection, keys, and where a drop lands.

Qt's real drag loop can't run in a test (QDrag.exec hands control to the
platform), so the drop is delivered the way Qt would deliver it, through
_PageList.dropEvent, with a stand-in event carrying the position and the source.
Everything either side of that is the genuine widget: the selection model, the
key handling, the signals, the delegate's cell geometry.
"""

import pytest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from ui.page_panel import PagePanel


class _FakeDrop:
    """What dropEvent reads off a real QDropEvent, and nothing else."""

    def __init__(self, source, pos: QPoint):
        self._source = source
        self._pos = pos
        self.accepted = False
        self.ignored = False

    def source(self):
        return self._source

    def pos(self):
        return self._pos

    def acceptProposedAction(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


class _FakeDoc:
    """Just enough document for the strip: a page count and blank thumbnails."""

    def __init__(self, count: int):
        self._count = count
        self.doc = object()

    def page_count(self):
        return self._count

    def get_page_size(self, page_num):
        return (200.0, 260.0)

    def render_thumbnail(self, page_num, max_width=110):
        from PySide6.QtGui import QPixmap
        pm = QPixmap(max_width, max_width)
        pm.fill(Qt.GlobalColor.white)
        return pm


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qt_app):
    p = PagePanel()
    p.resize(150, 700)
    p.set_document(_FakeDoc(6))
    p.show()
    QApplication.processEvents()
    # Cell sizing is debounced off a viewport resize, which never lands in a
    # test. Without this the cells keep their pre-show width and every drop
    # position below is measured against geometry the user would never see.
    p._apply_layout()
    yield p
    p.close()
    p.deleteLater()


def _row_center(panel, row: int) -> QPoint:
    return panel._list.visualItemRect(panel._list.item(row)).center()


def _row_bottom(panel, row: int) -> QPoint:
    rect = panel._list.visualItemRect(panel._list.item(row))
    return QPoint(rect.center().x(), rect.bottom() - 1)


def _row_top(panel, row: int) -> QPoint:
    rect = panel._list.visualItemRect(panel._list.item(row))
    return QPoint(rect.center().x(), rect.top() + 1)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_the_strip_allows_more_than_one_page_to_be_selected(panel):
    from PySide6.QtWidgets import QAbstractItemView
    assert (panel._list.selectionMode()
            is QAbstractItemView.SelectionMode.ExtendedSelection)


def test_a_shift_range_reports_every_row_in_it(panel):
    panel._list.setCurrentRow(1)
    panel._list.item(2).setSelected(True)
    panel._list.item(3).setSelected(True)
    assert panel.selected_rows() == [1, 2, 3]


def test_ctrl_toggling_a_page_out_leaves_a_gap(panel):
    panel._list.clearSelection()
    for row in (1, 2, 3):
        panel._list.item(row).setSelected(True)
    panel._list.item(2).setSelected(False)
    assert panel.selected_rows() == [1, 3]


def test_the_header_counts_a_multi_selection(panel):
    panel._list.item(0).setSelected(True)
    panel._list.item(1).setSelected(True)
    assert panel._title.text() == "2 selected"


def test_the_header_goes_back_to_its_label_for_one_page(panel):
    panel._list.setCurrentRow(2)
    assert panel._title.text() == "Pages"


def test_the_strip_carries_no_delete_button(panel):
    """The dedicated delete button is gone from the strip. Deleting stays on the
    Delete key, the right-click menu and the Organizer."""
    from PySide6.QtWidgets import QAbstractButton
    assert not hasattr(panel, "_del_btn")
    assert panel.findChildren(QAbstractButton) == []


def test_syncing_the_page_does_not_collapse_a_multi_selection(panel):
    """The page-change round trip a shift-click causes must not undo it."""
    panel._list.setCurrentRow(1)
    panel._list.item(2).setSelected(True)
    panel._list.item(3).setSelected(True)
    panel.set_current_page(1)          # what the host echoes back
    assert panel.selected_rows() == [1, 2, 3]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_key_asks_for_the_whole_selection(panel):
    asked = []
    panel.pages_delete_requested.connect(asked.append)
    panel._list.clearSelection()
    panel._list.item(1).setSelected(True)
    panel._list.item(4).setSelected(True)
    panel._list.keyPressEvent(
        QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Delete,
                  Qt.KeyboardModifier.NoModifier))
    assert asked == [[1, 4]]


def test_backspace_deletes_too(panel):
    asked = []
    panel.pages_delete_requested.connect(asked.append)
    panel._list.setCurrentRow(3)
    panel._list.keyPressEvent(
        QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Backspace,
                  Qt.KeyboardModifier.NoModifier))
    assert asked == [[3]]


def test_the_context_menu_still_asks_for_the_selection(panel):
    """With the button gone, right-click is the discoverable way to delete."""
    asked = []
    panel.pages_delete_requested.connect(asked.append)
    panel._list.setCurrentRow(2)
    menu = panel._build_context_menu()
    labels = [a.text() for a in menu.actions() if a.text()]
    assert "Delete Page" in labels
    next(a for a in menu.actions() if a.text() == "Delete Page").trigger()
    assert asked == [[2]]


def test_nothing_selected_asks_for_nothing(panel):
    asked = []
    panel.pages_delete_requested.connect(asked.append)
    panel._list.clearSelection()
    panel._request_delete()
    assert asked == []


# ---------------------------------------------------------------------------
# Drop position and the order it produces
# ---------------------------------------------------------------------------

def test_dropping_below_a_row_midpoint_inserts_after_it(panel):
    assert panel._list._drop_row(_row_bottom(panel, 2)) == 3


def test_dropping_above_a_row_midpoint_inserts_before_it(panel):
    assert panel._list._drop_row(_row_top(panel, 2)) == 2


def test_dropping_past_the_last_row_appends(panel):
    below = QPoint(20, panel._list.viewport().height() + 400)
    assert panel._list._drop_row(below) == 6


def test_a_drag_reports_the_order_the_document_should_take(panel):
    got = []
    panel.pages_reorder_requested.connect(lambda o, r: got.append((o, r)))
    panel._list.setCurrentRow(0)
    drop = _FakeDrop(panel._list, _row_bottom(panel, 2))
    panel._list.dropEvent(drop)
    assert drop.accepted
    assert got == [([1, 2, 0, 3, 4, 5], [0])]


def test_a_multi_page_drag_moves_the_whole_selection_together(panel):
    got = []
    panel.pages_reorder_requested.connect(lambda o, r: got.append((o, r)))
    panel._list.clearSelection()
    panel._list.item(0).setSelected(True)
    panel._list.item(2).setSelected(True)
    drop = _FakeDrop(panel._list, _row_bottom(panel, 5))
    panel._list.dropEvent(drop)
    order, rows = got[0]
    assert rows == [0, 2]
    assert order == [1, 3, 4, 5, 0, 2]      # the pair lands together, in order


def test_dropping_a_page_back_where_it_started_asks_for_nothing(panel):
    got = []
    panel.pages_reorder_requested.connect(lambda o, r: got.append(o))
    panel._list.setCurrentRow(2)
    panel._list.dropEvent(_FakeDrop(panel._list, _row_center(panel, 2)))
    assert got == []


def test_a_drop_from_somewhere_else_is_refused(panel):
    got = []
    panel.pages_reorder_requested.connect(lambda o, r: got.append(o))
    panel._list.setCurrentRow(0)
    drop = _FakeDrop(object(), _row_bottom(panel, 3))
    panel._list.dropEvent(drop)
    assert drop.ignored and not drop.accepted
    assert got == []


def test_the_insertion_indicator_tracks_the_drop_row(panel):
    assert panel._list.drag_target_row() is None
    panel._list._drag_row = 3
    assert panel._list.drag_target_row() == 3
    panel._list._clear_drag()
    assert panel._list.drag_target_row() is None


# ---------------------------------------------------------------------------
# The strip is rebuilt from the document, never edited in place
# ---------------------------------------------------------------------------

def test_a_drop_leaves_the_widget_untouched_until_the_document_says_so(panel):
    """Widget order and page order can't drift if the widget never moves rows."""
    before = [panel._list.item(i).text() for i in range(panel._list.count())]
    panel._list.setCurrentRow(0)
    panel._list.dropEvent(_FakeDrop(panel._list, _row_bottom(panel, 4)))
    after = [panel._list.item(i).text() for i in range(panel._list.count())]
    assert after == before


def test_refresh_restores_the_page_and_the_moved_block(panel):
    panel.refresh(current_page=2, select=[2, 3])
    assert panel._list.currentRow() == 2
    assert panel.selected_rows() == [2, 3]


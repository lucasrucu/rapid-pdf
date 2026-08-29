"""The window-level view controls: the status bar's fit group and the pan tool.

The lone text "Fit" button only ever meant fit-page, and nothing said so. It is
now four icons in one exclusive group, each naming its mode in the tooltip.

A real MainWindow is built here because what is under test is the wiring: the
group to the canvas, the canvas's fit_mode_broken back to the group, and the
tool rail to the canvas. Nothing runs the event loop, so the deferred update
check never fires.
"""


import pytest

from PySide6.QtWidgets import QApplication

from ui.canvas import FIT_MODES
from ui.main_window import _FIT_CONTROLS, MainWindow


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app):
    w = MainWindow()
    yield w
    w._force_quit = True
    w.close()
    w.deleteLater()


def test_the_group_offers_every_mode_the_canvas_knows(window):
    assert set(window._fit_btns) == set(FIT_MODES)


def test_the_old_text_fit_button_is_gone(window):
    assert not hasattr(window, "_fit_btn")


def test_every_mode_names_itself_in_a_tooltip(window):
    tips = {m: window._fit_btns[m].toolTip() for m in window._fit_btns}
    assert tips == {
        "fit_page": "Fit page",
        "fit_width": "Fit width",
        "fit_height": "Fit height",
        "actual": "100% (actual size)",
    }


def test_the_control_table_and_the_canvas_agree_on_the_names():
    """These names also have to match core.settings' view.default_fit_mode."""
    assert [m for m, _, _, _ in _FIT_CONTROLS] == [
        "fit_page", "fit_width", "fit_height", "actual"]
    assert set(FIT_MODES) == {m for m, _, _, _ in _FIT_CONTROLS}


def test_only_one_mode_can_be_active(window):
    window._fit_btns["fit_page"].click()
    window._fit_btns["fit_width"].click()
    checked = [m for m, b in window._fit_btns.items() if b.isChecked()]
    assert checked == ["fit_width"]


def test_clicking_a_mode_tells_the_canvas(window):
    window._fit_btns["fit_height"].click()
    assert window.view._canvas.fit_mode() == "fit_height"


def test_nothing_is_active_until_a_mode_is_chosen(window):
    assert window._fit_group.checkedButton() is None
    assert window.view._canvas.fit_mode() is None


def test_a_manual_zoom_puts_the_active_mode_out(window):
    """The canvas breaks the fit when the user zooms; the group has to follow."""
    window._fit_btns["fit_page"].click()
    assert window._fit_group.checkedButton() is not None
    window.view._canvas.fit_mode_broken.emit()
    assert window._fit_group.checkedButton() is None


def test_the_group_can_be_used_again_after_a_break(window):
    """Clearing an exclusive group is fiddly; make sure it stays usable."""
    window._fit_btns["fit_page"].click()
    window.view._canvas.fit_mode_broken.emit()
    window._fit_btns["actual"].click()
    assert window.view._canvas.fit_mode() == "actual"
    assert window._fit_btns["actual"].isChecked()


def test_the_pan_tool_is_on_the_rail_and_reaches_the_canvas(window):
    """Item 2's toolbar toggle, wired end to end."""
    assert "pan" in window.view._toolbar._tool_btns
    window.view._toolbar.trigger_tool("pan")
    assert window.view._canvas.is_panning()
    window.view._toolbar.trigger_tool("select")
    assert not window.view._canvas.is_panning()

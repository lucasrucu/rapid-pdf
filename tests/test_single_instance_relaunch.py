"""Relaunching Rapid PDF while it is already running raises the window.

The bug this covers: `InstanceServer._flush` only emitted `batch_ready` when
the aggregated batch had files in it. Double-clicking the shortcut (or the
Start menu entry) launches a second process with no files at all; that process
handed its empty payload to the primary and exited, the primary dropped it, and
nothing happened. From the user's side the app simply refused to open.

The aggregation window is driven straight through `add_launch` here rather than
over the local socket. The socket path is the same two lines either side of it
and needs two live processes to exercise, while the decision being tested is
entirely inside `_flush`.
"""

import pytest

from PySide6.QtCore import QCoreApplication, QElapsedTimer
from PySide6.QtWidgets import QApplication

from core.single_instance import AGGREGATE_MS, InstanceServer, parse_cli


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def server(qt_app):
    """A server object without a listening socket: only the aggregation and
    flush logic is under test, and listening would collide with a real app."""
    s = InstanceServer()
    yield s
    s.deleteLater()


@pytest.fixture
def batches(server):
    seen = []
    server.batch_ready.connect(lambda files, combine: seen.append((files, combine)))
    return seen


def _wait_for_flush(batches, timeout_ms=AGGREGATE_MS * 6):
    clock = QElapsedTimer()
    clock.start()
    while clock.elapsed() < timeout_ms:
        QCoreApplication.processEvents()
        if batches:
            return True
    return False


@pytest.fixture
def pdf(tmp_path):
    import fitz

    path = tmp_path / "one.pdf"
    raw = fitz.open()
    raw.new_page(width=200, height=200)
    raw.save(str(path))
    raw.close()
    return str(path)


# ---------------------------------------------------------------------------

def test_a_launch_with_no_files_still_raises_the_window(server, batches):
    """Double-clicking the shortcut while the app runs. The batch is empty and
    it must still be emitted, because handle_cli_files raises the window before
    it looks at the file list."""
    server.add_launch([], False)
    assert _wait_for_flush(batches), "the empty relaunch was swallowed"
    assert batches == [([], False)]


def test_a_launch_with_files_still_carries_them(server, batches, pdf):
    server.add_launch([pdf], False)
    assert _wait_for_flush(batches)
    assert batches == [([pdf], False)]


def test_files_that_no_longer_exist_drop_out_but_the_launch_survives(
        server, batches, tmp_path):
    """A path that vanished between the launch and the flush is not a reason to
    ignore the launch: the window still comes forward, just empty-handed."""
    server.add_launch([str(tmp_path / "gone.pdf")], False)
    assert _wait_for_flush(batches)
    assert batches == [([], False)]


def test_no_launch_at_all_emits_nothing(server, batches):
    """The timer only runs because add_launch started it, so a bare flush must
    stay silent rather than raising the window at random."""
    server._flush()
    assert batches == []


def test_a_burst_of_launches_is_one_batch(server, batches, pdf):
    """Explorer fires the context-menu verb once per selected file."""
    server.add_launch([pdf], False)
    server.add_launch([], True)
    assert _wait_for_flush(batches)
    assert batches == [([pdf], True)]


def test_the_pending_state_is_cleared_between_batches(server, batches, pdf):
    server.add_launch([pdf], True)
    assert _wait_for_flush(batches)
    batches.clear()

    server.add_launch([], False)
    assert _wait_for_flush(batches)
    assert batches == [([], False)]      # not still carrying the earlier file


def test_a_bare_relaunch_parses_as_no_files_and_no_combine():
    """What the second process actually has on its command line."""
    assert parse_cli([r"C:\Program Files\Rapid PDF\rapid-pdf.exe"]) == ([], False)

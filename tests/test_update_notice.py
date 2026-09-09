"""The update strip: does it stay out of the way, and does it say the right thing.

No network and no threads here. The strip's own logic is what is under test:
it is hidden until there is something to offer, it says what is on offer, it
offers the right thing for the kind of install it is running in, Later hides it
and throws the download away, and it follows the theme like every other widget
in the app. The fetching and the running are tested in tests/test_update.py,
which never touches Qt.
"""

import json

import pytest
from PySide6.QtWidgets import QApplication

from core.update import client
from core.update.release import parse_latest
from ui import update_notice as notice_mod
from ui.theme import DARK, LIGHT
from ui.update_notice import UpdateNotice, _StageWorker

from test_update import release_payload


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def an_update(version="1.4.0", running="1.3.0"):
    return client.UpdateInfo(
        release=parse_latest(json.dumps(release_payload(version))),
        running=running)


def test_it_starts_hidden(qt_app):
    notice = UpdateNotice()
    assert not notice.isVisible()


def test_nothing_to_offer_leaves_it_hidden(qt_app):
    # The startup case on a machine with no network: check() answered None and
    # the user must never learn that anything was attempted.
    notice = UpdateNotice()
    notice._on_check_done(None)
    assert not notice.isVisible()
    assert notice._label.text() == ""


def test_an_offer_names_the_version_and_the_size(qt_app):
    notice = UpdateNotice()
    notice._on_check_done(an_update("1.4.0", "1.3.0"))
    text = notice._label.text()
    assert "1.4.0" in text
    assert "1.3.0" in text
    assert "MB" in text


def test_later_hides_it_and_throws_the_download_away(qt_app):
    notice = UpdateNotice()
    notice._on_check_done(an_update())

    discarded = []
    notice._staged = type("Staged", (), {"discard": lambda self: discarded.append(1)})()
    notice._dismiss()

    assert not notice.isVisible()
    assert discarded == [1], "a large folder was left beside the install"
    assert notice._staged is None


@pytest.fixture
def no_browser(monkeypatch):
    """Catch the release page instead of opening one in a real browser."""
    opened = []
    monkeypatch.setattr(notice_mod.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()))
    return opened


@pytest.mark.parametrize("kind, label", [
    (client.INSTALLED, "Update now"),
    (client.PORTABLE, "Download"),
    (client.SOURCE, "View release"),
])
def test_the_button_says_what_this_kind_of_install_can_actually_do(
        qt_app, monkeypatch, kind, label):
    # Only an installed copy updates itself. Offering "Update now" to a
    # portable copy would be offering something that cannot work.
    monkeypatch.setattr(notice_mod.client, "install_kind", lambda *a: kind)
    notice = UpdateNotice()
    notice._on_check_done(an_update())
    assert notice._action.text() == label


def test_a_portable_copy_is_sent_to_the_page_and_told_what_to_do(
        qt_app, monkeypatch, no_browser):
    # The whole portable path: nothing is downloaded, nothing is staged, the
    # release page opens and the strip stays up carrying the other half of the
    # instruction. Running the installer here would build a SECOND install.
    monkeypatch.setattr(notice_mod.client, "install_kind",
                        lambda *a: client.PORTABLE)
    monkeypatch.setattr(notice_mod.client, "install_dir",
                        lambda: "C:\\somewhere\\RapidPDF")
    monkeypatch.setattr(notice_mod.client, "stage",
                        lambda *a, **k: pytest.fail(
                            "a portable copy must not download an installer"))
    notice = UpdateNotice()
    notice._on_check_done(an_update())
    notice._on_action()

    assert no_browser and "releases" in no_browser[0]
    assert notice._staged is None
    assert "unzip" in notice._label.text().lower()


def test_running_from_source_opens_the_page_and_gets_out_of_the_way(
        qt_app, monkeypatch, no_browser):
    monkeypatch.setattr(notice_mod.client, "install_dir", lambda: None)
    notice = UpdateNotice()
    notice._on_check_done(an_update())
    notice._on_action()

    assert no_browser
    assert not notice.isVisible()


def test_it_follows_the_theme(qt_app):
    notice = UpdateNotice()
    for palette in (LIGHT, DARK):
        notice.apply_palette(palette)
        qss = notice.styleSheet()
        assert palette.surface in qss
        assert palette.accent in qss, "the strip lost the one accent it carries"
        assert "qlineargradient" not in qss


def test_a_running_check_is_not_started_twice(qt_app):
    # The Help menu while the startup check is still in flight. A second
    # thread would be started and the first one leaked.
    notice = UpdateNotice()
    sentinel = object()
    notice._thread, notice._worker = object(), sentinel
    notice.start_check(manual=True)
    assert notice._worker is sentinel, "a second check thread was started"


def test_cancelling_stops_the_download_at_the_next_chunk(qt_app):
    # Closing the app mid-download. A read loop of tens of MB does not notice
    # a thread being asked to quit, so the stop goes through the progress
    # callback instead: it raises, and client.stage cleans up after itself
    # (tests/test_update.py pins that half).
    worker = _StageWorker(an_update(), "nowhere")
    worker._tick(1, 2, "downloading")           # not cancelled: fine
    worker.cancel()
    with pytest.raises(client.UpdateError):
        worker._tick(2, 2, "downloading")


def test_shutdown_is_safe_when_nothing_is_running(qt_app):
    notice = UpdateNotice()
    notice.shutdown()
    notice.shutdown()


def test_shutdown_survives_a_worker_with_nothing_connected(qt_app):
    # The intermittent that came out of a suite run under load on 9 Sept 2026.
    # A no-argument disconnect() on a worker with no connections raises
    # TypeError, not RuntimeError, and shutdown() runs inside closeEvent, so
    # letting it out means the app fails to close.
    notice = UpdateNotice()
    worker = _StageWorker(an_update(), "nowhere")
    notice._worker, notice._thread = worker, None
    notice.shutdown()             # must not raise
    assert notice._worker is None


def test_a_staged_update_is_not_re_downloaded(qt_app):
    notice = UpdateNotice()
    notice._staged, notice._worker = object(), None
    notice.start_check()
    assert notice._worker is None

"""Where a file dialog opens, which until now was "wherever the process was".

Every QFileDialog in the app passed "" as its starting directory. Qt does not
read that as "no preference", it reads it as the process working directory, so
the Open dialog landed on the desktop for a shortcut launch and on whatever
folder Explorer was showing for a context-menu launch. `files.default_folder`
and `files.default_folder_mode` shipped in the schema with no reader; these are
the reader.

The two modes share one key on purpose (see core/settings.py): the folder to
start in is the same thing either way, the only difference is who writes it.
"""

import pytest

from core.settings import (
    Settings, dialog_start_dir, remember_dialog_dir, set_settings,
)


@pytest.fixture
def store(tmp_path):
    fresh = Settings(tmp_path / "settings.json", debounce_ms=0,
                     migrate_legacy=False)
    previous = set_settings(fresh)
    yield fresh
    set_settings(previous)


def test_a_fresh_install_has_nothing_to_offer(store):
    """"" is what Qt gets, which is what it got before. The point is that it
    stops being the answer as soon as the user has opened anything."""
    assert dialog_start_dir() == ""


def test_the_remembered_folder_is_used(store, tmp_path):
    store.files.default_folder = str(tmp_path)
    assert dialog_start_dir() == str(tmp_path)


def test_a_file_path_resolves_to_its_folder(store, tmp_path):
    """Callers pass the open document's path, not its folder."""
    doc = tmp_path / "drawing.pdf"
    doc.write_bytes(b"")
    assert dialog_start_dir(doc) == str(tmp_path)


def test_the_fallback_only_matters_when_nothing_is_remembered(store, tmp_path):
    remembered = tmp_path / "remembered"
    remembered.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    assert dialog_start_dir(other) == str(other)
    store.files.default_folder = str(remembered)
    assert dialog_start_dir(other) == str(remembered)


def test_a_folder_that_has_gone_is_skipped(store, tmp_path):
    """A remembered path on a drive that is no longer there must not make the
    dialog refuse to open somewhere sensible."""
    store.files.default_folder = str(tmp_path / "unplugged")
    fallback = tmp_path / "still-here"
    fallback.mkdir()
    assert dialog_start_dir(fallback) == str(fallback)


def test_a_remembered_folder_never_resolves_to_its_parent(store, tmp_path):
    """A folder is a folder. If the remembered one has gone, its parent is a
    place the user never chose, so it is not an answer."""
    store.files.default_folder = str(tmp_path / "unplugged")
    assert dialog_start_dir() == ""


def test_nothing_usable_at_all_is_still_the_empty_string(store, tmp_path):
    store.files.default_folder = str(tmp_path / "gone")
    assert dialog_start_dir(tmp_path / "gone" / "also-gone.pdf") == ""


# ---------------------------------------------------------------------------
# Remembering
# ---------------------------------------------------------------------------

def test_last_used_records_the_folder_of_the_chosen_file(store, tmp_path):
    chosen = tmp_path / "picked.pdf"
    chosen.write_bytes(b"")
    remember_dialog_dir(chosen)
    assert store.files.default_folder == str(tmp_path)
    assert dialog_start_dir() == str(tmp_path)


def test_a_fixed_folder_is_never_overwritten_by_a_dialog(store, tmp_path):
    fixed = tmp_path / "fixed"
    fixed.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    store.files.default_folder_mode = "fixed"
    store.files.default_folder = str(fixed)
    remember_dialog_dir(elsewhere / "opened.pdf")
    assert store.files.default_folder == str(fixed)


def test_a_cancelled_dialog_records_nothing(store, tmp_path):
    store.files.default_folder = str(tmp_path)
    remember_dialog_dir("")
    remember_dialog_dir(None)
    assert store.files.default_folder == str(tmp_path)


def test_a_path_that_does_not_exist_records_nothing(store, tmp_path):
    store.files.default_folder = str(tmp_path)
    remember_dialog_dir(tmp_path / "gone" / "file.pdf")
    assert store.files.default_folder == str(tmp_path)


def test_it_survives_a_reload(store, tmp_path):
    """The first dialog of the NEXT run is the one this is for."""
    chosen = tmp_path / "picked.pdf"
    chosen.write_bytes(b"")
    remember_dialog_dir(chosen)
    store.flush()

    reloaded = Settings(store.path, debounce_ms=0, migrate_legacy=False)
    assert reloaded.files.default_folder == str(tmp_path)

"""Password protected PDFs: opening them, and never keeping the password.

WHAT WAS WRONG. `PDFDocument.open` refused every encrypted file outright with
"This PDF is password protected, so it cannot be opened" and offered no way to
give it one. Client-issued certificate packages arrive protected, so the app
could not read the files it exists to read.

FOUR THINGS ARE PINNED HERE, and the third is the one worth the file.

1. THE ROUND TRIP. An encrypted file is detected, the right password opens it,
   the wrong one does not, and the tries run out rather than looping forever.

2. THE TWO KINDS OF PASSWORD. A user password locks the file: nothing can be
   read until it is given. An owner password does not lock anything, it
   withholds permissions, and such a file has to OPEN, with what it withholds
   reported rather than ignored. Measured on PyMuPDF 1.27.2.3: an owner-only
   file reports `needs_pass` 0 and a `permissions` bit field with the withheld
   bits clear, so neither `needs_pass` nor `is_encrypted` can be the test for
   "is this file protected". The metadata's `encryption` entry is, and it
   survives authentication, which the other two do not.

3. THE PASSWORD IS NOWHERE. Not on the document object, not in the settings
   store, not in the session record, not in `last_open_error`, not in the
   repr of anything. `test_password_is_not_in_anything_the_app_keeps` walks
   the object's own state and the whole settings file looking for it, and
   `test_failed_unlock_message_never_repeats_the_password` covers the near
   miss: a message that helpfully says which password did not work.

4. SAVING DOES NOT UNDO THE PROTECTION. A plain `doc.save()` writes an
   UNENCRYPTED file, so every save this app has ever done would have published
   a protected package in the clear. Every write now passes
   `encryption=PDF_ENCRYPT_KEEP`, and an encrypted file saved in place goes out
   incrementally so the handle is never closed and the user is never asked for
   the password a second time.

THE FIXTURES ARE REAL ENCRYPTION, not a stand-in. PyMuPDF writes AES-256 itself,
so these files are genuinely protected and MuPDF genuinely refuses them without
the password.
"""

import json
import os

import fitz
import pytest

from PySide6.QtWidgets import QApplication

from core.pdf_document import (
    AUTH_OWNER,
    AUTH_USER,
    PASSWORD_REQUIRED,
    SAVE_MODE_INCREMENTAL,
    UNLOCK_ATTEMPT_LIMIT,
    PDFDocument,
    denied_permissions,
)

USER_PW = "correct-horse-battery"
OWNER_PW = "owner-of-the-file"

RESTRICTED = int(fitz.PDF_PERM_PRINT | fitz.PDF_PERM_ACCESSIBILITY)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    """QPixmap needs one. `render_page` is asserted on below, and building a
    QPixmap with no application crashes the interpreter rather than raising."""
    yield QApplication.instance() or QApplication([])


def _build(path, pages=3, user=None, owner=None, permissions=None):
    """A small PDF, optionally encrypted. `user=None and owner=None` is plain."""
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"page {index}")
    kwargs = {}
    if user is not None or owner is not None:
        kwargs["encryption"] = fitz.PDF_ENCRYPT_AES_256
        kwargs["permissions"] = (RESTRICTED if permissions is None else permissions)
        if user is not None:
            kwargs["user_pw"] = user
        if owner is not None:
            kwargs["owner_pw"] = owner
    doc.save(str(path), **kwargs)
    doc.close()
    return str(path)


@pytest.fixture
def locked(tmp_path):
    """A file that cannot be read at all without the user password."""
    return _build(tmp_path / "locked.pdf", user=USER_PW, owner=OWNER_PW)


@pytest.fixture
def owner_only(tmp_path):
    """A file that opens freely but withholds permissions. The common case for
    a client-issued certificate: readable, not editable."""
    return _build(tmp_path / "owner.pdf", user="", owner=OWNER_PW)


# ---------------------------------------------------------------------------
# Detection and the password round trip
# ---------------------------------------------------------------------------

def test_encrypted_file_is_detected_and_does_not_open_yet(locked):
    pdf = PDFDocument()
    assert pdf.open(locked) is False
    assert pdf.needs_password() is True
    assert pdf.is_open() is False
    assert pdf.doc is None, "a locked handle must not sit in .doc, where every render trusts it"
    assert pdf.locked_path() == locked
    assert pdf.last_open_error == PASSWORD_REQUIRED
    pdf.close()


def test_right_password_opens_it_and_it_renders(locked):
    pdf = PDFDocument()
    pdf.open(locked)
    assert pdf.unlock(USER_PW) is True
    assert pdf.is_open() is True
    assert pdf.needs_password() is False
    assert pdf.page_count() == 3
    assert pdf.path == locked
    # The bug this whole check exists for: a locked document reports a page
    # count and then raises on the first pixmap. Render one.
    assert not pdf.render_page(0, 1.0).isNull()
    assert "page 0" in pdf.doc[0].get_text()
    pdf.close()


def test_wrong_password_is_rejected_and_the_file_stays_locked(locked):
    pdf = PDFDocument()
    pdf.open(locked)
    assert pdf.unlock("not it") is False
    assert pdf.needs_password() is True, "still waiting, with tries left"
    assert pdf.is_open() is False
    assert pdf.failed_unlock_attempts == 1
    # And the right one still works afterwards.
    assert pdf.unlock(USER_PW) is True
    assert pdf.failed_unlock_attempts == 0
    pdf.close()


def test_the_tries_run_out_rather_than_looping_forever(locked):
    pdf = PDFDocument()
    pdf.open(locked)
    for attempt in range(UNLOCK_ATTEMPT_LIMIT):
        assert pdf.unlock(f"wrong {attempt}") is False
    assert pdf.needs_password() is False, (
        "needs_password has to go false when the tries run out, or a "
        "`while needs_password()` prompt loop never ends")
    assert pdf.is_open() is False
    assert str(UNLOCK_ATTEMPT_LIMIT) in pdf.last_open_error
    pdf.close()


def test_cancelling_leaves_the_document_empty(locked):
    pdf = PDFDocument()
    pdf.open(locked)
    pdf.cancel_unlock()
    assert pdf.needs_password() is False
    assert pdf.is_open() is False
    assert pdf.doc is None
    pdf.close()


def test_owner_password_also_opens_it_and_lifts_the_restrictions(locked):
    pdf = PDFDocument()
    pdf.open(locked)
    assert pdf.unlock(OWNER_PW) is True
    assert pdf.opened_as_owner() is True
    assert pdf.denied_operations() == [], "the owner password grants everything"
    pdf.close()

    other = PDFDocument()
    other.open(locked)
    other.unlock(USER_PW)
    assert other.opened_as_owner() is False
    assert other.denied_operations(), "the user password leaves the restrictions on"
    other.close()


def test_unlock_codes_are_the_measured_ones(locked):
    """The constants are documentation of MuPDF's answer, so check them."""
    doc = fitz.open(locked)
    assert doc.authenticate(USER_PW) == AUTH_USER
    doc.close()
    doc = fitz.open(locked)
    assert doc.authenticate(OWNER_PW) == AUTH_OWNER
    doc.close()


# ---------------------------------------------------------------------------
# The owner-password document: it OPENS
# ---------------------------------------------------------------------------

def test_owner_password_document_opens_without_a_prompt(owner_only):
    pdf = PDFDocument()
    assert pdf.open(owner_only) is True, (
        "a document restricted by an owner password opens fine everywhere "
        "else and must open here")
    assert pdf.needs_password() is False
    assert pdf.page_count() == 3
    assert not pdf.render_page(0, 1.0).isNull()
    pdf.close()


def test_owner_password_restrictions_are_reported_not_ignored(owner_only):
    pdf = PDFDocument()
    pdf.open(owner_only)
    info = pdf.encryption_info()
    assert info.encrypted is True
    assert info.needed_password is False
    assert info.owner_access is False
    assert info.restricted is True
    assert "changing the content" in info.denied
    assert "copying text out" in info.denied
    assert "printing" not in info.denied, "the fixture grants printing"
    assert info.notice and "does not allow" in info.notice
    pdf.close()


def test_a_plain_document_reports_no_protection_at_all(tmp_path):
    pdf = PDFDocument()
    pdf.open(_build(tmp_path / "plain.pdf"))
    info = pdf.encryption_info()
    assert (info.encrypted, info.needed_password, info.restricted) == (False, False, False)
    assert info.notice is None
    assert pdf.is_encrypted() is False
    pdf.close()


def test_is_encrypted_survives_authentication(locked):
    """`doc.is_encrypted` goes FALSE once the password is accepted, which is
    why it cannot be the test. This one has to stay true."""
    pdf = PDFDocument()
    pdf.open(locked)
    pdf.unlock(USER_PW)
    assert pdf.doc.is_encrypted is False, "the PyMuPDF flag this must not be built on"
    assert pdf.is_encrypted() is True
    pdf.close()


def test_denied_permissions_names_every_restriction(owner_only):
    doc = fitz.open(owner_only)
    denied = denied_permissions(doc)
    assert "changing the content" in denied
    assert "adding, removing or reordering pages" in denied
    doc.close()


# ---------------------------------------------------------------------------
# The password is nowhere
# ---------------------------------------------------------------------------

def _all_strings(value, depth=0):
    """Every string reachable from `value`, for the leak hunt below."""
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, (bytes, bytearray)):
        yield value.decode("utf-8", "replace")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _all_strings(key, depth + 1)
            yield from _all_strings(item, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _all_strings(item, depth + 1)


def test_password_is_not_in_anything_the_app_keeps(locked):
    pdf = PDFDocument()
    pdf.open(locked)
    assert pdf.unlock(USER_PW) is True

    haystack = list(_all_strings(vars(pdf)))
    haystack.append(repr(pdf))
    haystack.append(str(pdf.__dict__))
    haystack.append(pdf.last_open_error or "")
    haystack.append(pdf.last_save_error or "")
    haystack.append(str(pdf.save_plan()))
    haystack.append(str(pdf.encryption_info()))
    leaked = [s for s in haystack if USER_PW in s]
    assert not leaked, f"the password reached {leaked}"
    pdf.close()


def test_failed_unlock_message_never_repeats_the_password(locked):
    pdf = PDFDocument()
    pdf.open(locked)
    typo = "corect-horse-battery"
    assert pdf.unlock(typo) is False
    assert typo not in (pdf.last_open_error or "")
    assert USER_PW not in (pdf.last_open_error or "")
    # And the give-up message too, which is built on a different branch.
    for _ in range(UNLOCK_ATTEMPT_LIMIT):
        pdf.unlock(typo)
    assert typo not in (pdf.last_open_error or "")
    pdf.close()


def test_the_settings_file_never_learns_the_password(locked, tmp_path):
    """The whole store on disk, read back as text. Session restore writes
    through this, so if a password could reach the session it would be here."""
    from core.settings import settings

    pdf = PDFDocument()
    pdf.open(locked)
    pdf.unlock(USER_PW)
    store = settings()
    store.session.windows = [{
        "geometry": [0, 0, 800, 600], "screen": None, "current": 0,
        "tabs": [{"path": pdf.path, "page": 0, "zoom": 1.0,
                  "raster_scale": 1.0, "fit_mode": None}],
    }]
    try:
        store.flush()
    except AttributeError:
        pass
    raw = ""
    for candidate in (getattr(store, "path", None), getattr(store, "_path", None)):
        if candidate and os.path.exists(str(candidate)):
            raw = open(str(candidate), encoding="utf-8").read()
            break
    assert raw, "the settings store did not write a file to inspect"
    assert USER_PW not in raw
    assert OWNER_PW not in raw
    # And the record itself, whatever the file looked like.
    assert USER_PW not in json.dumps(store.session.windows)
    store.session.windows = []
    pdf.close()


def test_session_restore_of_an_encrypted_file_re_prompts(locked, tmp_path):
    """A restored tab is a NEW open, so it stops for the password again.

    The session record carries a path and nothing else, which is what makes
    this true rather than a coincidence, so both halves are asserted: the
    record has no password in it, and reopening from it asks for one.
    """
    from ui.session import _restorable

    record = {"tabs": [{"path": locked, "page": 0, "zoom": 1.0,
                        "raster_scale": 1.0, "fit_mode": None}]}
    found, missing = _restorable(record)
    assert missing == 0 and len(found) == 1
    assert set(found[0]) == {"path", "page", "zoom", "raster_scale", "fit_mode"}, (
        "session tabs must record nothing beyond these; a password field here "
        "would be written to disk")

    reopened = PDFDocument()
    assert reopened.open(found[0]["path"]) is False
    assert reopened.needs_password() is True, (
        "a restored encrypted document must ask again, never remember")
    reopened.close()


# ---------------------------------------------------------------------------
# Saving keeps the protection
# ---------------------------------------------------------------------------

def test_saving_an_encrypted_file_keeps_it_encrypted(locked):
    pdf = PDFDocument()
    pdf.open(locked)
    pdf.unlock(USER_PW)
    assert pdf.save() is True

    check = fitz.open(locked)
    assert check.needs_pass, "the save dropped the encryption"
    assert check.authenticate(USER_PW)
    check.close()
    # And the document is still usable in this window, without asking again.
    assert pdf.is_open() is True
    assert pdf.needs_password() is False
    assert not pdf.render_page(0, 1.0).isNull()
    pdf.close()


def test_an_encrypted_in_place_save_is_incremental(locked):
    """Not about the bytes: the rewrite path closes and reopens the file, and
    the reopened file would need the password nobody kept."""
    pdf = PDFDocument()
    pdf.open(locked)
    pdf.unlock(USER_PW)
    plan = pdf.save_plan()
    assert plan.mode == SAVE_MODE_INCREMENTAL
    assert plan.keeps_encryption is True
    assert plan.signed is False
    assert plan.needs_confirmation is False, "encryption is not a question to ask"
    pdf.close()


def test_save_as_of_an_encrypted_file_writes_an_encrypted_copy(locked, tmp_path):
    pdf = PDFDocument()
    pdf.open(locked)
    pdf.unlock(USER_PW)
    target = str(tmp_path / "copy.pdf")
    assert pdf.save(target) is True
    check = fitz.open(target)
    assert check.needs_pass, "Save As dropped the encryption"
    assert check.authenticate(USER_PW)
    check.close()
    pdf.close()


def test_owner_restrictions_survive_a_save(owner_only, tmp_path):
    pdf = PDFDocument()
    pdf.open(owner_only)
    before = pdf.denied_operations()
    target = str(tmp_path / "resaved.pdf")
    assert pdf.save(target) is True
    check = PDFDocument()
    check.open(target)
    assert check.denied_operations() == before, (
        "the owner's restrictions were dropped by the save")
    check.close()
    pdf.close()


def test_the_save_plan_says_what_the_owner_withheld(owner_only):
    pdf = PDFDocument()
    pdf.open(owner_only)
    plan = pdf.save_plan()
    assert plan.keeps_encryption is True
    assert plan.restriction_note and "does not allow" in plan.restriction_note
    assert plan.needs_confirmation is False, (
        "a permission restriction is reported, not enforced; it must not block")
    pdf.close()


def test_a_plain_save_is_unchanged_by_all_of_this(tmp_path):
    """The ordinary document is the one that must not regress."""
    path = _build(tmp_path / "plain.pdf")
    pdf = PDFDocument()
    pdf.open(path)
    plan = pdf.save_plan()
    assert plan.keeps_encryption is False
    assert plan.restriction_note is None
    assert plan.mode != SAVE_MODE_INCREMENTAL
    assert pdf.save() is True
    check = fitz.open(path)
    assert not check.needs_pass and not check.is_encrypted
    check.close()
    pdf.close()


# ---------------------------------------------------------------------------
# The UI helper that drives the loop
# ---------------------------------------------------------------------------

def test_open_with_password_drives_the_whole_round_trip(locked):
    from ui.password_prompt import open_with_password

    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "wrong" if len(asked) == 1 else USER_PW

    pdf = PDFDocument()
    assert open_with_password(pdf, locked, ask=ask) is True
    assert len(asked) == 2
    assert USER_PW not in asked[1], "the retry prompt must not echo the password"
    assert pdf.is_open() is True
    pdf.close()


def test_open_with_password_takes_no_for_an_answer(locked):
    from ui.password_prompt import open_with_password

    pdf = PDFDocument()
    assert open_with_password(pdf, locked, ask=lambda prompt: None) is False
    assert pdf.needs_password() is False
    assert pdf.is_open() is False
    pdf.close()


def test_open_with_password_leaves_a_plain_file_alone(tmp_path):
    from ui.password_prompt import open_with_password

    def ask(prompt):        # pragma: no cover - must never run
        raise AssertionError("a plain file was asked for a password")

    pdf = PDFDocument()
    assert open_with_password(pdf, _build(tmp_path / "plain.pdf"), ask=ask) is True
    pdf.close()


def test_open_with_password_does_not_swallow_other_failures(tmp_path):
    from ui.password_prompt import open_with_password

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a PDF at all")
    pdf = PDFDocument()
    assert open_with_password(pdf, str(broken), ask=lambda p: USER_PW) is False
    assert pdf.needs_password() is False
    assert pdf.last_open_error and "Could not open" in pdf.last_open_error
    pdf.close()

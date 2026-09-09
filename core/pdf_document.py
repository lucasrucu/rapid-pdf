import fitz
import json
import os
import re
import tempfile
from collections import OrderedDict
from typing import NamedTuple

from PySide6.QtGui import QPixmap, QImage

from core.render_scale import AUTO, choose_render_scale
from core.resources import bundled_tessdata_dir
from core.settings import settings


def _resolve_tessdata() -> str | None:
    """Language data folder for the OCR engine embedded in PyMuPDF.

    Precedence: an explicit TESSDATA_PREFIX (the user knows best, e.g. for
    extra languages) > the bundled assets/tessdata folder (ships with the
    app, so OCR works on machines with no Tesseract install) > None, which
    lets PyMuPDF hunt for an installed Tesseract-OCR like before."""
    if os.environ.get("TESSDATA_PREFIX"):
        return None   # pdfocr_tobytes reads the env var itself
    return bundled_tessdata_dir()


RAPID_PDF_TAG = "rapid-pdf"
# Name of the embedded file that carries the editable annotation model, so a
# document saved by rapid-pdf reopens with its objects still movable/editable.
MODEL_EMBED_NAME = "rapid_pdf_model.json"

# How many rendered page pixmaps to keep in the LRU cache. A1 drawings rasterise
# to large QPixmaps (a 2384x1684pt page at zoom 1.5 is ~3576x2526 px ≈ 36 MB of
# 32-bit pixels). Keep this small so memory stays bounded on big documents while
# still covering the realistic hot pattern: lift + reload + a couple of
# page-switch round-trips all hit the same page+zoom.
RENDER_CACHE_MAX = 6

# How a save is going to be written. INCREMENTAL appends a new revision to the
# end of the existing file and leaves every byte already in it alone, which is
# the only kind of write a digital signature survives. REWRITE is the normal
# full rebuild (garbage collect + deflate) and produces a different file, so
# every signature in it is void.
SAVE_MODE_INCREMENTAL = "incremental"
SAVE_MODE_REWRITE = "rewrite"

# How many times a password may be tried against one encrypted file before the
# open is given up. THIS IS NOT A SECURITY CONTROL and must not be sold as one:
# anybody holding the file can retry forever with any tool they like, and
# nothing here can stop that. It exists so a mistyped password ends in a
# sentence rather than in a dialog that keeps coming back.
UNLOCK_ATTEMPT_LIMIT = 3

# What `doc.authenticate()` answers. MuPDF returns a bit field, measured on
# PyMuPDF 1.27.2.3: 0 for a password that does not fit, 2 when the password
# given was the USER password (the file opens and its permission restrictions
# still apply), 4 when it was the OWNER password (the file opens and every
# permission is granted). A file whose user and owner passwords are the same
# answers 6.
AUTH_FAILED = 0
AUTH_USER = 2
AUTH_OWNER = 4

# The operations a PDF can withhold, and what to call each one in front of a
# user. `doc.permissions` is a bit field with every bit SET on a file that
# restricts nothing (measured: -4, which is every bit but the low two), so a
# MISSING bit is a restriction. Named by fitz attribute rather than by value
# because not every PyMuPDF build defines every one of them.
PERMISSION_LABELS = (
    ("PDF_PERM_PRINT", "printing"),
    ("PDF_PERM_MODIFY", "changing the content"),
    ("PDF_PERM_COPY", "copying text out"),
    ("PDF_PERM_ANNOTATE", "adding annotations"),
    ("PDF_PERM_FORM", "filling in form fields"),
    ("PDF_PERM_ACCESSIBILITY", "extracting text for a screen reader"),
    ("PDF_PERM_ASSEMBLE", "adding, removing or reordering pages"),
    ("PDF_PERM_PRINT_HQ", "printing at full quality"),
)

# The restrictions that stand between the user and an ordinary edit-and-save.
# Reported on the save path; the rest are only worth saying once on open.
_EDIT_PERMISSIONS = ("PDF_PERM_MODIFY", "PDF_PERM_ANNOTATE", "PDF_PERM_ASSEMBLE")

# Shown when open() stops on an encrypted file. It is a prompt for the UI to
# act on rather than an error to display, and `PDFDocument.needs_password()` is
# what a caller should branch on; the text is here so a caller that only knows
# about `last_open_error` still says something true.
PASSWORD_REQUIRED = "This PDF is password protected. A password is needed to open it."


def _permission_bit(name: str) -> int:
    """The fitz constant behind a permission name, or 0 if this build lacks it."""
    try:
        return int(getattr(fitz, name))
    except (AttributeError, TypeError, ValueError):
        return 0


def denied_permissions(doc, only: tuple = PERMISSION_LABELS) -> list[str]:
    """The things `doc` does not allow, in words. Empty when it allows everything.

    An unencrypted document, and an encrypted one opened with its OWNER
    password, both report every bit set and so answer [].
    """
    try:
        allowed = int(doc.permissions)
    except Exception:
        return []
    out = []
    for name, label in only:
        bit = _permission_bit(name)
        if bit and not (allowed & bit):
            out.append(label)
    return out


class EncryptionInfo(NamedTuple):
    """What protection this document carries, read live rather than remembered.

    NO PASSWORD IS IN HERE, and none is anywhere else either. The password is
    handed to MuPDF, MuPDF keeps the derived key inside the document handle,
    and nothing in this process holds the characters the user typed once
    `unlock()` has returned. See `PDFDocument.unlock`.
    """

    encrypted: bool               # the file itself is encrypted
    needed_password: bool         # a password was required to open it at all
    owner_access: bool            # the owner password was the one that opened it
    denied: tuple[str, ...]       # operations the file withholds, in words

    @property
    def restricted(self) -> bool:
        return bool(self.denied)

    @property
    def notice(self) -> str | None:
        """One finished sentence for the status bar, or None when there is
        nothing worth saying."""
        if not self.encrypted:
            return None
        if not self.denied:
            return "This PDF is encrypted. It carries no restrictions."
        return ("This PDF is protected and its owner does not allow "
                + _join_words(self.denied) + ".")


def _join_words(items) -> str:
    """'a', 'a and b', 'a, b and c'. Used in sentences shown to the user."""
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


class SavePlan(NamedTuple):
    """What the next save is going to do, decided before anything is written.

    `reason` is a finished sentence fit to put in front of a user, and it is
    set if and only if `breaks_signature` is true. The core never opens a
    dialog; it hands this back and lets the window ask.

    `restriction_note` is separate on purpose. A permission restriction is
    something to SAY, not something to stop on: the owner of an encrypted file
    can deny editing, but rapid-pdf is not a rights-management product and
    refusing the save would only send the user to a tool that ignores the flag
    anyway. It is set when the document denies an edit this app has just made,
    and it never blocks (`needs_confirmation` stays about signatures alone).
    """

    mode: str                     # SAVE_MODE_INCREMENTAL or SAVE_MODE_REWRITE
    signed: bool                  # the document carries at least one real signature
    breaks_signature: bool        # writing it will make readers reject that signature
    reason: str | None            # what breaks and why, in words, or None
    keeps_encryption: bool = False    # the written file stays encrypted, as it was
    restriction_note: str | None = None   # a permission the owner withheld, in words

    @property
    def needs_confirmation(self) -> bool:
        return self.breaks_signature


def _signature_widgets(doc) -> list[str]:
    """Names of the signature fields in `doc` that have actually been SIGNED.

    An empty signature field is not a signature. Blank RFCC and RFWCC forms
    ship with one sitting on the page waiting for someone to sign it, and
    `get_sigflags()` reports 3 for those exactly as it does for a signed file
    (measured on PyMuPDF 1.27.2.3: adding an empty signature widget to a fresh
    document and saving it gives sigflags 3). Treating that as signed would
    push every blank form onto the incremental path and warn the user about
    breaking a signature that does not exist, so the field's /V entry is what
    decides: it holds the signature dictionary, and it is absent until the file
    is signed.
    """
    names = []
    for page in doc:
        for widget in page.widgets():
            if widget.field_type != fitz.PDF_WIDGET_TYPE_SIGNATURE:
                continue
            try:
                kind, _value = doc.xref_get_key(widget.xref, "V")
            except Exception:
                continue
            if kind and kind != "null":
                names.append(widget.field_name or "(unnamed)")
    return names


def _highlight_quad(visible_rect, derot) -> "fitz.Quad":
    """The quad to hand add_highlight_annot for a freehand highlight box.

    ONE QUAD FROM THE RECT, AND THAT IS DELIBERATE. A PDF highlight normally
    carries one quad per line of selected text, because that is where it comes
    from in a reader. Rapid PDF's highlight is not a text selection at all: the
    user drags a box over a drawing, which may have no text under it whatsoever.
    A single quad covering that box is the honest translation, and it is what
    every reader then paints.

    THE CORNERS ARE MAPPED INDIVIDUALLY, not taken from the derotated bounding
    box, and on a rotated page the difference is visible. MuPDF gives a
    highlight a marker-pen appearance whose overhang is scaled off the quad's
    own HEIGHT (measured on 1.27.2.3: about h/16 top and bottom, about
    h*0.2357 left and right). Feed it the derotated BOUNDING BOX of a 190x30
    box on a 90-degree page and it reads the 190 as the height, so it pads
    45pt sideways and the highlight balloons to four times its width. Feed it
    the four corners with their roles kept (upper-left stays upper-left in the
    quad even after the matrix moves it) and the overhang follows the box the
    user actually drew.

    On an unrotated page this is exactly `visible_rect.quad`.
    """
    r = fitz.Rect(visible_rect).normalize()
    ul, ur = fitz.Point(r.x0, r.y0), fitz.Point(r.x1, r.y0)
    ll, lr = fitz.Point(r.x0, r.y1), fitz.Point(r.x1, r.y1)
    if derot is not None:
        ul, ur, ll, lr = ul * derot, ur * derot, ll * derot, lr * derot
    return fitz.Quad(ul, ur, ll, lr)


def source_is_readable(source) -> bool:
    """Whether pages can still be pulled out of this render source.

    THE QUESTION `if source.doc:` DOES NOT ANSWER, and getting that wrong is
    known bug 6 in docs/tabs-plan.md. PyMuPDF's `Document` defines `__len__`,
    so truth-testing a CLOSED document raises "document closed" rather than
    returning False. Every lazy thumbnail render in the app is scheduled on a
    zero timer and run later, which is exactly the window in which the clone it
    was scheduled against can have been closed, so each of them has to ask this
    instead of trusting a truth test.

    Takes any render source, not just a PDFDocument: the page panel and the
    Organizer are both handed stand-ins whose `.doc` is not a fitz document at
    all, and one of those has no opinion about being closed, so it is taken at
    its word.
    """
    if source is None:
        return False
    doc = getattr(source, "doc", None)
    if doc is None:
        return False
    try:
        return not doc.is_closed
    except AttributeError:
        return True          # not a fitz document; it cannot have been closed
    except Exception:
        return False         # anything a closed document raises means no


class PDFDocument:
    def __init__(self):
        self.doc: fitz.Document | None = None
        self.path: str | None = None
        # Why the last save() returned False, in words fit to show a user, or
        # None. Set on every failure and cleared at the top of every save, so
        # the caller reads it immediately after a False and never later.
        self.last_save_error: str | None = None
        # Why the last open() returned False, in words fit to show a user, or
        # None. Same contract as last_save_error: read it straight after a False.
        self.last_open_error: str | None = None
        # True when the last save() returned False because it would have voided
        # a digital signature and nobody had said to go ahead. It is NOT an
        # error: the document is untouched and the same call with
        # allow_signature_break=True will write it. The window reads this right
        # after a False to decide between an error box and a question box.
        self.last_save_blocked_by_signature: bool = False
        # Whether pages have been added, removed or reordered since this file
        # was opened or last saved. An incremental save can still write that,
        # but no reader will accept a signature over a page tree that has moved
        # underneath it, so it is the difference between a save that keeps a
        # signature and one that only keeps the bytes. See save_plan().
        self._structure_changed = False
        # Cross-document page moves this document has been part of SINCE ITS
        # LAST SAVE, so the close prompt can say what is actually at stake.
        # Two lists of plain strings (the other document's display name):
        #   sent_to    - pages that left here and are now living in that file
        #   taken_from - pages that arrived here out of that file
        # Set by the transfer command, cleared by save(). See
        # DocumentView.transfer_warning.
        self.transfers_sent: list[tuple[int, str]] = []
        self.transfers_taken: list[tuple[int, str]] = []
        # LRU cache of rendered page pixmaps keyed by (page_num, zoom_key).
        # A cache hit makes a repeated render_page of the same page+zoom free
        # (the lift re-render, reload-after-strip, organizer/page round-trips).
        # MUST be invalidated whenever a page's content changes. A stale pixmap
        # showing a lifted-out image still present, or old baked markup, is a
        # correctness regression worse than slowness. See invalidate_* below and
        # the call sites in canvas/main_window.
        self._render_cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        # The raster scale this document's pages are drawn at, decided once from
        # page geometry on first ask and then never again. None means "not yet
        # decided"; see render_scale() for why it is settled once and not
        # recomputed when the setting changes.
        self._render_scale: float | None = None
        # An encrypted file that open() reached but could not read, held back
        # from `self.doc` on purpose. `self.doc` means "a document that can be
        # read", and every is_open/render/save path in the app relies on that,
        # so a locked handle waits here instead until `unlock()` promotes it.
        # NO PASSWORD IS EVER STORED BESIDE IT.
        self._locked: "fitz.Document | None" = None
        self._locked_path: str | None = None
        # Failed password attempts against the file in `_locked`, and what
        # authenticate() said about the one that finally worked (AUTH_USER or
        # AUTH_OWNER). Counts and bit flags only; see unlock().
        self.failed_unlock_attempts: int = 0
        self._authenticated_as: int = 0

    # ------------------------------------------------------------------
    # Rendered-page pixmap cache
    # ------------------------------------------------------------------

    @staticmethod
    def _zoom_key(zoom: float) -> float:
        # Round so tiny float drift on zoom doesn't defeat the cache, while
        # genuinely different zoom levels still key separately.
        return round(float(zoom), 4)

    def render_page_cached(self, page_num: int, zoom: float = 1.5) -> QPixmap:
        """render_page with an LRU pixmap cache keyed by (page_num, zoom).

        Returns the SAME QPixmap instance for repeated calls, so callers must treat
        it as read-only (copy() before cropping; setPixmap shares it, which is
        fine). Any mutation of the page's content must call invalidate_render_page
        (single page) or invalidate_render_cache (whole doc) first.
        """
        key = (page_num, self._zoom_key(zoom))
        pix = self._render_cache.get(key)
        if pix is not None:
            self._render_cache.move_to_end(key)   # mark most-recently-used
            return pix
        pix = self.render_page(page_num, zoom)
        # Don't cache an empty/failed render (e.g. doc closed); a later valid
        # render must not be shadowed by a cached blank.
        if not pix.isNull():
            self._render_cache[key] = pix
            self._render_cache.move_to_end(key)
            while len(self._render_cache) > RENDER_CACHE_MAX:
                self._render_cache.popitem(last=False)   # evict least-recently-used
        return pix

    def invalidate_render_page(self, page_num: int):
        """Drop every cached zoom-level for one page (its content changed)."""
        for key in [k for k in self._render_cache if k[0] == page_num]:
            del self._render_cache[key]

    def invalidate_render_cache(self):
        """Drop the whole cache (doc reopened/saved, pages reordered/deleted)."""
        self._render_cache.clear()

    def adopt(self, fitz_doc):
        """Take ownership of an in-memory fitz document (e.g. the Combine
        dialog's merged output). The document has no path yet, so the first
        save is forced through Save As; nothing touches disk until then."""
        if self.doc:
            self.doc.close()
        self._discard_locked()
        self.invalidate_render_cache()
        self._render_scale = None    # different document, different geometry
        self.doc = fitz_doc
        self.path = None
        self._structure_changed = False   # brand new document, nothing to compare to
        self._authenticated_as = 0        # a merged document carries no protection

    def replace_from_bytes(self, payload: bytes) -> bool:
        """Swap this document's CONTENT for `payload`, keeping its identity.

        The landing point for work done on a private copy of this document on
        a background thread: see core/ocr_worker.py, which OCRs its own
        independent fitz.Document because PyMuPDF documents are not thread
        safe, and hands the finished file back as bytes for the UI thread to
        apply here.

        Not `adopt`, and the difference is the whole reason this exists.
        `adopt` takes over from a genuinely different document, so it drops the
        path and forces the next save through Save As. This is the SAME
        document with new page content, so the path stays and the next Ctrl+S
        writes where it always would have.

        `_render_scale` is deliberately NOT reset either. Page geometry is
        unchanged by an OCR pass, and the scale is baked into the scene
        coordinates of every annotation on the document and into the undo
        stack behind them: see `render_scale` for why changing it under a live
        document is not a thing that can be done in isolation.
        """
        if not payload:
            return False
        try:
            replacement = fitz.open("pdf", payload)
        except Exception as e:
            self.last_open_error = f"Could not apply the result:\n{e}"
            return False
        path = self.path
        if self.doc:
            self.doc.close()
        self.doc = replacement
        self.path = path
        self.invalidate_render_cache()
        return True

    def open(self, path: str) -> bool:
        """Open a file, or return False with the reason in last_open_error.

        THE needs_pass CHECK IS NOT OPTIONAL, and it is known bug 4 in
        docs/tabs-plan.md. fitz.open() SUCCEEDS on a password-protected PDF: it
        hands back a document that reports a real page count and then raises
        "document closed or encrypted" on the first render. The app used to
        accept the file, draw an empty two-page document and blow up as soon as
        anything asked for a pixmap.

        THAT CHECK USED TO BE THE END OF IT: the file was refused with "This PDF
        is password protected, so it cannot be opened" and there was no way to
        give it one. Client-issued certificate packages arrive protected, so
        that refused real work. A locked file now PAUSES the open instead. The
        handle is kept, unauthenticated, in `self._locked`, `needs_password()`
        goes true, and the caller prompts and comes back through `unlock()`.

        `self.doc` STAYS NONE WHILE A FILE IS LOCKED, and that is deliberate
        rather than tidy. `self.doc` means "a document that can be read", and
        `is_open`, every render, every save and the whole page-transfer path
        take it at its word. A locked handle answers `len()` honestly and then
        raises "document closed or encrypted" on the first pixmap, so putting
        one in `self.doc` would put back exactly the bug this check was added
        for. Nothing downstream needs to learn about encryption.

        A file with only an OWNER password is not locked at all: it opens, and
        what it carries is a set of permission restrictions rather than a
        challenge. It goes through the ordinary path and `encryption_info()`
        reports what its owner withheld. Measured on PyMuPDF 1.27.2.3: such a
        file reports `needs_pass` 0 and a `permissions` bit field with the
        withheld bits clear.
        """
        self.last_open_error = None
        self._discard_locked()
        self.failed_unlock_attempts = 0
        self._authenticated_as = 0
        try:
            if self.doc:
                self.doc.close()
            self.doc = None
            self.invalidate_render_cache()   # new document, no stale pixmaps
            self._render_scale = None        # and a fresh scale decision
            self._structure_changed = False  # as it sits on disk, so far
            candidate = fitz.open(path)
            if getattr(candidate, "needs_pass", False):
                self._locked = candidate
                self._locked_path = path
                self.last_open_error = PASSWORD_REQUIRED
                return False
            self.doc = candidate
            self.path = path
            return True
        except Exception as e:
            print(f"Open error: {e}")
            self.doc = None
            self.last_open_error = f"Could not open the PDF:\n{e}"
            return False

    # ------------------------------------------------------------------
    # Encrypted files: the password round trip
    #
    # NOTHING HERE KEEPS A PASSWORD. `unlock()` hands the characters straight to
    # MuPDF, which derives the file key inside the document handle and holds
    # that; the parameter goes out of scope when the call returns and no
    # attribute, settings key, session record, log line or exception message
    # ever carries it. That last one matters most: every message built below is
    # a fixed string, so a password cannot reach a message box or a print()
    # through `last_open_error`. tests/test_encryption.py asserts it.
    # ------------------------------------------------------------------

    def _discard_locked(self):
        """Let go of a locked handle without reading anything out of it."""
        if self._locked is not None:
            try:
                self._locked.close()
            except Exception:
                pass
        self._locked = None
        self._locked_path = None

    def needs_password(self) -> bool:
        """True when open() reached an encrypted file and stopped for a password.

        The one thing a caller has to branch on after a False from `open()`.
        False for every other kind of open failure, so a caller that does not
        know about encryption keeps showing its error box and nothing changes.
        """
        return self._locked is not None

    def locked_path(self) -> str | None:
        """The file waiting on a password, for a prompt that names it."""
        return self._locked_path

    def unlock_attempts_left(self) -> int:
        """How many more passwords may be tried before the open is given up."""
        if self._locked is None:
            return 0
        return max(0, UNLOCK_ATTEMPT_LIMIT - self.failed_unlock_attempts)

    def unlock(self, password: str) -> bool:
        """Try `password` against the file waiting on one. True finishes the open.

        A False leaves `last_open_error` set to a sentence worth showing, and
        the caller looks at `needs_password()` again to decide whether to ask
        once more: it stays true while there are attempts left and goes FALSE
        when they run out, so a `while pdf.needs_password()` loop terminates by
        itself and a giving-up user just calls `cancel_unlock()`.

        Either password opens the file. The USER password opens it under
        whatever restrictions its owner set; the OWNER password opens it with
        all of them lifted, which is why `opened_as_owner()` is a separate
        question from `is_open()`.
        """
        if self._locked is None:
            self.last_open_error = "There is no locked PDF waiting for a password."
            return False
        try:
            code = int(self._locked.authenticate(password or ""))
        except Exception:
            # A malformed crypt dictionary raises rather than answering 0. It
            # is a wrong password as far as anyone here can tell, and the
            # exception text is not repeated: it can carry file internals and
            # this path is one keystroke away from the password itself.
            code = AUTH_FAILED
        if not code:
            self.failed_unlock_attempts += 1
            if self.unlock_attempts_left() <= 0:
                self._discard_locked()
                self.last_open_error = (
                    "That password did not open this PDF, and there were no "
                    f"more tries left after {UNLOCK_ATTEMPT_LIMIT}. Open it "
                    "again to start over.")
            else:
                left = self.unlock_attempts_left()
                self.last_open_error = (
                    "That password did not open this PDF. "
                    + (f"{left} tries left." if left != 1 else "1 try left."))
            return False
        self.doc = self._locked
        self.path = self._locked_path
        self._locked = None
        self._locked_path = None
        self._authenticated_as = code
        self.failed_unlock_attempts = 0
        self.last_open_error = None
        self.invalidate_render_cache()
        self._render_scale = None
        self._structure_changed = False
        return True

    def cancel_unlock(self):
        """The user gave up on the password. Leave this object empty."""
        self._discard_locked()
        self.failed_unlock_attempts = 0
        self.last_open_error = None

    def opened_as_owner(self) -> bool:
        """Whether the OWNER password is the one this document was opened with."""
        return bool(self._authenticated_as & AUTH_OWNER)

    def is_encrypted(self) -> bool:
        """Whether the FILE carries encryption, whether or not it is unlocked.

        NOT `doc.is_encrypted`, which is the "still locked" flag and goes false
        the moment a password is accepted, and not `doc.needs_pass` either,
        which stays 0 on a file protected by an owner password alone. The
        metadata's `encryption` entry is the one that survives both, measured
        on PyMuPDF 1.27.2.3: it names the algorithm ("Standard V5 R6 256-bit
        AES") for every encrypted file and is None for a plain one.
        """
        if self._locked is not None:
            return True
        if not self.is_open():
            return False
        try:
            return bool((self.doc.metadata or {}).get("encryption"))
        except Exception:
            return False

    def denied_operations(self) -> list[str]:
        """What this document's owner does not allow, in words. [] for most files."""
        if not self.is_open():
            return []
        return denied_permissions(self.doc)

    def encryption_info(self) -> EncryptionInfo:
        """The whole protection picture in one value. See EncryptionInfo."""
        if self._locked is not None:
            return EncryptionInfo(True, True, False, ())
        if not self.is_open():
            return EncryptionInfo(False, False, False, ())
        return EncryptionInfo(
            self.is_encrypted(),
            bool(getattr(self.doc, "needs_pass", False)),
            self.opened_as_owner(),
            tuple(self.denied_operations()),
        )

    def close(self):
        if self.doc:
            self.doc.close()
        self.doc = None
        self.path = None
        self._discard_locked()
        self.failed_unlock_attempts = 0
        self._authenticated_as = 0
        self.clear_transfer_ledger()
        self.invalidate_render_cache()
        self._render_scale = None
        self._structure_changed = False

    def is_open(self) -> bool:
        """Whether there is a document here that can still be read.

        NOT the same question as `if pdf.doc:`. See `source_is_readable`.
        """
        return source_is_readable(self)

    def page_count(self) -> int:
        return len(self.doc) if self.doc else 0

    def get_page_size(self, page_num: int) -> tuple[float, float]:
        if not self.doc or page_num >= len(self.doc):
            return (0.0, 0.0)
        # page.bound() gives the visible dimensions after rotation; page.rect does not.
        r = self.doc[page_num].bound()
        return (r.width, r.height)

    def render_scale(self) -> float:
        """The raster scale for THIS document, decided once and then fixed.

        Asked for on the open path, before anything is drawn, and answered from
        `page.bound()` on the first page, which costs nothing: the page's size
        is in the PDF's own structure, so no rasterisation is needed to learn
        it. See core/render_scale.py for the megapixel budget behind the choice
        and the measurements that set it.

        MEMOISED ON PURPOSE, and the memo is the feature rather than an
        optimisation. Annotations live in scene space, which is rendered-pixel
        space, so the scale is baked into the coordinates of every mark on the
        document and into the undo stack behind them. Changing it while a
        document is open would mean rescaling all of that in step. So the
        answer is computed on first use and returned unchanged forever after:
        the canvas can call this on every page load, a settings change lands on
        the next tab the user opens, and a save (which reopens the file in
        place) keeps the scale the markup was drawn against.

        Reset only where a genuinely different document takes this object over:
        `open`, `adopt` and `close`.
        """
        if self._render_scale is None:
            width, height = self.get_page_size(0)
            try:
                setting = settings().view.render_scale
            except Exception:
                # A settings store that cannot be built must not stop a file
                # opening. Auto is the default anyway, so this loses nothing
                # but an explicit override nobody can read.
                setting = AUTO
            self._render_scale = choose_render_scale(
                width, height, self.page_count(), setting)
        return self._render_scale

    @staticmethod
    def _render_page_at_zoom(page, zoom: float) -> QPixmap:
        """Rasterise a fitz page at the given uniform zoom into an opaque QPixmap.

        Shared by render_page (fixed zoom) and render_thumbnail (zoom derived
        from a target width) so the fitz→QImage→QPixmap conversion lives once.

        THE BUFFER IS BOUND TO A NAME ON PURPOSE, and it is not a style choice.
        QImage does NOT copy the memory it is handed; it borrows it and expects
        the caller to keep it alive for as long as the QImage is. This used to
        read `QImage(bytes(pix.samples), ...)`, where the bytes object was an
        unnamed temporary whose last reference died the moment the QImage
        constructor returned. Every render after that point was reading freed
        memory, and `QPixmap.fromImage` on the next line was the one deep copy
        that had to happen while the buffer was still valid. It usually got
        away with it, because a just-freed block is usually still intact, which
        is exactly what makes this class of bug show up as a random crash
        rather than a reproducible one.
        """
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        samples = pix.samples          # keep the buffer alive past fromImage
        img = QImage(samples, pix.width, pix.height, pix.stride,
                     QImage.Format.Format_RGB888)
        return QPixmap.fromImage(img)

    def render_page(self, page_num: int, zoom: float = 1.5) -> QPixmap:
        if not self.doc or page_num >= len(self.doc):
            return QPixmap()
        return self._render_page_at_zoom(self.doc[page_num], zoom)

    def render_thumbnail(self, page_num: int, max_width: int = 110) -> QPixmap:
        if not self.doc or page_num >= len(self.doc):
            return QPixmap()
        page = self.doc[page_num]
        # page.bound() gives the visible (post-rotation) dimensions; page.rect does not.
        zoom = max_width / page.bound().width
        return self._render_page_at_zoom(page, zoom)

    # ------------------------------------------------------------------
    # Digital signatures, and what a save is allowed to do to them
    # ------------------------------------------------------------------

    def _note_structure_change(self):
        """Record that the page tree moved, for the next save_plan()."""
        self._structure_changed = True

    def _incremental_filename(self, target: str) -> str | None:
        """The exact string to hand `doc.save(..., incremental=True)`, or None.

        PyMuPDF gates the incremental path on `self.name != filename or
        self.stream`, and that is a RAW STRING COMPARISON against the name the
        document was opened with, not a path comparison. Hand it anything else,
        including the same file spelled with different separators, and it
        raises ValueError("incremental needs original file"). So the document's
        own `name` is what gets passed, and `target` is only checked to be that
        same file.

        `stream` is the other half and it is the one that bites after OCR.
        `replace_from_bytes` swaps the content for a document opened from bytes
        while KEEPING `self.path`, so `path`, `is_same` and even
        `can_save_incrementally()` all still say yes while PyMuPDF would refuse.
        A document with no file behind it cannot be appended to, whatever the
        path field says.
        """
        doc = self.doc
        name = getattr(doc, "name", "") or ""
        if not name or getattr(doc, "stream", None):
            return None
        try:
            if os.path.abspath(name) != os.path.abspath(target):
                return None
        except Exception:
            return None
        return name

    def _can_append(self, target: str) -> bool:
        """Whether a new revision can be appended to `target` rather than
        rewriting it. Both halves of the question in one place: PyMuPDF's own
        answer, and the filename rule in `_incremental_filename`."""
        try:
            if not self.doc.can_save_incrementally():
                return False
        except Exception:
            return False
        return self._incremental_filename(target) is not None

    def _reopen_after_write(self, target: str):
        """Take the freshly written file back as the live document.

        The in-place rewrite swaps a new file over the one the handle was on,
        so the handle has to be replaced. An ENCRYPTED file comes back LOCKED,
        because on disk it still is and nothing in this process kept the
        password. It goes to `_locked` rather than to `self.doc`, so the next
        thing that looks at this document asks for the password instead of
        raising "document closed or encrypted" out of a render.

        Rare in practice: `save_plan` routes an encrypted in-place save down
        the incremental path, which never closes the handle. This is the
        fallback for a file that cannot take an appended revision (one MuPDF
        had to repair, or one whose content was rebuilt by OCR in this window).
        """
        reopened = fitz.open(target)
        if getattr(reopened, "needs_pass", False):
            self._locked = reopened
            self._locked_path = target
            self.doc = None
            self.failed_unlock_attempts = 0
            self._authenticated_as = 0
        else:
            self.doc = reopened

    def signature_names(self) -> list[str]:
        """Names of the signed signature fields in this document, if any.

        Empty for an unsigned file, and empty for a blank form that carries an
        unsigned signature field. Cheap on the common case: `get_sigflags()`
        answers -1 when the file has no signature fields at all, so nothing
        walks the pages unless there is something to find.
        """
        if not self.is_open():
            return []
        try:
            if self.doc.get_sigflags() < 0:
                return []
        except Exception:
            pass          # no AcroForm to read; fall through and look properly
        try:
            return _signature_widgets(self.doc)
        except Exception as e:
            print(f"Signature scan error: {e}")
            # Something in the form tree is unreadable. Assume the worst rather
            # than the best: a file we cannot inspect is one we must not silently
            # rewrite. sigflags 3 means the catalog claims signatures exist.
            try:
                return ["(unreadable signature field)"] if self.doc.get_sigflags() == 3 else []
            except Exception:
                return []

    def is_signed(self) -> bool:
        """Whether this document carries at least one digital signature.

        THE REASON THIS EXISTS. Every save this app has ever done was a full
        rewrite (`garbage=4, deflate=True`), which produces a different file
        and voids every signature in it. Lucas opens signed RFCC, RFWCC and SAC
        certificates daily; touching one annotation and pressing Ctrl+S used to
        silently destroy the signature on the file he then sent on. Nothing
        told him. See save_plan() for what is done about it.
        """
        return bool(self.signature_names())

    def save_plan(self, path: str | None = None) -> SavePlan:
        """Decide how the next save would be written, WITHOUT writing anything.

        Call it to find out whether to ask the user something before saving.
        `save()` runs it again itself, so a caller that does not care about
        signatures can ignore it entirely and still not lose one by accident.

        The rules, measured against PyMuPDF 1.27.2.3 rather than remembered:

        - An unsigned document is a plain rewrite, as before. There is nothing
          at stake and the compaction is worth having.
        - A signed document saved over the file it was opened from goes out
          incrementally: the existing bytes are left alone and a new revision
          is appended, which is the only write a signature survives.
          `incremental=True` forbids `garbage` ("Can't do incremental writes
          with garbage collection") and requires `encryption=PDF_ENCRYPT_KEEP`
          ("Can't do incremental writes when changing encryption"), so neither
          is passed. It also refuses a different filename ("incremental needs
          original file") and a file MuPDF had to repair on open ("Can't do
          incremental writes on a repaired file"), which is what
          `can_save_incrementally()` answers.
        - A signed document going anywhere else (Save As, an untitled document
          from Combine, a file that cannot take an incremental update) can only
          be written as a rewrite, and that breaks the signature.
        - A signed document whose PAGES have moved breaks the signature even
          though the write itself is still incremental. The bytes survive; the
          signature covers a page tree that is no longer the document.

        ENCRYPTION IS ALWAYS KEPT, on every branch, and `keeps_encryption` says
        so. `save()` passes `encryption=fitz.PDF_ENCRYPT_KEEP` to every write,
        so a protected certificate saved from this app comes back protected by
        the same password. It used to be dropped: a plain `doc.save()` writes
        an UNENCRYPTED file (measured, PyMuPDF 1.27.2.3), so Ctrl+S on a
        client's protected package quietly published it in the clear. The flag
        costs nothing on an unencrypted document, where it is a no-op.

        AN ENCRYPTED DOCUMENT SAVED IN PLACE GOES OUT INCREMENTALLY TOO, signed
        or not, and that is about keeping it usable rather than about the
        bytes. The in-place rewrite writes a temp file and swaps it over the
        original, which means closing the handle and opening the new file, and
        the new file needs the password again. Nothing here kept it, and
        nothing here is going to, so a rewrite would leave the user re-typing
        the password after every Ctrl+S. The incremental path writes through
        the handle it already has and never closes it, so the document stays
        open and unlocked. The cost is the compaction, which is the same trade
        the signed path already makes.
        """
        target = path or self.path
        if not self.is_open() or not target:
            return SavePlan(SAVE_MODE_REWRITE, False, False, None)

        keeps = self.is_encrypted()
        note = self._restriction_note()
        is_same = (self.path is not None
                   and os.path.abspath(target) == os.path.abspath(self.path))
        names = self.signature_names()
        if not names:
            if keeps and is_same and self._can_append(target):
                return SavePlan(SAVE_MODE_INCREMENTAL, False, False, None, True, note)
            return SavePlan(SAVE_MODE_REWRITE, False, False, None, keeps, note)

        who = ", ".join(names)

        if not is_same:
            if self.path is None:
                where = ("This document was built in this window (Combine), so it "
                         "has to be written out as a whole new file.")
            else:
                where = ("Saving to a different file has to write the whole file "
                         "out fresh.")
            return SavePlan(
                SAVE_MODE_REWRITE, True, True,
                f"This PDF is digitally signed ({who}).\n\n{where} The signature "
                "cannot come with it, and the saved copy will open with the "
                "signature shown as invalid.\n\nSave anyway?", keeps, note)

        if not self._can_append(target):
            return SavePlan(
                SAVE_MODE_REWRITE, True, True,
                f"This PDF is digitally signed ({who}), but a new revision cannot "
                "be appended to it: either its structure had to be repaired when "
                "it was opened, or its contents have already been rebuilt in this "
                "window (Enhance for Search does that). Saving rewrites the whole "
                "file and the signature will be shown as invalid afterwards."
                "\n\nSave anyway?", keeps, note)

        if self._structure_changed:
            return SavePlan(
                SAVE_MODE_INCREMENTAL, True, True,
                f"This PDF is digitally signed ({who}), and pages have been added, "
                "removed or reordered. The edit will be appended rather than "
                "rewritten, but a signature covers the pages it was applied to, so "
                "readers will show it as invalid.\n\nSave anyway?", keeps, note)

        return SavePlan(SAVE_MODE_INCREMENTAL, True, False, None, keeps, note)

    def _restriction_note(self) -> str | None:
        """A sentence naming the edits this document's owner withheld, or None.

        Reported rather than enforced, and the difference is a decision. The
        permission bits in a PDF are a request to the reader, not a lock: the
        file is already decrypted in memory by the time they can be read, every
        other tool the user has ignores them, and refusing the save would lose
        their work while protecting nothing. So the app says what the owner
        asked for and lets the user decide. Lifted entirely when the file was
        opened with its owner password, which is what the owner password is
        for.
        """
        if not self.is_encrypted() or self.opened_as_owner():
            return None
        blocked = denied_permissions(
            self.doc, tuple(p for p in PERMISSION_LABELS if p[0] in _EDIT_PERMISSIONS))
        if not blocked:
            return None
        return ("The owner of this PDF does not allow " + _join_words(blocked)
                + ". Rapid PDF will save your changes anyway; other readers may "
                "show the file as restricted.")

    def save(self, path: str | None = None,
             allow_signature_break: bool = False) -> bool:
        """Write the document. False means nothing was written where it was asked.

        A False ALWAYS leaves `last_save_error` set to something worth showing
        the user, and the in-place path has one failure mode that needs saying
        out loud. If the finished file cannot be swapped over the original (it
        is open in Acrobat, it is read-only, a sync client has it locked, or
        another Rapid PDF window is holding it), the new content is salvaged
        next to it as `<name>.pdf.bak` so no work is lost.

        THAT SALVAGE USED TO BE SILENT. `save()` returned False, the window
        said "Could not save the PDF", and `self.path` was left naming the
        original. Four things then disagreed: the live document was the .bak,
        the title bar and the tab named the original, the original on disk
        still held the old content, and the next Save wrote to whichever of
        them the path said. Two windows can now hold the same file, which makes
        a losing swap ordinary rather than exotic, so it is closed from both
        ends: the .bak is ADOPTED as the document's path, so everything
        downstream names the file that actually holds the work, and the reason
        goes in `last_save_error` for the caller to show.

        SIGNED DOCUMENTS ARE NOT REWRITTEN BEHIND THE USER'S BACK. `save_plan()`
        decides first. If the plan is incremental the write appends a revision
        and the signature lives; if the plan breaks a signature the save is
        REFUSED (False, `last_save_blocked_by_signature` True, the reason and
        the question in `last_save_error`) until the caller comes back with
        `allow_signature_break=True`. Passing that flag on an unsigned document
        does nothing at all, so a caller that always passes it has simply
        opted out of the protection.

        AN ENCRYPTED DOCUMENT STAYS ENCRYPTED. Every write here passes
        `encryption=fitz.PDF_ENCRYPT_KEEP`, so the saved file keeps the
        protection and the permissions it arrived with, under the same
        password. A plain `doc.save()` writes the file out UNENCRYPTED, which
        is what this used to do: opening a client's protected certificate
        package, moving one annotation and pressing Ctrl+S published it in the
        clear with nothing said. No password is needed for any of this and none
        is asked for; MuPDF still holds the key from the open. See save_plan()
        for why an encrypted in-place save goes out incrementally.
        """
        self.last_save_error = None
        self.last_save_blocked_by_signature = False
        if not self.doc or not (self.path or path):
            self.last_save_error = "There is no document to save."
            return False
        target = path or self.path
        plan = self.save_plan(target)
        if plan.breaks_signature and not allow_signature_break:
            self.last_save_blocked_by_signature = True
            self.last_save_error = plan.reason
            return False
        # An untitled (merged) doc has no current path → it's never an in-place save.
        is_same = self.path is not None and os.path.abspath(target) == os.path.abspath(self.path)
        # A save bakes markup/redactions into page content and (in-place) reopens
        # the document. Every cached page pixmap is now stale (would still show
        # pre-bake content); drop them all.
        self.invalidate_render_cache()
        tmp_path = None
        try:
            if plan.mode == SAVE_MODE_INCREMENTAL:
                # Appends a revision to the end of the file it was opened from.
                # No temp file and no atomic swap, because there is nothing to
                # swap: the original bytes stay exactly where they are and the
                # edit goes on the end. That is the whole point, and it is why
                # the signature survives.
                #
                # Neither `garbage` nor a new encryption setting is legal here
                # (PyMuPDF raises FzErrorArgument for both), and `deflate` is
                # left off as well: it would only compress the handful of new
                # annotation objects, and the fewer options on this path the
                # fewer ways it can start refusing on a later PyMuPDF.
                #
                # The document is NOT closed and reopened afterwards. The
                # in-place rewrite below has to, because it swaps the file out
                # from under an open handle; this one writes through the handle
                # it already has and stays live on the same file, which a second
                # incremental save straight after was measured to be fine with.
                self.doc.save(self._incremental_filename(target) or target,
                              incremental=True,
                              encryption=fitz.PDF_ENCRYPT_KEEP)
            elif is_same:
                dir_path = os.path.dirname(os.path.abspath(target))
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=dir_path) as tf:
                    tmp_path = tf.name
                # If this write raises, the outer except cleans up tmp_path (the
                # doc is still open and untouched, so the save simply fails safely).
                self.doc.save(tmp_path, garbage=4, deflate=True,
                              encryption=fitz.PDF_ENCRYPT_KEEP)
                # PyMuPDF can't write over its own open file, so close before the
                # swap. Drop the handle to None immediately: if anything below
                # fails, the except must never leave self.doc pointing at a closed
                # document (that would make every later render/save raise
                # "document closed" with no way to recover from the UI).
                self.doc.close()
                self.doc = None
                try:
                    # os.replace is atomic and overwrites on both POSIX and Windows.
                    # shutil.move falls back to a non-atomic copy when the target
                    # already exists on Windows, which can leave a truncated,
                    # corrupt original if the process dies mid-copy.
                    os.replace(tmp_path, target)
                except Exception as move_err:
                    # Couldn't swap the new file in; salvage the new content so no
                    # work is lost. Reopen from the .bak so the document stays live.
                    bak = target + ".bak"
                    try:
                        os.replace(tmp_path, bak)
                        self.doc = fitz.open(bak)
                        # Adopt it. The live document IS this file now, so
                        # letting `path` keep naming the original is what makes
                        # the two diverge without anybody being told.
                        self.path = bak
                        raise RuntimeError(
                            f"Could not overwrite:\n{target}\n\n"
                            f"Your work was saved to:\n{bak}\n\n"
                            "That file is now the open document. The original "
                            "is unchanged. Close whatever is holding it (another "
                            "window, Acrobat, a sync client) and use Save As to "
                            "put this back over it."
                        ) from move_err
                    except RuntimeError:
                        raise
                    except Exception as bak_err:
                        print(f"Save recovery error: {bak_err}")
                        # Last resort: try reopening the original (unchanged on disk).
                        try:
                            self.doc = fitz.open(target)
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"Could not overwrite:\n{target}\n\n"
                            "The edits could not be written anywhere and are "
                            "still only in this window. Use Save As to put them "
                            "somewhere writable before closing it."
                        ) from move_err
                # Reopen the freshly written file as the live document. An
                # encrypted file that was unlocked in this session comes back
                # LOCKED, because the file on disk still is: nothing here holds
                # the password to open it again, and nothing here is going to
                # start. The handle goes to `_locked` so the next read asks for
                # the password rather than raising "document closed or
                # encrypted" out of a render.
                self._reopen_after_write(target)
            else:
                self.doc.save(target, garbage=4, deflate=True,
                              encryption=fitz.PDF_ENCRYPT_KEEP)
            # Adopt the target as the canonical path so later saves write in place.
            self.path = target
            # Whatever this document owed the other side of a page move is now
            # on disk, so the close prompt has nothing left to warn about.
            self.clear_transfer_ledger()
            # The page tree on disk now matches the one in memory, so the next
            # save has no page moves of its own to warn about.
            self._structure_changed = False
            return True
        except Exception as e:
            print(f"Save error: {e}")
            # The window shows this verbatim, so a RuntimeError raised above
            # carries its own wording and anything else gets a line built here.
            self.last_save_error = (
                str(e) if isinstance(e, RuntimeError)
                else f"Could not save:\n{target}\n\n{e}")
            # If the temp file was written but never renamed into place (the swap
            # succeeds by renaming it away, and the .bak path renames it too), it's
            # orphaned next to the target. Clean it up so failed saves don't litter.
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False

    # ------------------------------------------------------------------
    # OCR ("Enhance for Search"): on-demand, explicit only
    # ------------------------------------------------------------------

    def page_has_text(self, page_num: int) -> bool:
        """True if this page already carries an extractable text layer.

        Used to skip pages that don't need OCR. Most pages in a normal
        editing session already have real text, so this keeps a full-document
        OCR pass fast and avoids garbling/duplicating existing text.
        """
        if not self.doc or page_num >= len(self.doc):
            return False
        try:
            return bool(self.doc[page_num].get_text().strip())
        except Exception:
            return False

    def ocr_page(self, page_num: int, language: str = "eng", dpi: int = 150) -> bool:
        """Replace this page's content with an OCR'd version carrying an
        invisible, searchable text layer, via the Tesseract engine compiled
        into PyMuPDF (no tesseract.exe needed at runtime).

        Only meant to be called on pages that fail page_has_text() (i.e.
        scanned/image-only pages). This rasterizes the page, so running it
        on a page that already has real vector text/graphics would destroy
        that content, not just add a text layer alongside it.

        Note: fitz.Page.get_textpage_ocr() alone does NOT persist a text
        layer into the saved file. It only returns an in-memory TextPage
        for immediate extraction. Producing bytes via Pixmap.pdfocr_tobytes()
        and splicing that in as the new page is what actually survives
        doc.save() and a later reopen (verified by testing).

        Dependency note: the OCR ENGINE is embedded in PyMuPDF, but the
        LANGUAGE DATA is not. _resolve_tessdata() supplies it: a user-set
        TESSDATA_PREFIX first, then the bundled assets/tessdata (ships in
        the installer, so OCR works on machines without Tesseract), then
        PyMuPDF's own hunt for an installed Tesseract-OCR. Only if all
        three come up empty does this raise RuntimeError("No tessdata
        specified and Tesseract is not installed"). Callers must surface
        errors to the user instead of swallowing them.

        Returns True on success; raises on OCR failure so the caller can
        report the real reason (the old behavior of returning False buried
        the missing-tessdata error).
        """
        if not self.doc or page_num >= len(self.doc):
            return False
        page = self.doc[page_num]
        pix = page.get_pixmap(dpi=dpi)
        ocr_bytes = pix.pdfocr_tobytes(compress=True, language=language,
                                       tessdata=_resolve_tessdata())
        ocr_src = fitz.open("pdf", ocr_bytes)
        try:
            # Insert the OCR'd replacement right after the original, then
            # drop the original, which keeps this page's position in the
            # document unchanged.
            self.doc.insert_pdf(ocr_src, from_page=0, to_page=0, start_at=page_num + 1)
        finally:
            ocr_src.close()
        self.doc.delete_page(page_num)
        self.invalidate_render_page(page_num)
        return True

    def page_char_count(self, page_num: int) -> int:
        """Number of extractable text characters on the page (0 = no text
        layer). Used to verify, in-app, that an OCR pass actually produced
        searchable text."""
        if not self.doc or page_num >= len(self.doc):
            return 0
        try:
            return len(self.doc[page_num].get_text().strip())
        except Exception:
            return 0

    def text_layer_report(self) -> list[int]:
        """Per-page character counts for the whole document, index = page."""
        return [self.page_char_count(pn) for pn in range(self.page_count())]

    def search_text(self, needle: str) -> list[tuple[int, "fitz.Rect"]]:
        """Find every occurrence of `needle` (case-insensitive, as PyMuPDF
        does) across the document. Returns [(page_num, rect), ...] in page
        order; rects are in the page's displayed coordinate space, the same
        space render_page rasterises (so scene coords = rect * zoom)."""
        hits: list[tuple[int, fitz.Rect]] = []
        if not self.doc or not needle:
            return hits
        for pn in range(len(self.doc)):
            try:
                for r in self.doc[pn].search_for(needle):
                    hits.append((pn, r))
            except Exception as e:
                print(f"Search error (page {pn}): {e}")
        return hits

    def remove_image_placement(self, page_num: int, xref: int) -> bool:
        """Remove the single content-stream draw of `xref` on this page, non-destructively.

        Visio/automation pages (e.g. from noe_painter) stamp each image with one
        `<a b c d e f> cm /Name Do` operator on top of a full-page background raster.
        Redacting the image's rect to "erase" it also blanks the background pixels
        underneath -> a white hole. Deleting just that one placement operator removes
        the image while leaving everything behind it untouched (no hole), the way a
        real PDF editor moves an object.

        Only the tight `cm` (six numbers) immediately-before-`Do` form is removed:
        that cm exists solely to place this image, so dropping it is self-contained.
        Returns True if a placement was removed; False if the safe pattern wasn't
        found (caller should fall back to redaction).
        """
        if not self.doc or page_num >= len(self.doc):
            return False
        page = self.doc[page_num]
        name = None
        for im in page.get_images(full=True):
            if im[0] == xref:
                name = im[7]
                break
        if not name:
            return False
        esc = re.escape(name.encode("latin-1"))
        # six-number cm directly followed by the image's /Name Do
        pat = re.compile(rb'(?:-?[\d.]+\s+){6}cm\s*/' + esc + rb'\s+Do\b')
        for sx in page.get_contents():
            raw = self.doc.xref_stream(sx)
            new, n = pat.subn(b'', raw)
            if n >= 1:
                self.doc.update_stream(sx, new)
                # This page's content changed (image placement gone). Drop its
                # cached pixmap so a reload can't show the still-present image.
                self.invalidate_render_page(page_num)
                return True
        return False

    def page_content_snapshot(self, page_num: int) -> list | None:
        """The raw bytes of every content stream on this page, with their xrefs.

        Paired with restore_page_content() so an edit that only rewrites a content
        stream (remove_image_placement) can be put back byte for byte. Returns
        None when the page can't be read, which callers treat as "not undoable".
        """
        if not self.doc or page_num >= len(self.doc):
            return None
        try:
            return [(sx, self.doc.xref_stream(sx))
                    for sx in self.doc[page_num].get_contents()]
        except Exception as e:
            print(f"Content snapshot failed: {e}")
            return None

    def restore_page_content(self, page_num: int, snapshot) -> bool:
        """Put a page_content_snapshot() back, and drop the page's cached pixmap."""
        if not self.doc or not snapshot or page_num >= len(self.doc):
            return False
        try:
            for sx, raw in snapshot:
                self.doc.update_stream(sx, raw)
        except Exception as e:
            print(f"Content restore failed: {e}")
            return False
        # The page's content changed back, so the cached pixmap is stale.
        self.invalidate_render_page(page_num)
        return True

    def move_page(self, from_idx: int, to_idx: int):
        if self.doc:
            self.doc.move_page(from_idx, to_idx)
            self.invalidate_render_cache()   # page indices shifted
            self._note_structure_change()

    def reorder(self, new_order: list):
        """Reorder pages so that new page i is the page currently at new_order[i].

        new_order must be a permutation of range(page_count). Annotations travel
        with their pages (verified: fitz keeps page contents on select()).
        """
        if self.doc and sorted(new_order) == list(range(len(self.doc))):
            self.doc.select(list(new_order))
            self.invalidate_render_cache()   # page indices changed
            self._note_structure_change()

    def clone_with_annotations(self, dicts_by_page: dict):
        """Return a throwaway fitz.Document copy with the given markup baked in.

        Lets us render thumbnails that include unsaved annotations WITHOUT mutating
        the live document (which would double-render markup in the editor). Caller
        owns the returned doc and should close() it when done.
        """
        clone = fitz.open()
        try:
            if self.doc:
                clone.insert_pdf(self.doc)
                writer = PDFDocument()
                writer.doc = clone           # reuse write_annotations on the clone
                try:
                    for pn, dicts in dicts_by_page.items():
                        if dicts and 0 <= pn < len(clone):
                            writer.write_annotations(pn, dicts)
                finally:
                    writer.doc = None        # detach so it never closes the clone
        except Exception:
            clone.close()
            raise
        return clone

    def strip_dangling_toc(self) -> int:
        """Drop bookmarks whose target page no longer exists. Returns how many.

        KNOWN BUG 5 in docs/tabs-plan.md, and it is older than the page-move
        work. `fitz.Document.delete_page` renumbers the table of contents but
        leaves the DELETED page's own entry pointing at -1, and that survives a
        save, so the file we write carries a broken bookmark. Measured on a
        3-page file with one bookmark per page:

            after delete_page(1): [[1,'One',1], [1,'Two',-1], [1,'Three',2]]

        A page number of 0 or less is fitz's "no destination" marker (real pages
        are 1-based here), so both are dropped. Cheap enough to run after every
        delete, and it does nothing at all on a document with no bookmarks.
        """
        if not self.doc:
            return 0
        try:
            toc = self.doc.get_toc(simple=True)
        except Exception:
            return 0
        if not toc:
            return 0
        kept = [entry for entry in toc if len(entry) > 2 and entry[2] > 0]
        dropped = len(toc) - len(kept)
        if dropped:
            try:
                self.doc.set_toc(kept)
            except Exception:
                return 0
        return dropped

    def delete_page(self, page_num: int):
        if self.doc and 0 <= page_num < len(self.doc):
            self.doc.delete_page(page_num)
            self.strip_dangling_toc()
            self.invalidate_render_cache()   # pages after this one renumbered
            self._note_structure_change()

    def delete_pages(self, page_nums: list) -> list:
        """Delete a whole selection of pages in one go.

        Indices are into the CURRENT document and may arrive in any order or
        with duplicates. Returns the ascending list actually deleted, which is
        what an undo needs to put them back at.
        """
        if not self.doc:
            return []
        rows = sorted({p for p in page_nums if 0 <= p < len(self.doc)})
        if not rows:
            return []
        for page_num in reversed(rows):   # descending, so the rest stay valid
            self.doc.delete_page(page_num)
        self.strip_dangling_toc()
        self.invalidate_render_cache()
        self._note_structure_change()
        return rows

    def extract_pages(self, page_nums: list):
        """A standalone in-memory PDF holding copies of `page_nums`, ascending.

        This is the stash that makes a page delete undoable: take the copy
        first, then delete. Page content and annotations travel with the copy
        (insert_pdf keeps both). Document-level things a lone page cannot carry
        by itself, such as a link pointing at another page, do not, so an undone
        delete restores what you can see rather than a byte-identical page.
        Caller owns the returned document.
        """
        stash = fitz.open()
        if not self.doc:
            return stash
        for page_num in sorted({p for p in page_nums if 0 <= p < len(self.doc)}):
            stash.insert_pdf(self.doc, from_page=page_num, to_page=page_num)
        return stash

    def restore_pages(self, stash, positions: list):
        """Put stashed pages back at the indices they held before a delete.

        `positions` lines up with the stash's own page order (both ascending).
        Inserting lowest-first is what keeps the arithmetic trivial: everything
        below an insertion point is already back in place, so the next position
        is still correct with no adjustment.
        """
        if not self.doc or stash is None:
            return
        for k, at in enumerate(sorted(positions)):
            if k >= len(stash):
                break
            self.doc.insert_pdf(stash, from_page=k, to_page=k,
                                start_at=max(0, min(at, len(self.doc))))
        self.invalidate_render_cache()   # page set/indices changed
        self._note_structure_change()

    # ------------------------------------------------------------------
    # Moving pages between two LIVE documents (phase 5 of docs/tabs-plan.md)
    # ------------------------------------------------------------------

    def transfer_pages_from(self, src: "PDFDocument", rows: list, at: int) -> int:
        """Copy `rows` out of another OPEN document and land them at `at`.

        The counterpart of `restore_pages` for a source that is a live document
        rather than a stash, and the whole engine half of dragging a page from
        one tab into another. A MOVE is this call followed by
        `src.delete_pages(rows)`; a copy is this call on its own.

        ONE insert_pdf PER ROW, deliberately. A multi-selection can be
        non-contiguous, and one call per page keeps the arithmetic trivial:
        everything already inserted sits below the next insertion point, so the
        k-th row goes to `at + k` with no adjustment.

        `src` MUST be a different Document object. PyMuPDF's insert_pdf refuses
        to read a document into itself, so a drop back into the document the
        pages came from is routed to the plain reorder path instead of here
        (see PagePanel's drop handling). Asserted rather than tolerated: a
        silent no-op would look like a page that vanished.

        What travels and what does not is measured in docs/tabs-plan.md ("What
        comes along with a page"). Annotations, links to the outside world,
        fonts, page size and rotation travel. Internal GOTO links pointing
        outside the copied range, layers, and unsaved rapid-pdf markup do not;
        the first two are reported by transfer_report() so the UI can say so,
        and markup is carried separately as JSON by the command.
        """
        if not self.doc or src is None or src.doc is None:
            return 0
        if src.doc is self.doc:
            raise ValueError("transfer_pages_from cannot read a document into itself")
        rows = sorted({int(r) for r in rows if 0 <= int(r) < src.page_count()})
        if not rows:
            return 0
        at = max(0, min(int(at), self.page_count()))
        for k, row in enumerate(rows):
            self.doc.insert_pdf(src.doc, from_page=row, to_page=row,
                                start_at=at + k, links=True, annots=True,
                                widgets=True)
        self.invalidate_render_cache()   # page set/indices changed
        self._note_structure_change()
        return len(rows)

    def transfer_report(self, rows: list) -> dict:
        """What moving `rows` OUT of this document will quietly lose or rename.

        PyMuPDF reports none of this: it drops an internal link whose target is
        outside the copied range, flattens layers, and renames a colliding form
        field, all silently and with no exception. Read before the move, so the
        UI can say it once in the status bar rather than leaving the user to
        find out on the next save.

        Keys: `links` (internal GOTO links that will not survive), `layers`
        (True when the source has optional content groups at all), `widgets`
        (form fields on the moved pages, which are the ones a name collision
        can rename in the destination).
        """
        out = {"links": 0, "layers": False, "widgets": 0}
        if not self.doc:
            return out
        rows = sorted({int(r) for r in rows if 0 <= int(r) < self.page_count()})
        moving = set(rows)
        for row in rows:
            try:
                page = self.doc[row]
            except Exception:
                continue
            try:
                for link in page.get_links():
                    if link.get("kind") == fitz.LINK_GOTO and link.get("page") not in moving:
                        out["links"] += 1
            except Exception:
                pass
            try:
                out["widgets"] += sum(1 for _ in page.widgets())
            except Exception:
                pass
        try:
            out["layers"] = bool(self.doc.get_ocgs())
        except Exception:
            out["layers"] = False
        return out

    def note_pages_sent(self, count: int, to_name: str):
        """Record that `count` pages left here for `to_name` and are not saved."""
        self.transfers_sent.append((int(count), to_name))

    def note_pages_taken(self, count: int, from_name: str):
        """Record that `count` pages arrived here out of `from_name`, unsaved."""
        self.transfers_taken.append((int(count), from_name))

    def forget_last_transfer(self, sent: bool):
        """Undo's half of the ledger: drop the entry the redo just wrote."""
        ledger = self.transfers_sent if sent else self.transfers_taken
        if ledger:
            ledger.pop()

    def clear_transfer_ledger(self):
        self.transfers_sent = []
        self.transfers_taken = []

    def insert_pdf(self, src_path: str, from_page: int = 0,
                   to_page: int = -1, start_at: int = -1):
        if not self.doc:
            return
        src = fitz.open(src_path)
        self.doc.insert_pdf(src, from_page=from_page, to_page=to_page, start_at=start_at)
        src.close()
        self.invalidate_render_cache()   # page set/indices changed
        self._note_structure_change()

    # ------------------------------------------------------------------
    # Editable annotation model (embedded JSON): for save/reopen round-trip
    # ------------------------------------------------------------------

    def _model_embed_names(self) -> list[str]:
        """Every embedded entry that holds a rapid-pdf model.

        On a malformed/garbage-collected name tree, embfile_add can append a digit
        on a name collision (e.g. 'rapid_pdf_model.json2'), leaving a stale second
        copy that embfile_del(MODEL_EMBED_NAME) never removes. Matching the base
        name as a PREFIX catches every such copy so writes can purge them all and
        reads can ignore the stale ones.
        """
        if not self.doc:
            return []
        try:
            return [n for n in self.doc.embfile_names()
                    if n == MODEL_EMBED_NAME or n.startswith(MODEL_EMBED_NAME)]
        except Exception:
            return []

    def write_annotation_model(self, model: dict):
        """Embed the editable annotation model as a JSON file inside the PDF.

        Replaces any previous copy. Stored at the document (catalog) level so it
        survives page reorder/delete and a deflate+garbage save.
        """
        if not self.doc:
            return
        try:
            data = json.dumps(model).encode("utf-8")
            # Purge EVERY previous copy, not just the exact base name. A prior
            # save could have left a suffixed duplicate ('…json2'); if even one
            # stale copy survived, read_annotation_model could pick it and silently
            # restore an OLD set of annotations (e.g. only pages 0-1), so newer
            # pages' markup would vanish on reopen. (embfile_upd is unreliable for
            # raw bytes in this PyMuPDF build, so delete + add.)
            for name in self._model_embed_names():
                try:
                    self.doc.embfile_del(name)
                except Exception:
                    pass
            self.doc.embfile_add(MODEL_EMBED_NAME, data)
        except Exception as e:
            print(f"Embed model error: {e}")

    def read_annotation_model(self) -> dict | None:
        """Return the embedded editable annotation model, or None if absent.

        If the file carries more than one copy (a stale duplicate from an older
        save), pick the richest (the one describing the most annotations) so a
        leftover earlier copy can never override the latest saved markup.
        """
        if not self.doc:
            return None
        best, best_count = None, -1
        for name in self._model_embed_names():
            try:
                data = self.doc.embfile_get(name)
                model = json.loads(bytes(data).decode("utf-8"))
            except Exception as e:
                print(f"Read model error ({name}): {e}")
                continue
            count = sum(len(v) for v in model.get("pages", {}).values())
            if count > best_count:
                best, best_count = model, count
        return best

    def delete_tagged_annotations(self, page_num: int):
        """Strip rapid-pdf's baked annotations from a page.

        Used on open so reconstructed editable items don't double-render on top of
        the markup that was baked into the file on the previous save.

        THE TAG IS THE ONLY THING IT MATCHES ON, and that is what keeps files
        written by older versions working. Highlights used to be baked as filled
        Square annotations and are now baked as real Highlight text markup;
        both carry `title == RAPID_PDF_TAG`, so a document saved by 1.9.0 or
        earlier is still stripped, still rebuilt from its embedded JSON model
        (which never recorded the annotation subtype in the first place), and
        still re-baked, now in the new form. Nothing here keys off the subtype
        and nothing should start to.
        """
        if not self.doc or page_num >= len(self.doc):
            return
        page = self.doc[page_num]
        for a in list(page.annots()):
            if a.info.get("title") == RAPID_PDF_TAG:
                page.delete_annot(a)
        # Baked markup just stripped from this page → its cached render is stale.
        self.invalidate_render_page(page_num)

    def write_annotations(self, page_num: int, annotations: list):
        """Replace all rapid-pdf-tagged annotations on this page with the given list.

        Annotation dicts carry fitz_rects in the page's visible coordinate space
        (matching the canvas render). For rotated pages, fitz annotation APIs expect
        PDF user space coords, so we apply the page's derotation matrix to convert.
        """
        if not self.doc or page_num >= len(self.doc):
            return
        page = self.doc[page_num]

        # For rotated pages, annotation rects/points are in visible (rendered) space
        # but fitz expects native PDF user space. Derotation converts between the two.
        derot = page.derotation_matrix if page.rotation != 0 else None

        # Page content is about to change (markup rewritten) → drop its cache.
        self.invalidate_render_page(page_num)

        # Remove only our tagged annotations
        to_delete = [a for a in page.annots() if a.info.get("title") == RAPID_PDF_TAG]
        for a in to_delete:
            page.delete_annot(a)

        for ann in annotations:
            ann_type = ann.get("type")
            rect = ann.get("fitz_rect")
            color = ann.get("color")
            opacity = ann.get("opacity", 1.0)

            # Keep the pre-derotation rect. A highlight quad is built from the
            # four CORNERS of the visible rect, each mapped through derot
            # separately, and that is not the same shape as the derotated
            # bounding box. See _highlight_quad.
            visible_rect = fitz.Rect(rect).normalize() if rect is not None else None

            # Convert from visible space to PDF user space for rotated pages.
            if rect is not None and derot is not None:
                rect = fitz.Rect(rect) * derot

            # Normalize the rect so matrix multiplication can't produce an inverted
            # (negative-width/height) rect that crashes PyMuPDF's C layer.
            if rect is not None:
                rect = fitz.Rect(rect).normalize()

            try:
                if ann_type == "highlight":
                    if rect is None or rect.is_empty or rect.is_infinite:
                        print(f"Annotation write skipped (highlight): degenerate rect {rect}")
                        continue
                    # A REAL TEXT-MARKUP HIGHLIGHT, not a filled box. This used
                    # to be add_rect_annot with a solid fill, which rapid-pdf
                    # drew acceptably and every other reader drew as an OPAQUE
                    # RECTANGLE sitting on top of the text. Acrobat users got a
                    # blanked-out line where a highlight was meant to be.
                    # add_highlight_annot writes subtype /Highlight, which
                    # readers composite with a multiply blend, so whatever is
                    # underneath shows through the way a marker pen behaves.
                    annot = page.add_highlight_annot(
                        _highlight_quad(visible_rect, derot))
                    # /C on a text-markup annotation is the marker colour and it
                    # goes in the STROKE slot. Passing fill= here is silently
                    # ignored, which is worth saying out loud because the rect
                    # branch below does use fill.
                    annot.set_colors(stroke=color if color else (1.0, 1.0, 0.0))
                    annot.set_opacity(opacity)
                    info = annot.info
                    info["title"] = RAPID_PDF_TAG
                    # THE TYPED NOTE HAS TO GO IN THE FILE. It used to be
                    # written on the rect branch and dropped here, so a note
                    # typed into a highlight survived a reopen in rapid-pdf
                    # (the embedded JSON model still had it) and did not exist
                    # for anybody else. That is the worst shape a bug can take:
                    # it looked saved and was not.
                    if ann.get("text"):
                        info["content"] = ann["text"]
                    annot.set_info(info)
                    annot.update()

                elif ann_type == "rect":
                    if rect is None or rect.is_empty or rect.is_infinite:
                        print(f"Annotation write skipped (rect): degenerate rect {rect}")
                        continue
                    annot = page.add_rect_annot(rect)
                    stroke = ann.get("stroke_color") or color or (0.0, 0.0, 0.0)
                    fill = ann.get("fill_color")
                    colors = {"stroke": stroke}
                    if fill:
                        colors["fill"] = fill
                    annot.set_colors(colors)
                    annot.set_opacity(opacity)
                    annot.set_border(width=ann.get("line_width", 2))
                    info = annot.info
                    info["title"] = RAPID_PDF_TAG
                    if ann.get("text"):
                        info["content"] = ann["text"]
                    annot.set_info(info)
                    annot.update()

                elif ann_type == "line":
                    p1 = ann.get("p1")
                    p2 = ann.get("p2")
                    if p1 and p2:
                        if derot is not None:
                            p1 = fitz.Point(p1) * derot
                            p2 = fitz.Point(p2) * derot
                        annot = page.add_line_annot(p1, p2)
                        stroke = ann.get("color") or (0.0, 0.0, 0.0)
                        annot.set_colors(stroke=stroke)
                        annot.set_opacity(opacity)
                        annot.set_border(width=ann.get("line_width", 2))
                        info = annot.info
                        info["title"] = RAPID_PDF_TAG
                        annot.set_info(info)
                        annot.update()

                elif ann_type == "text":
                    text = ann.get("text", "")
                    font_size = ann.get("font_size", 12)
                    color = ann.get("color", (0.0, 0.0, 0.0))
                    if rect and text:
                        if rect.is_empty or rect.is_infinite:
                            print(f"Annotation write skipped (text): degenerate rect {rect}")
                            continue
                        annot = page.add_freetext_annot(
                            rect, text,
                            fontsize=font_size,
                            text_color=color,
                            fill_color=None,
                        )
                        info = annot.info
                        info["title"] = RAPID_PDF_TAG
                        annot.set_info(info)
                        annot.update()

                elif ann_type == "image":
                    image_bytes = ann.get("image_bytes")
                    if not image_bytes:
                        print("Annotation write skipped (image): no image_bytes")
                        continue
                    if rect is None or rect.is_empty or rect.is_infinite:
                        print(f"Annotation write skipped (image): degenerate rect {rect}")
                        continue
                    if rect.width < 1 or rect.height < 1:
                        print(f"Annotation write skipped (image): rect too small {rect}")
                        continue
                    # rotate=page.rotation counteracts the page's own rotation so
                    # the image content appears upright in the rendered view. Without
                    # this, a page rotated 90° would bake the image rotated 90° as
                    # well, making it appear wrong after the save/auto-reload cycle.
                    # The rect was already derotated above for rotated pages.
                    page.insert_image(rect, stream=image_bytes,
                                      rotate=page.rotation)

            except Exception as e:
                print(f"Annotation write error ({ann_type}): {e}")

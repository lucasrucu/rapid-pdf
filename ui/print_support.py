"""Printing: Ctrl+P, the page range, the fit to paper, and the page loop.

ONE DIALOG, AND IT IS WINDOWS'. Ctrl+P opens the system print dialog and
nothing else. The printer, the copies, the colour, the paper, the page range
and print-to-file are all its fields, and this module does not offer a second
version of any of them. Everything that used to sit in an options dialog in
front of it is now permanent behaviour: every page is FITTED to the paper, and
every sheet takes the orientation of the page going on it. Both are the right
answer for the mixed A1/A3/A4 packs this app exists for, and neither is a
question worth a window.

The system dialog's preview pane says "This app doesn't support print preview".
That is not something this module can fix. Qt's Windows print dialog is the
legacy Win32 `PrintDlgEx` common dialog (qtbase,
src/printsupport/dialogs/qprintdialog_win.cpp, still true on the 6.8 branch),
and the preview pane in the Windows 11 unified dialog is fed by the WinRT pull
contract (PrintManager / IPrintDocumentPageSource, the app's own Paginate and
GetPreviewPage callbacks). A `PrintDlgEx` app never registers that contract, so
Windows has nothing to ask for pages and prints the apology instead. Every
classic Win32 app behaves the same way, Notepad included. `_preview` below is
the in-app alternative and it is deliberately NOT on the Ctrl+P path; see its
docstring.

WHAT GETS PRINTED IS WHAT IS ON SCREEN, NOT WHAT IS ON DISK. Markup in this app
lives as Qt items on the canvas and is only written into the PDF on save (see
DocumentView._flush_annotations), so printing the file's bytes would print a
drawing with none of the rectangles, lines or labels the user just put on it,
and would print the old version of any page they have edited since the last
save. So a print renders a THROWAWAY CLONE with the current markup baked in,
which is the same clone the Organizer and the page strip already render their
thumbnails from (PDFDocument.clone_with_annotations). Unsaved markup prints;
the live document is never touched; the clone is closed the moment the job
ends. `markup_baked_copy` below is the one place that is built, and it is built
AFTER the dialog is accepted, so a cancelled print costs nothing.

THE RESOLUTION COMES FROM THE PRINTER, NOT FROM THE SCREEN. The app's on-screen
raster scale is a fixed ladder frozen per document (core/render_scale.py),
because annotation coordinates are measured in rendered pixels. It is a SCREEN
number, about 108 to 216 DPI, and putting it on paper would look exactly as bad
as it sounds. What matters on paper is dots per inch of PAPER, so the scale
each page is rasterised at is derived from the destination rectangle in the
printer's own device pixels:

    zoom = (destination width in device px / page width in points)
           * min(1, PRINT_DPI_CAP / printer resolution)

The first term alone would rasterise 1:1 with the device. That is right in
principle and ruinous in practice: Qt reports 1200 DPI for a high-resolution
printer, and an A4 page at 1200 DPI is 9916 x 14033 px, which is 139
megapixels and about 420 MB of 24-bit pixels FOR ONE PAGE. The second term caps
the effective paper resolution at PRINT_DPI_CAP, which is 300, the point past
which a laser printer stops resolving more anyway.

Notice what the first term does for a big drawing on small paper, which is the
case this app exists for. An A1 drawing fitted onto A4 gets a destination
rectangle the size of A4, so it rasterises to an A4-sized image: 8.7
megapixels, not the 70 that rendering an A1 at 300 DPI would cost. The DPI
follows the paper, so the page size of the SOURCE never blows the budget up.

MAX_RENDER_MEGAPIXELS is the backstop for the other direction, an A1 drawing on
A1 paper, where the destination really is that big. It trades resolution for
staying alive, and it only ever bites on paper larger than A2.

ONE PAGE AT A TIME, AND CANCELLABLE. `print_document` renders a page, draws it,
frees it and moves to the next. Nothing is built up front, so a 500-page
document costs one page of memory rather than 500, and the progress callback
between pages is where the UI gets to breathe and where Cancel gets read. The
whole app runs on the GUI thread (only OCR has a worker), so a print cannot be
made to happen in the background here; it can be made to stay answerable, and
that is what the callback pair is for.

EACH PAGE PICKS ITS OWN ORIENTATION, ALWAYS. A commissioning pack is A4 check
sheets with A3 and A1 drawings in the middle of it, and forcing one orientation
on the whole document means half of it prints sideways in a corner. The system
dialog has ONE orientation control for the whole job, so it cannot express
this and this module overrides it per sheet. `orientation_for` reads the page's
own aspect (from `bound()`, which is the size AFTER the page's own /Rotate, so
a portrait page rotated 90 degrees counts as landscape) and the loop sets it
before starting that sheet. Measured on PySide6 6.11 / Qt 6.11: changing the
orientation and the page size between `newPage()` calls works and the output
really does carry mixed page sizes. Two things go with that, and both are load
bearing:

  - the orientation for the FIRST page has to be set before `QPainter.begin`,
    because there is no `newPage` in front of it;
  - `QPainter.viewport()` does NOT follow the change. It still reports the
    first page's rectangle on page two. The destination rectangle is therefore
    read from `printer.pageLayout().paintRectPixels()` every page, which does
    follow it.

FIT NEVER CROPS, AND FIT IS THE ONLY MODE. `fit_page` scales by the SMALLER of
the two ratios, so the whole page always lands inside the paper with its aspect
ratio intact, and the leftover is white space on one axis. There used to be an
actual-size choice next to it; it went with the options dialog, because an A1
drawing at its true size on A4 paper loses its edges and that is never what
somebody standing at a printer wanted.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPageLayout, QPainter
from PySide6.QtPrintSupport import (
    QAbstractPrintDialog, QPrintDialog, QPrinter, QPrintPreviewDialog,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QMessageBox, QProgressDialog,
)

from core.pdf_document import PDFDocument

#: Which pages, as the system dialog can express it. There is no "selection"
#: here any more: the page strip's ticks used to be a range you could print and
#: the system dialog has no field for them, so the range it does have (a first
#: and a last page) is the one the user gets.
RANGE_ALL = "all"
RANGE_CURRENT = "current"
RANGE_CUSTOM = "custom"
RANGE_MODES = (RANGE_ALL, RANGE_CURRENT, RANGE_CUSTOM)

#: The highest paper resolution anything is rasterised at, whatever the printer
#: claims. 300 DPI is where a laser printer stops resolving more detail and it
#: is a quarter of the 1200 Qt reports for a high-resolution device, which is
#: sixteen times fewer pixels per page.
PRINT_DPI_CAP = 300

#: The ceiling on ONE page's rendered image, in megapixels. Only reached when
#: the paper itself is large: A4 and A3 at 300 DPI are 8.7 and 17.4. An A1
#: sheet at 300 DPI would be 70, so it comes down to about 226 DPI instead,
#: which is still well past what a plotter puts on paper for line work.
MAX_RENDER_MEGAPIXELS = 40.0

#: A print of this many pages or fewer just happens; anything longer gets the
#: progress dialog with its Cancel button.
PROGRESS_AFTER_PAGES = 4

#: QPrintPreviewDialog renders every page it is asked for, up front, on the GUI
#: thread. A 500-page preview is a frozen window, so the preview is capped and
#: says that it is capped. Printing itself is not capped. Only `_preview` reads
#: this, and `_preview` is off the Ctrl+P path.
PREVIEW_PAGE_CAP = 50

#: A range token: "5", "2-7", "9-" (to the end) or "-4" (from the start).
_RANGE_TOKEN = re.compile(r"^(\d*)(?:[-–](\d*))?$")


class PageRangeError(ValueError):
    """A page range string that cannot be turned into pages.

    Carries a finished sentence fit to put in front of a user, the same
    contract `PDFDocument.last_save_error` follows.
    """


# ----------------------------------------------------------------------
# Which pages
# ----------------------------------------------------------------------


def parse_page_range(text: str, page_count: int) -> list[int]:
    """The 0-based pages named by a range string, ascending and deduplicated.

    Accepts what a person types: "1-5, 8", "3 7 9", "2;4", "10-" for ten to the
    end, "-4" for the start to four. Separators are commas, semicolons and
    whitespace, in any mixture. A backwards range ("5-2") is read as the range
    the user meant rather than refused, because there is only one thing it can
    mean. Spaces around a dash are closed up first, so "1 - 3" is the range one
    to three and not the two pages 1 and 3: whitespace is a separator
    everywhere else, and a dash with room around it is the one place a person
    means it not to be.

    Out-of-document numbers are CLAMPED, not refused: "1-999" on a 12-page
    document is the whole document, which is what it obviously means. A range
    that is entirely past the end has nothing left after clamping, and that is
    an error, because printing nothing is never what was meant.

    Raises PageRangeError, with a sentence, for anything malformed: an empty
    string, page zero, a bare dash, a letter, a second dash in one token.

    Nobody types into this app any more: the string it parses now comes from
    the system dialog's first and last page boxes, through
    `pages_from_printer`. It stays this forgiving because the clamping and the
    backwards-range rule are what keep that read honest, and because the day
    this app grows its own print pane it will need every line of it back.
    """
    if page_count <= 0:
        raise PageRangeError("There is no document to print.")
    if text is None or not text.strip():
        raise PageRangeError("Type the pages to print, like 1-5, 8.")

    pages: set[int] = set()
    saw_a_page_past_the_end = False
    tidied = re.sub(r"\s*([-–])\s*", r"\1", text.strip())
    for token in re.split(r"[\s,;]+", tidied):
        if not token:
            continue
        match = _RANGE_TOKEN.match(token)
        if match is None:
            raise PageRangeError(
                f"{token!r} is not a page or a range. Use numbers like 1-5, 8.")
        first, last = match.group(1), match.group(2)
        if last is None:
            # A bare number. "" cannot get here: the pattern needs a dash for
            # both groups to be empty and that case is caught below.
            if not first:
                raise PageRangeError(
                    f"{token!r} is not a page or a range. Use numbers like 1-5, 8.")
            low = high = int(first)
        else:
            if not first and not last:
                raise PageRangeError(
                    "A dash on its own is not a range. Use 1-5, or 5- for "
                    "page 5 to the end.")
            low = int(first) if first else 1
            high = int(last) if last else page_count
        if low < 1 or high < 1:
            raise PageRangeError("Pages are numbered from 1.")
        if low > high:
            low, high = high, low
        if low > page_count:
            saw_a_page_past_the_end = True
        pages.update(range(low - 1, min(high, page_count)))

    if not pages:
        # Everything clamped away, so every number was past the last page.
        if saw_a_page_past_the_end:
            raise PageRangeError(
                f"This document has {page_count} "
                f"page{'' if page_count == 1 else 's'}.")
        raise PageRangeError("Type the pages to print, like 1-5, 8.")
    return sorted(pages)


def resolve_pages(mode: str, page_count: int, current_page: int = 0,
                  custom: str = "") -> list[int]:
    """The 0-based pages a range MODE names, ascending.

    The one entry point for the three choices the system dialog can come back
    with, so `pages_from_printer` and the job cannot disagree about what any of
    them meant.
    """
    if page_count <= 0:
        raise PageRangeError("There is no document to print.")
    if mode == RANGE_ALL:
        return list(range(page_count))
    if mode == RANGE_CURRENT:
        if not 0 <= current_page < page_count:
            raise PageRangeError("There is no current page to print.")
        return [current_page]
    if mode == RANGE_CUSTOM:
        return parse_page_range(custom, page_count)
    raise PageRangeError(f"Unknown page range mode {mode!r}.")


def pages_from_printer(printer: QPrinter, page_count: int,
                       current_page: int = 0) -> list[int]:
    """The 0-based pages the system print dialog was left asking for.

    THE ONE PLACE THE DIALOG'S ANSWER IS READ. Qt hands the Windows dialog's
    range back on the QPrinter, as a print range plus a first and last page, so
    this turns those two numbers into the same page list everything downstream
    already takes.

    Anything that is not a range or the current page is the whole document.
    That covers `Selection` too, which cannot be reached: the option is never
    enabled on the dialog, because a PDF viewer's idea of a selection is pages
    ticked in the strip and the system dialog has no way to be told about them.
    Printing everything is the safe reading of a range nobody expressed.

    A range whose numbers are both zero is Qt saying the boxes were left empty,
    which is the whole document as well. One zero is the open-ended range the
    user typed on one side of the dash.
    """
    try:
        chosen = printer.printRange()
    except Exception:                                  # noqa: BLE001
        chosen = QPrinter.PrintRange.AllPages
    if chosen == QPrinter.PrintRange.CurrentPage:
        return resolve_pages(RANGE_CURRENT, page_count, current_page)
    if chosen == QPrinter.PrintRange.PageRange:
        first, last = int(printer.fromPage()), int(printer.toPage())
        if first <= 0 and last <= 0:
            return resolve_pages(RANGE_ALL, page_count)
        first = first if first > 0 else 1
        last = last if last > 0 else page_count
        return resolve_pages(RANGE_CUSTOM, page_count,
                             custom=f"{first}-{last}")
    return resolve_pages(RANGE_ALL, page_count)


# ----------------------------------------------------------------------
# Where the page lands on the paper
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PageFit:
    """One page's placement on one sheet, in the printer's device pixels."""

    dest: QRectF        # where the page is drawn
    scale: float        # device pixels per PDF point, the SAME on both axes


def fit_page(page_w_pt: float, page_h_pt: float, target) -> PageFit:
    """Place a page of `page_w_pt` x `page_h_pt` points inside `target`.

    `target` is the printable area in device pixels.

    Takes the smaller of the two ratios. That is the whole no-crop guarantee:
    scaling by the larger one would fill the paper and push the other axis off
    the edge, which is the failure that makes people print a drawing twice. The
    page is centred in whatever is left over.

    Both axes are scaled by ONE number, so the aspect ratio survives by
    construction rather than by arithmetic that has to be checked.
    """
    if page_w_pt <= 0 or page_h_pt <= 0:
        raise ValueError("a page has to have a size before it can be printed")
    tw, th = float(target.width()), float(target.height())
    if tw <= 0 or th <= 0:
        raise ValueError("the printable area of this paper is empty")

    scale = min(tw / page_w_pt, th / page_h_pt)
    w, h = page_w_pt * scale, page_h_pt * scale
    left = float(target.x()) + (tw - w) / 2.0
    top = float(target.y()) + (th - h) / 2.0
    return PageFit(dest=QRectF(left, top, w, h), scale=scale)


def orientation_for(page_w_pt: float, page_h_pt: float):
    """The sheet orientation a page of this shape wants.

    Read from the size AFTER the page's own rotation (`PDFDocument.get_page_size`
    uses `bound()` for exactly this reason), so a portrait page carrying
    /Rotate 90 asks for landscape, which is how it is read on screen.

    A square page gets portrait. It makes no difference to the fit and one of
    the two had to be picked.
    """
    if page_w_pt > page_h_pt:
        return QPageLayout.Orientation.Landscape
    return QPageLayout.Orientation.Portrait


def render_zoom(page_w_pt: float, page_h_pt: float, fit: PageFit,
                printer_dpi: int, dpi_cap: int = PRINT_DPI_CAP,
                budget_mpx: float = MAX_RENDER_MEGAPIXELS) -> float:
    """The PyMuPDF zoom (pixels per point) to rasterise this page at.

    `fit.scale` is 1:1 with the printer's device pixels, so on a device that
    reports 1200 DPI it is a 139-megapixel A4. The cap brings the PAPER
    resolution down to `dpi_cap`, and the megapixel budget is the second
    backstop for paper big enough that even 300 DPI is too many pixels. See the
    module docstring for the numbers.

    Never returns zero. A destination rectangle small enough to round the zoom
    to nothing would make PyMuPDF hand back an empty pixmap, which prints as a
    blank sheet with no error anywhere.
    """
    zoom = float(fit.scale)
    if printer_dpi > dpi_cap > 0:
        zoom *= dpi_cap / float(printer_dpi)
    pixels = (page_w_pt * zoom) * (page_h_pt * zoom)
    budget = max(budget_mpx, 0.0) * 1_000_000.0
    if budget > 0 and pixels > budget:
        zoom *= math.sqrt(budget / pixels)
    return max(zoom, 0.01)


# ----------------------------------------------------------------------
# The job
# ----------------------------------------------------------------------


@dataclass
class PrintResult:
    """What a finished (or abandoned) print did."""

    printed: int = 0
    cancelled: bool = False
    error: str | None = None

    def message(self) -> str:
        """One status-bar line about the whole job."""
        if self.error:
            return self.error
        if self.cancelled:
            return (f"Print cancelled after {self.printed} "
                    f"page{'' if self.printed == 1 else 's'}")
        return f"Printed {self.printed} page{'' if self.printed == 1 else 's'}"


def print_document(render: PDFDocument, pages, printer: QPrinter,
                   on_progress=None, is_cancelled=None) -> PrintResult:
    """Draw `pages` of `render` onto `printer`, one page at a time.

    `render` is a document whose pages already carry everything that should
    appear on paper (see `markup_baked_copy`). `pages` are 0-based.

    `on_progress(done, total)` is called after each page lands and is where the
    caller pumps the event loop; `is_cancelled()` is read before each page and
    stops the job cleanly. Both are plain callables rather than signals so the
    loop can be driven from a test with no widgets in it at all.

    THE ORDER INSIDE THE LOOP IS THE WHOLE THING:

    1. read the cancel flag, before any work is done for this page;
    2. set the orientation this page wants. Before `begin()` for the first
       page and before `newPage()` for the rest, because both of those are
       what commit a sheet's layout. This deliberately overrides whatever the
       system dialog's one Orientation control was left on: that control
       cannot say "each page the way its own drawing wants";
    3. re-read the paint rectangle from the page layout. `painter.viewport()`
       is stale after an orientation change and reports the previous sheet;
    4. fit, rasterise, draw, and let the pixmap go before the next page.
    """
    total = len(pages)
    result = PrintResult()
    if render is None or render.doc is None:
        result.error = "There is nothing to print."
        return result
    if total == 0:
        result.error = "No pages to print."
        return result

    cancelled = is_cancelled or (lambda: False)
    painter = QPainter()
    started = False
    try:
        for page_num in pages:
            if cancelled():
                result.cancelled = True
                break
            if not 0 <= page_num < render.page_count():
                continue
            page_w, page_h = render.get_page_size(page_num)
            if page_w <= 0 or page_h <= 0:
                continue

            printer.setPageOrientation(orientation_for(page_w, page_h))
            if not started:
                if not painter.begin(printer):
                    result.error = "The printer would not accept the job."
                    return result
                painter.setRenderHint(
                    QPainter.RenderHint.SmoothPixmapTransform, True)
                started = True
            elif not printer.newPage():
                result.error = ("The printer stopped accepting pages after "
                                f"{result.printed}.")
                break

            paint = printer.pageLayout().paintRectPixels(printer.resolution())
            # Origin (0, 0), not the paint rect's own, because Qt already puts
            # the painter's origin at the top-left of the printable area. Using
            # paint.x()/y() here would inset the page by the margin twice.
            target = QRectF(0.0, 0.0, float(paint.width()), float(paint.height()))
            fit = fit_page(page_w, page_h, target)
            zoom = render_zoom(page_w, page_h, fit, printer.resolution())
            pixmap = render.render_page(page_num, zoom)
            if pixmap.isNull():
                continue
            painter.drawPixmap(fit.dest, pixmap, QRectF(pixmap.rect()))
            del pixmap          # one page of pixels at a time, not len(pages)
            result.printed += 1
            if on_progress is not None:
                on_progress(result.printed, total)
    finally:
        if started:
            if result.cancelled:
                # Ask the device to throw the job away rather than print the
                # half of it that has already been drawn. A device that cannot
                # (PDF output) says so and the file is written anyway.
                printer.abort()
            painter.end()
    return result


# ----------------------------------------------------------------------
# What is printed: the live view, markup and all
# ----------------------------------------------------------------------


def markup_baked_copy(doc: PDFDocument, canvas) -> PDFDocument:
    """A throwaway document carrying the unsaved markup as real annotations.

    The same trick the Organizer and the page strip use for their thumbnails
    (DocumentView._make_markup_baked_render), and for the same reason: the
    canvas holds the markup as Qt items until a save writes it into the file,
    so anything that renders the live document renders a page with the markup
    missing. Baking into a CLONE means the document on screen is not touched
    and the user's undo history is not disturbed by a print.

    The caller owns the result and must call `close_render` on it.
    """
    dicts_by_page = {}
    if canvas is not None:
        for page_num in range(doc.page_count()):
            try:
                dicts_by_page[page_num] = canvas.get_all_annotation_dicts(page_num)
            except Exception:
                # One unprintable item must not cost the user the whole print.
                dicts_by_page[page_num] = []
    render = PDFDocument()
    render.doc = doc.clone_with_annotations(dicts_by_page)
    return render


def close_render(render):
    """Close a clone from `markup_baked_copy`. Safe to call twice."""
    if render is None or render.doc is None:
        return
    try:
        render.doc.close()
    except Exception:
        pass
    render.doc = None


@dataclass
class PrintSource:
    """Everything a print needs to know about the document being printed."""

    doc: PDFDocument
    canvas: object = None
    current_page: int = 0
    name: str = "document"

    @property
    def page_count(self) -> int:
        return self.doc.page_count() if self.doc else 0


def source_from_view(view) -> PrintSource | None:
    """Read a DocumentView for what a print needs, or None if it holds nothing.

    THE ONE PLACE THIS MODULE REACHES INSIDE A VIEW. `_doc` and `_canvas` are
    the view's own, and there is no public accessor for either; the tests reach
    for them the same way. Keeping both reads here means a rename in
    DocumentView breaks one function rather than five call sites, and
    everything else in this file takes a PrintSource.

    The page strip's ticks are no longer read. They fed the old "Selected
    pages" range, and the system print dialog has no field that could carry
    them.
    """
    if view is None:
        return None
    doc = getattr(view, "_doc", None)
    if doc is None or doc.doc is None or doc.page_count() <= 0:
        return None
    try:
        current = int(view.current_page())
    except Exception:
        current = 0
    return PrintSource(
        doc=doc,
        canvas=getattr(view, "_canvas", None),
        current_page=current,
        name=view.document_name() or "document",
    )


# ----------------------------------------------------------------------
# The whole gesture, from Ctrl+P to paper
# ----------------------------------------------------------------------


def _run_with_progress(window, render, pages, printer) -> PrintResult:
    """Run a job, with a progress dialog once it is long enough to want one.

    The dialog is the ONLY thing keeping a long print answerable. Everything in
    this app runs on the GUI thread, so without the `processEvents` in the
    progress callback a 500-page job is a frozen window with no way out; with
    it, the window repaints between pages and the Cancel button is read.
    """
    if len(pages) <= PROGRESS_AFTER_PAGES:
        return print_document(render, pages, printer)

    progress = QProgressDialog(f"Printing {len(pages)} pages…", "Cancel",
                               0, len(pages), window)
    progress.setWindowTitle("Printing")
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(0)
    progress.setValue(0)

    def report(done, total):
        progress.setLabelText(f"Printing page {done} of {total}…")
        progress.setValue(done)
        QApplication.processEvents()

    try:
        return print_document(render, pages, printer, on_progress=report,
                              is_cancelled=progress.wasCanceled)
    finally:
        progress.close()


def _preview(window, render, pages, printer) -> PrintResult:
    """Show the print in a preview window before anything reaches paper.

    NOTHING CALLS THIS, ON PURPOSE, AND IT IS KEPT ON PURPOSE. Ctrl+P goes
    straight to the system dialog; a second preview window in front of a
    dialog that has a preview pane of its own was two windows in the way. It is
    still here because the system dialog's pane says "This app doesn't support
    print preview" and always will (see the module docstring), so an in-app
    preview is the only preview this app can ever have. What it needs before it
    comes back is a pane drawn in this app's own chrome, not Qt's stock toolbar
    on a window that shows page sizes it made up.

    Capped at PREVIEW_PAGE_CAP pages, and the cap is stated rather than hidden:
    QPrintPreviewDialog renders everything it is given up front on the GUI
    thread, so an uncapped preview of a 500-page pack is a hang. What prints
    afterwards is the full range; only the picture is short.
    """
    shown = pages[:PREVIEW_PAGE_CAP]
    result = PrintResult()

    def paint(target_printer):
        # The preview asks for a repaint on every zoom and page turn, so this
        # runs more than once. Nothing here keeps state between calls.
        outcome = print_document(render, shown, target_printer)
        result.printed = outcome.printed
        result.error = outcome.error

    dialog = QPrintPreviewDialog(printer, window)
    dialog.setWindowTitle(
        f"Print Preview: first {len(shown)} of {len(pages)} pages"
        if len(shown) < len(pages) else "Print Preview")
    dialog.paintRequested.connect(paint)
    dialog.exec()
    return result


def print_active_document(window, view=None) -> PrintResult:
    """File > Print… (Ctrl+P). The whole gesture, in one call, one window.

    The system print dialog is the ONLY thing that opens. It owns the printer,
    the copies, the colour, the paper, the page range and print-to-file, and
    nothing here asks any of that again.

    THE CLONE IS BUILT AFTER THE DIALOG IS ACCEPTED, and closed on every path
    out. It is a full copy of the document in memory, so a Ctrl+P the user
    backed out of should not have paid for one, and leaking one per print would
    be a real leak on the A1 drawings this app is for.
    """
    source = source_from_view(view if view is not None else window.view)
    if source is None:
        return PrintResult(error="Open a PDF before printing.")

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setDocName(source.name)

    dialog = QPrintDialog(printer, window)
    dialog.setWindowTitle("Print")
    # The range boxes are bounded by the document rather than left open, so
    # "print pages 1 to 9999" cannot be asked for. setMinMax turns the range
    # option on by itself; PrintCurrentPage is the only other one worth having,
    # and Selection is deliberately absent (see `pages_from_printer`).
    dialog.setMinMax(1, source.page_count)
    dialog.setOption(
        QAbstractPrintDialog.PrintDialogOption.PrintCurrentPage, True)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return PrintResult(cancelled=True)

    render = None
    try:
        pages = pages_from_printer(printer, source.page_count,
                                   source.current_page)
        render = markup_baked_copy(source.doc, source.canvas)
        return _run_with_progress(window, render, pages, printer)
    except PageRangeError as exc:
        return PrintResult(error=str(exc))
    except Exception as exc:                          # noqa: BLE001
        return PrintResult(error=f"Could not print: {exc}")
    finally:
        close_render(render)


def report_print_result(window, result: PrintResult):
    """Put the outcome where the user will see it: the status bar, or a box.

    An error gets a dialog because it means no paper came out and the user has
    to do something about it. Everything else is one status-bar line, because a
    successful print announces itself by arriving at the printer.
    """
    if result.error:
        QMessageBox.warning(window, "Print", result.error)
        return
    if result.cancelled and not result.printed:
        return
    window.show_status(result.message())

"""Printing: Ctrl+P, the page range, the fit to paper, and the page loop.

WHAT GETS PRINTED IS WHAT IS ON SCREEN, NOT WHAT IS ON DISK. Markup in this app
lives as Qt items on the canvas and is only written into the PDF on save (see
DocumentView._flush_annotations), so printing the file's bytes would print a
drawing with none of the rectangles, lines or labels the user just put on it,
and would print the old version of any page they have edited since the last
save. So a print renders a THROWAWAY CLONE with the current markup baked in,
which is the same clone the Organizer and the page strip already render their
thumbnails from (PDFDocument.clone_with_annotations). Unsaved markup prints;
the live document is never touched; the clone is closed the moment the job
ends. `markup_baked_copy` below is the one place that is built.

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

EACH PAGE PICKS ITS OWN ORIENTATION. A commissioning pack is A4 check sheets
with A3 and A1 drawings in the middle of it, and forcing one orientation on the
whole document means half of it prints sideways in a corner. `orientation_for`
reads the page's own aspect (from `bound()`, which is the size AFTER the page's
own /Rotate, so a portrait page rotated 90 degrees counts as landscape) and the
loop sets it before starting that sheet. Measured on PySide6 6.11 / Qt 6.11:
changing the orientation and the page size between `newPage()` calls works and
the output really does carry mixed page sizes. Two things go with that, and
both are load bearing:

  - the orientation for the FIRST page has to be set before `QPainter.begin`,
    because there is no `newPage` in front of it;
  - `QPainter.viewport()` does NOT follow the change. It still reports the
    first page's rectangle on page two. The destination rectangle is therefore
    read from `printer.pageLayout().paintRectPixels()` every page, which does
    follow it.

FIT NEVER CROPS. `fit_page` scales by the SMALLER of the two ratios, so the
whole page always lands inside the paper with its aspect ratio intact, and the
leftover is white space on one axis. Actual size is the other choice and it can
overflow, which is what actual size means when an A1 drawing meets A4 paper;
the job counts those pages and says so afterwards rather than letting the user
find out at the printer.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPageLayout, QPainter
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrintPreviewDialog
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressDialog,
    QRadioButton, QVBoxLayout,
)

from core.pdf_document import PDFDocument

#: How the page is sized onto the paper. Fit is the default and the one that
#: covers a mixed commissioning pack; actual size is for the rare case where a
#: drawing has to come out at its true scale and the paper is big enough.
FIT_PAGE = "fit"
ACTUAL_SIZE = "actual"
SCALE_MODES = (FIT_PAGE, ACTUAL_SIZE)

#: Which pages. "selection" is the pages ticked in the left strip or the
#: Organizer, and it is offered only when there are some.
RANGE_ALL = "all"
RANGE_CURRENT = "current"
RANGE_SELECTION = "selection"
RANGE_CUSTOM = "custom"
RANGE_MODES = (RANGE_ALL, RANGE_CURRENT, RANGE_SELECTION, RANGE_CUSTOM)

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
#: says that it is capped. Printing itself is not capped.
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
                  selection=(), custom: str = "") -> list[int]:
    """The 0-based pages a range MODE names, ascending.

    The one entry point for the four choices, so the dialog, the preview and
    the job cannot disagree about what "selection" meant.

    Selection is whatever is ticked in the page strip or the Organizer, sorted
    and filtered to pages that exist. An empty selection is an error rather
    than a silent fall back to the whole document: the control offering it is
    disabled when there is nothing selected, so reaching here with none is a
    bug worth hearing about.
    """
    if page_count <= 0:
        raise PageRangeError("There is no document to print.")
    if mode == RANGE_ALL:
        return list(range(page_count))
    if mode == RANGE_CURRENT:
        if not 0 <= current_page < page_count:
            raise PageRangeError("There is no current page to print.")
        return [current_page]
    if mode == RANGE_SELECTION:
        pages = sorted({p for p in selection if 0 <= p < page_count})
        if not pages:
            raise PageRangeError("No pages are selected.")
        return pages
    if mode == RANGE_CUSTOM:
        return parse_page_range(custom, page_count)
    raise PageRangeError(f"Unknown page range mode {mode!r}.")


# ----------------------------------------------------------------------
# Where the page lands on the paper
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PageFit:
    """One page's placement on one sheet, in the printer's device pixels."""

    dest: QRectF        # where the page is drawn
    scale: float        # device pixels per PDF point, the SAME on both axes
    clipped: bool       # the page is bigger than the paper and will lose edges


def fit_page(page_w_pt: float, page_h_pt: float, target, mode: str,
             dpi: int) -> PageFit:
    """Place a page of `page_w_pt` x `page_h_pt` points inside `target`.

    `target` is the printable area in device pixels and `dpi` is the printer's
    resolution, which is what makes actual size mean anything.

    FIT takes the smaller of the two ratios. That is the whole no-crop
    guarantee: scaling by the larger one would fill the paper and push the
    other axis off the edge, which is the failure that makes people print a
    drawing twice. The page is centred in whatever is left over.

    ACTUAL SIZE ignores the paper and uses dpi/72, one PDF point being 1/72 of
    an inch. A page larger than the paper is centred and clipped, and `clipped`
    says so, because the alternative (quietly shrinking it) is fit-to-page
    wearing the wrong label.

    Both modes scale both axes by ONE number, so the aspect ratio survives by
    construction rather than by arithmetic that has to be checked.
    """
    if page_w_pt <= 0 or page_h_pt <= 0:
        raise ValueError("a page has to have a size before it can be printed")
    tw, th = float(target.width()), float(target.height())
    if tw <= 0 or th <= 0:
        raise ValueError("the printable area of this paper is empty")

    if mode == ACTUAL_SIZE:
        scale = dpi / 72.0
    else:
        scale = min(tw / page_w_pt, th / page_h_pt)

    w, h = page_w_pt * scale, page_h_pt * scale
    # One POINT of slack, in device pixels, and it has to be that generous.
    # "A4" is not one number: the paper is 595.276 x 841.89 points and the A4
    # pages this app opens are usually a rounded 595 x 842, so an A4 page at
    # actual size on A4 paper overhangs by a fraction of a point. At 1200 DPI
    # that fraction is 16 device pixels, so a half-pixel tolerance reported
    # every ordinary check sheet as clipped. Nothing that genuinely does not
    # fit misses by less than a point.
    slack = max(1.0, dpi / 72.0)
    clipped = w > tw + slack or h > th + slack
    left = float(target.x()) + (tw - w) / 2.0
    top = float(target.y()) + (th - h) / 2.0
    return PageFit(dest=QRectF(left, top, w, h), scale=scale, clipped=clipped)


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
class PrintOptions:
    """The choices that are not the printer's own. See PrintOptionsDialog."""

    scale_mode: str = FIT_PAGE
    #: Set each sheet's orientation from the page going on it, rather than
    #: printing the whole document in whatever the printer is set to.
    match_page_orientation: bool = True


@dataclass
class PrintResult:
    """What a finished (or abandoned) print did."""

    printed: int = 0
    cancelled: bool = False
    #: Pages that overflowed the paper at actual size. Always 0 for fit.
    clipped: int = 0
    error: str | None = None

    def message(self) -> str:
        """One status-bar line about the whole job."""
        if self.error:
            return self.error
        if self.cancelled:
            return (f"Print cancelled after {self.printed} "
                    f"page{'' if self.printed == 1 else 's'}")
        pages = f"{self.printed} page{'' if self.printed == 1 else 's'}"
        if self.clipped:
            return (f"Printed {pages}. {self.clipped} did not fit the paper at "
                    "actual size and lost their edges")
        return f"Printed {pages}"


def print_document(render: PDFDocument, pages, printer: QPrinter,
                   options: PrintOptions | None = None,
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
       what commit a sheet's layout;
    3. re-read the paint rectangle from the page layout. `painter.viewport()`
       is stale after an orientation change and reports the previous sheet;
    4. fit, rasterise, draw, and let the pixmap go before the next page.
    """
    options = options or PrintOptions()
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
        for index, page_num in enumerate(pages):
            if cancelled():
                result.cancelled = True
                break
            if not 0 <= page_num < render.page_count():
                continue
            page_w, page_h = render.get_page_size(page_num)
            if page_w <= 0 or page_h <= 0:
                continue

            if options.match_page_orientation:
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
            fit = fit_page(page_w, page_h, target, options.scale_mode,
                           printer.resolution())
            zoom = render_zoom(page_w, page_h, fit, printer.resolution())
            pixmap = render.render_page(page_num, zoom)
            if pixmap.isNull():
                continue
            painter.drawPixmap(fit.dest, pixmap, QRectF(pixmap.rect()))
            del pixmap          # one page of pixels at a time, not len(pages)
            result.printed += 1
            if fit.clipped:
                result.clipped += 1
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
    selection: tuple = ()
    name: str = "document"

    @property
    def page_count(self) -> int:
        return self.doc.page_count() if self.doc else 0

    def has_selection(self) -> bool:
        """Whether "Selected pages" is a choice that means anything here.

        MORE THAN ONE PAGE, and that is not fussiness. The page strip always
        has the page being read highlighted, so a selection of exactly one is
        the ordinary state of the app and offering it as a range would put a
        second, differently worded "Current page" next to the real one. Two or
        more is a selection somebody made on purpose.
        """
        return len({p for p in self.selection if 0 <= p < self.page_count}) > 1


def source_from_view(view) -> PrintSource | None:
    """Read a DocumentView for what a print needs, or None if it holds nothing.

    THE ONE PLACE THIS MODULE REACHES INSIDE A VIEW. `_doc`, `_canvas` and
    `_page_panel` are the view's own, and there is no public accessor for any
    of them; the tests reach for them the same way. Keeping all three reads
    here means a rename in DocumentView breaks one function rather than five
    call sites, and everything else in this file takes a PrintSource.

    The selection is the pages ticked in the LEFT STRIP. The Organizer's grid
    is the same selection by the time it matters: activating a page there
    switches back to the editor and moves the strip with it.
    """
    if view is None:
        return None
    doc = getattr(view, "_doc", None)
    if doc is None or doc.doc is None or doc.page_count() <= 0:
        return None
    panel = getattr(view, "_page_panel", None)
    selection = ()
    if panel is not None:
        try:
            selection = tuple(panel.selected_rows())
        except Exception:
            selection = ()
    try:
        current = int(view.current_page())
    except Exception:
        current = 0
    return PrintSource(
        doc=doc,
        canvas=getattr(view, "_canvas", None),
        current_page=current,
        selection=selection,
        name=view.document_name() or "document",
    )


# ----------------------------------------------------------------------
# The dialog
# ----------------------------------------------------------------------


@dataclass
class PrintChoice:
    """What the options dialog came back with."""

    pages: list = field(default_factory=list)
    options: PrintOptions = field(default_factory=PrintOptions)
    preview: bool = False


class PrintOptionsDialog(QDialog):
    """What to print and how it sits on the paper. The printer comes after.

    TWO STEPS, AND THIS IS THE FIRST. The second is QPrintDialog, which is the
    system's own and owns the printer, the paper, the copies and the collation.
    Everything here is a question that dialog has no field for: which pages of
    THIS document, whether a drawing is shrunk to the sheet or printed at its
    true size, and whether each page picks its own orientation. Putting them in
    front of the printer dialog rather than behind it means the printer dialog
    is the last thing touched before paper comes out, which is where every
    other app puts it.

    The range choices mirror the four a print dialog offers, including
    Selection, which is greyed out when nothing is ticked in the page strip
    rather than hidden, so it is visible as something that exists.
    """

    def __init__(self, parent, source: PrintSource,
                 options: PrintOptions | None = None,
                 range_mode: str = RANGE_ALL, custom: str = ""):
        super().__init__(parent)
        self._source = source
        self._choice = PrintChoice()
        self.setWindowTitle("Print")
        self.setModal(True)

        options = options or PrintOptions()
        count = source.page_count

        column = QVBoxLayout(self)

        pages_box = QGroupBox("Pages")
        pages_layout = QVBoxLayout(pages_box)
        self._range_group = QButtonGroup(self)
        self._range_btns = {}
        for mode, label in (
            (RANGE_ALL, f"All {count} page{'' if count == 1 else 's'}"),
            (RANGE_CURRENT, f"Current page ({source.current_page + 1})"),
            (RANGE_SELECTION,
             f"Selected pages ({len(source.selection)})"),
            (RANGE_CUSTOM, "Pages"),
        ):
            btn = QRadioButton(label)
            self._range_group.addButton(btn)
            self._range_btns[mode] = btn
            if mode == RANGE_CUSTOM:
                row = QHBoxLayout()
                row.addWidget(btn)
                self._custom = QLineEdit(custom)
                self._custom.setPlaceholderText("1-5, 8, 12-")
                self._custom.textEdited.connect(
                    lambda _: self._range_btns[RANGE_CUSTOM].setChecked(True))
                row.addWidget(self._custom, 1)
                pages_layout.addLayout(row)
            else:
                pages_layout.addWidget(btn)
        self._range_btns[RANGE_SELECTION].setEnabled(source.has_selection())
        if range_mode == RANGE_SELECTION and not source.has_selection():
            range_mode = RANGE_ALL
        self._range_btns.get(range_mode, self._range_btns[RANGE_ALL]).setChecked(True)
        column.addWidget(pages_box)

        size_box = QGroupBox("Size")
        size_layout = QVBoxLayout(size_box)
        self._scale_group = QButtonGroup(self)
        self._scale_btns = {}
        for mode, label, tip in (
            (FIT_PAGE, "Fit to page",
             "Shrink or grow each page to the paper, keeping its shape. "
             "Nothing is cut off."),
            (ACTUAL_SIZE, "Actual size",
             "Print at the page's true size. Anything bigger than the paper "
             "loses its edges."),
        ):
            btn = QRadioButton(label)
            btn.setToolTip(tip)
            self._scale_group.addButton(btn)
            self._scale_btns[mode] = btn
            size_layout.addWidget(btn)
        self._scale_btns.get(options.scale_mode,
                             self._scale_btns[FIT_PAGE]).setChecked(True)
        self._orientation = QCheckBox("Match each page's orientation")
        self._orientation.setToolTip(
            "A pack of A4 check sheets with A3 drawings in it prints each "
            "sheet the way round its page wants. Turn this off to print "
            "everything the way the printer is set.")
        self._orientation.setChecked(options.match_page_orientation)
        size_layout.addWidget(self._orientation)
        column.addWidget(size_box)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #c04040;")
        self._error.hide()
        column.addWidget(self._error)

        buttons = QDialogButtonBox()
        self._preview_btn = buttons.addButton(
            "Preview…", QDialogButtonBox.ButtonRole.ActionRole)
        self._print_btn = buttons.addButton(
            "Print…", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._print_btn.setDefault(True)
        self._preview_btn.clicked.connect(lambda: self._commit(preview=True))
        buttons.accepted.connect(lambda: self._commit(preview=False))
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

    def range_mode(self) -> str:
        for mode, btn in self._range_btns.items():
            if btn.isChecked():
                return mode
        return RANGE_ALL

    def custom_text(self) -> str:
        return self._custom.text()

    def options(self) -> PrintOptions:
        mode = FIT_PAGE
        for name, btn in self._scale_btns.items():
            if btn.isChecked():
                mode = name
        return PrintOptions(scale_mode=mode,
                            match_page_orientation=self._orientation.isChecked())

    def choice(self) -> PrintChoice:
        """What was chosen. Only meaningful after the dialog was accepted."""
        return self._choice

    def _commit(self, preview: bool):
        """Resolve the range and accept, or say what is wrong and stay open.

        The range is turned into pages HERE rather than after the dialog
        closes, because "8-3-1 is not a page or a range" is only useful while
        the box holding it is still on screen.
        """
        try:
            pages = resolve_pages(self.range_mode(), self._source.page_count,
                                  self._source.current_page,
                                  self._source.selection, self.custom_text())
        except PageRangeError as exc:
            self._error.setText(str(exc))
            self._error.show()
            if self.range_mode() == RANGE_CUSTOM:
                # Put the cursor back in the box that is wrong. Only that box:
                # the other three cannot be typed into, so moving focus there
                # would be moving it away from nothing the user can fix.
                self._custom.setFocus()
            return
        self._choice = PrintChoice(pages=pages, options=self.options(),
                                   preview=preview)
        self.accept()


# ----------------------------------------------------------------------
# The whole gesture, from Ctrl+P to paper
# ----------------------------------------------------------------------


def _run_with_progress(window, render, pages, printer, options) -> PrintResult:
    """Run a job, with a progress dialog once it is long enough to want one.

    The dialog is the ONLY thing keeping a long print answerable. Everything in
    this app runs on the GUI thread, so without the `processEvents` in the
    progress callback a 500-page job is a frozen window with no way out; with
    it, the window repaints between pages and the Cancel button is read.
    """
    if len(pages) <= PROGRESS_AFTER_PAGES:
        return print_document(render, pages, printer, options)

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
        return print_document(render, pages, printer, options,
                              on_progress=report,
                              is_cancelled=progress.wasCanceled)
    finally:
        progress.close()


def _preview(window, render, pages, printer, options) -> PrintResult:
    """Show the print in a preview window before anything reaches paper.

    Capped at PREVIEW_PAGE_CAP pages, and the cap is stated rather than
    hidden: QPrintPreviewDialog renders everything it is given up front on the
    GUI thread, so an uncapped preview of a 500-page pack is a hang. What
    prints afterwards is the full range; only the picture is short.
    """
    shown = pages[:PREVIEW_PAGE_CAP]
    result = PrintResult()

    def paint(target_printer):
        # The preview asks for a repaint on every zoom and page turn, so this
        # runs more than once. Nothing here keeps state between calls.
        outcome = print_document(render, shown, target_printer, options)
        result.printed = outcome.printed
        result.clipped = outcome.clipped
        result.error = outcome.error

    dialog = QPrintPreviewDialog(printer, window)
    dialog.setWindowTitle(
        f"Print Preview: first {len(shown)} of {len(pages)} pages"
        if len(shown) < len(pages) else "Print Preview")
    dialog.paintRequested.connect(paint)
    dialog.exec()
    return result


def print_active_document(window, view=None) -> PrintResult:
    """File > Print… (Ctrl+P). The whole gesture, in one call.

    THE CLONE IS BUILT ONCE AND CLOSED ON EVERY PATH, including a cancelled
    printer dialog and a preview the user backed out of. It is a full copy of
    the document in memory, so leaking one per Ctrl+P would be a real leak on
    the A1 drawings this app is for.
    """
    source = source_from_view(view if view is not None else window.view)
    if source is None:
        return PrintResult(error="Open a PDF before printing.")

    dialog = PrintOptionsDialog(window, source,
                               options=getattr(window, "_print_options", None),
                               range_mode=getattr(window, "_print_range",
                                                  RANGE_ALL),
                               custom=getattr(window, "_print_custom", ""))
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return PrintResult(cancelled=True)
    choice = dialog.choice()
    # Remembered for the rest of this window's life. A pack is printed a few
    # times in a row and the second Ctrl+P should not start from scratch.
    window._print_options = choice.options
    window._print_range = dialog.range_mode()
    window._print_custom = dialog.custom_text()

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setDocName(source.name)
    render = None
    try:
        render = markup_baked_copy(source.doc, source.canvas)
        if choice.preview:
            return _preview(window, render, choice.pages, printer,
                            choice.options)
        print_dialog = QPrintDialog(printer, window)
        print_dialog.setWindowTitle("Print")
        if print_dialog.exec() != QDialog.DialogCode.Accepted:
            return PrintResult(cancelled=True)
        return _run_with_progress(window, render, choice.pages, printer,
                                  choice.options)
    except Exception as exc:                      # noqa: BLE001
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

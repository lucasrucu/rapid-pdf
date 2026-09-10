"""Printing: the range, the fit, the loop, and a real print into a PDF.

WHAT IS TESTABLE AND WHAT IS NOT. There is no physical printer here and there
is no way to look at paper, so the honest split is:

  - the PAGE RANGE resolver and the FIT arithmetic are plain functions with no
    Qt device behind them, so they are tested exhaustively;
  - the PAGE LOOP is tested through a fake printer that records what it was
    asked to do, which is what proves it renders one page at a time and stops
    when cancelled;
  - the END TO END is a real QPrinter in PdfFormat writing a real file. That is
    the same QPrinter class, the same QPainter, the same page layout and the
    same paint engine plumbing a hardware print goes through; only the backend
    at the very bottom differs. Page count, page sizes, where the drawing
    landed on the sheet and whether the markup is on it are all readable back
    out of that file, so all four are asserted.

ONE DIALOG. Ctrl+P opens the system print dialog and nothing else, so the last
section asserts exactly that: one exec, and it is QPrintDialog's. Every other
modal exec in the path is booby-trapped to fail the test rather than open.

WHAT IS NOT TESTED, and is not claimed to be:

  - QPrintDialog actually opening. It is modal and on Windows it is the system
    dialog; a test that opens it hangs the suite. `print_active_document` is
    exercised with its `exec` patched, and what the patched exec leaves on the
    QPrinter is what the real dialog would leave there.
  - that a real printer driver honours a page-size change between pages. The
    PDF backend does (asserted below); a driver that ignores it prints on
    whatever is in the tray, which is what it would have done anyway.
  - colour management, copies, duplex, stapling, trays. None of it is ours: it
    belongs to the system dialog and is carried on the QPrinter untouched.
"""

from pathlib import Path

import fitz
import pytest

from PySide6.QtCore import QRect
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtPrintSupport import (
    QAbstractPrintDialog, QPrintDialog, QPrinter, QPrintPreviewDialog,
)
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox

import ui.print_support as ps
from ui.print_support import (
    MAX_RENDER_MEGAPIXELS, PRINT_DPI_CAP,
    RANGE_ALL, RANGE_CURRENT, RANGE_CUSTOM,
    PageRangeError, PrintResult, close_render, fit_page, markup_baked_copy,
    orientation_for, pages_from_printer, parse_page_range,
    print_active_document, print_document, render_zoom, resolve_pages,
    source_from_view,
)
from core.pdf_document import PDFDocument
from ui.main_window import MainWindow


A4 = (595.0, 842.0)
A3 = (842.0, 1191.0)
A1_LANDSCAPE = (2384.0, 1684.0)


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# Documents to print
# ---------------------------------------------------------------------------

def _make_pdf(path, sizes):
    """A PDF whose pages are the given (width, height) point sizes."""
    doc = fitz.open()
    for index, (width, height) in enumerate(sizes):
        page = doc.new_page(width=width, height=height)
        page.insert_text((30, 60), f"page {index + 1}", fontsize=24)
    doc.save(str(path))
    doc.close()
    return str(path)


def _inked_pdf(path, sizes):
    """Pages of the given sizes, each covered edge to edge in black.

    Ink at the edges is the point: `_ink_bounds` can only say where a page
    landed on the sheet if the page has something at its corners, and the
    ordinary fixture pages carry one small label near the top left.
    """
    doc = fitz.open()
    for width, height in sizes:
        page = doc.new_page(width=width, height=height)
        page.draw_rect(fitz.Rect(0, 0, width, height),
                       color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def mixed_pdf(tmp_path):
    """A commissioning pack in miniature: A4 sheets around one A1 drawing."""
    return _make_pdf(tmp_path / "mixed.pdf", [A4, A1_LANDSCAPE, A4])


@pytest.fixture
def a4_pdf(tmp_path):
    return _make_pdf(tmp_path / "a4.pdf", [A4] * 3)


def _open(path) -> PDFDocument:
    doc = PDFDocument()
    assert doc.open(path)
    return doc


def _pdf_printer(path, page_size=QPageSize.PageSizeId.A4, dpi=None) -> QPrinter:
    """A real QPrinter that writes a file instead of driving hardware.

    Full page with no margins, so an assertion about where a page landed is an
    assertion about the arithmetic and not about a driver's idea of a margin.
    """
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    printer.setPageSize(QPageSize(page_size))
    printer.setFullPage(True)
    printer.setPageMargins(printer.pageLayout().margins(),
                           QPageLayout.Unit.Point)
    if dpi:
        printer.setResolution(dpi)
    return printer


# ---------------------------------------------------------------------------
# 1. The page range resolver
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("1", [0]),
    ("3", [2]),
    ("1,2,3", [0, 1, 2]),
    ("1-3", [0, 1, 2]),
    ("1 - 3", [0, 1, 2]),            # a range split over spaces is still one
    ("1-3,5", [0, 1, 2, 4]),
    ("1-3, 5", [0, 1, 2, 4]),
    ("1;5", [0, 4]),
    ("2 4 6", [1, 3, 5]),
    ("5-", [4, 5, 6, 7, 8, 9]),      # to the end
    ("-3", [0, 1, 2]),               # from the start
    ("3-1", [0, 1, 2]),              # backwards, and there is only one meaning
    ("2,2,2", [1]),                  # duplicates collapse
    ("5,1", [0, 4]),                 # always ascending
    ("1-999", list(range(10))),      # past the end clamps
    ("  4  ", [3]),
    ("1-2,2-3", [0, 1, 2]),          # overlapping ranges merge
])
def test_parse_page_range_reads_what_a_person_types(text, expected):
    assert parse_page_range(text, 10) == expected


def test_a_range_split_over_spaces_is_not_two_pages():
    """Whitespace separates pages everywhere except around a dash."""
    assert parse_page_range("1 - 3", 10) == [0, 1, 2]
    assert parse_page_range("1 3", 10) == [0, 2]


@pytest.mark.parametrize("text", [
    "", "   ", "0", "abc", "1-2-3", "-", "1,,x", "1-a", "1.5",
])
def test_parse_page_range_refuses_malformed_input(text):
    with pytest.raises(PageRangeError):
        parse_page_range(text, 10)


def test_a_trailing_comma_alone_is_not_an_error():
    """Empty tokens between separators are skipped, not refused."""
    assert parse_page_range("1,2,", 10) == [0, 1]


def test_every_page_past_the_end_is_an_error_not_an_empty_print():
    with pytest.raises(PageRangeError) as caught:
        parse_page_range("50-60", 10)
    assert "10 pages" in str(caught.value)


def test_a_range_that_straddles_the_end_keeps_what_exists():
    assert parse_page_range("8-20", 10) == [7, 8, 9]


def test_parse_page_range_refuses_a_document_with_no_pages():
    with pytest.raises(PageRangeError):
        parse_page_range("1", 0)


def test_the_error_is_a_finished_sentence():
    """The message goes straight in front of a user, so it has to read as one."""
    with pytest.raises(PageRangeError) as caught:
        parse_page_range("banana", 10)
    message = str(caught.value)
    assert message.endswith(".")
    assert "banana" in message


@pytest.mark.parametrize("mode,expected", [
    (RANGE_ALL, [0, 1, 2, 3, 4]),
    (RANGE_CURRENT, [2]),
])
def test_resolve_pages_covers_the_modes_that_need_no_typing(mode, expected):
    assert resolve_pages(mode, 5, current_page=2) == expected


def test_a_current_page_off_the_end_is_an_error():
    with pytest.raises(PageRangeError):
        resolve_pages(RANGE_CURRENT, 5, current_page=9)


def test_resolve_pages_hands_the_custom_string_to_the_parser():
    assert resolve_pages(RANGE_CUSTOM, 10, custom="2-4") == [1, 2, 3]


def test_resolve_pages_rejects_a_mode_it_does_not_know():
    with pytest.raises(PageRangeError):
        resolve_pages("sideways", 5)


def test_resolve_pages_refuses_a_document_with_no_pages():
    with pytest.raises(PageRangeError):
        resolve_pages(RANGE_ALL, 0)


# ---------------------------------------------------------------------------
# 2. Reading the range back off the system dialog
# ---------------------------------------------------------------------------

def test_all_pages_is_the_whole_document(qt_app, tmp_path):
    printer = _pdf_printer(tmp_path / "out.pdf")
    printer.setPrintRange(QPrinter.PrintRange.AllPages)
    assert pages_from_printer(printer, 4) == [0, 1, 2, 3]


def test_a_range_from_the_dialog_becomes_those_pages(qt_app, tmp_path):
    printer = _pdf_printer(tmp_path / "out.pdf")
    printer.setPrintRange(QPrinter.PrintRange.PageRange)
    printer.setFromTo(2, 3)
    assert pages_from_printer(printer, 5) == [1, 2]


def test_the_current_page_from_the_dialog_is_the_page_on_screen(qt_app,
                                                                tmp_path):
    printer = _pdf_printer(tmp_path / "out.pdf")
    printer.setPrintRange(QPrinter.PrintRange.CurrentPage)
    assert pages_from_printer(printer, 5, current_page=3) == [3]


def test_an_empty_range_from_the_dialog_is_the_whole_document(qt_app,
                                                              tmp_path):
    """Qt reports 0 to 0 when the dialog's range boxes were never filled in."""
    printer = _pdf_printer(tmp_path / "out.pdf")
    printer.setPrintRange(QPrinter.PrintRange.PageRange)
    printer.setFromTo(0, 0)
    assert pages_from_printer(printer, 3) == [0, 1, 2]


def test_a_selection_the_dialog_cannot_offer_falls_back_to_everything(
        qt_app, tmp_path):
    """PrintSelection is never enabled, so this can only arrive by accident."""
    printer = _pdf_printer(tmp_path / "out.pdf")
    printer.setPrintRange(QPrinter.PrintRange.Selection)
    assert pages_from_printer(printer, 3) == [0, 1, 2]


def test_a_range_past_the_end_of_the_document_clamps(qt_app, tmp_path):
    printer = _pdf_printer(tmp_path / "out.pdf")
    printer.setPrintRange(QPrinter.PrintRange.PageRange)
    printer.setFromTo(2, 99)
    assert pages_from_printer(printer, 4) == [1, 2, 3]


# ---------------------------------------------------------------------------
# 3. Fitting a page to the paper
# ---------------------------------------------------------------------------

def _aspect(size):
    return size[0] / size[1]


@pytest.mark.parametrize("page", [A4, A3, A1_LANDSCAPE, (100.0, 100.0)])
@pytest.mark.parametrize("paper", [QRect(0, 0, 2480, 3508),      # A4 at 300dpi
                                   QRect(0, 0, 3508, 2480)])     # A4 landscape
def test_fit_keeps_the_aspect_ratio_and_stays_inside_the_paper(page, paper):
    fit = fit_page(page[0], page[1], paper)
    assert fit.dest.width() / fit.dest.height() == pytest.approx(_aspect(page))
    assert fit.dest.width() <= paper.width() + 1e-6
    assert fit.dest.height() <= paper.height() + 1e-6
    assert fit.dest.left() >= paper.x() - 1e-6
    assert fit.dest.top() >= paper.y() - 1e-6


@pytest.mark.parametrize("page", [A4, A3, A1_LANDSCAPE])
def test_fit_touches_one_pair_of_edges_so_nothing_is_wasted(page):
    """Fit is the LARGEST that fits: one axis has to reach the paper exactly."""
    paper = QRect(0, 0, 2480, 3508)
    fit = fit_page(page[0], page[1], paper)
    fills_width = fit.dest.width() == pytest.approx(paper.width())
    fills_height = fit.dest.height() == pytest.approx(paper.height())
    assert fills_width or fills_height


def test_fit_centres_what_is_left_over():
    paper = QRect(0, 0, 3508, 2480)             # landscape paper
    fit = fit_page(*A4, paper)                  # portrait page
    left = fit.dest.left()
    right = paper.width() - fit.dest.right()
    assert left == pytest.approx(right)
    assert fit.dest.top() == pytest.approx(0.0)


def test_fit_grows_a_small_page_as_well_as_shrinking_a_big_one():
    """An A5 check sheet on A4 paper is scaled UP, not left in the corner."""
    paper = QRect(0, 0, 2480, 3508)
    fit = fit_page(420.0, 595.0, paper)
    assert fit.scale > 1.0
    assert fit.dest.height() == pytest.approx(paper.height())


def test_an_a1_drawing_on_a4_paper_is_not_cropped():
    """The case this whole feature is for. Fit, and every edge survives."""
    paper = QRect(0, 0, 2480, 3508)
    fit = fit_page(*A1_LANDSCAPE, paper)
    assert fit.dest.width() == pytest.approx(paper.width())
    assert fit.dest.height() < paper.height()
    assert fit.dest.width() / fit.dest.height() == pytest.approx(
        _aspect(A1_LANDSCAPE))


def test_fit_is_the_only_mode_there_is():
    """Actual size went with the options dialog. Nothing can ask for a crop."""
    paper = QRect(0, 0, 2480, 3508)
    for page in (A4, A3, A1_LANDSCAPE, (100.0, 100.0)):
        fit = fit_page(page[0], page[1], paper)
        assert fit.dest.width() <= paper.width() + 1e-6
        assert fit.dest.height() <= paper.height() + 1e-6


def test_fit_offsets_by_the_target_origin():
    """Margins arrive as a target rectangle that does not start at zero."""
    paper = QRect(100, 200, 2480, 3508)
    fit = fit_page(*A4, paper)
    assert fit.dest.left() >= 100
    assert fit.dest.top() >= 200


@pytest.mark.parametrize("page_w,page_h", [(0, 100), (100, 0), (-5, 10)])
def test_fit_refuses_a_page_with_no_size(page_w, page_h):
    with pytest.raises(ValueError):
        fit_page(page_w, page_h, QRect(0, 0, 100, 100))


def test_fit_refuses_paper_with_no_printable_area():
    with pytest.raises(ValueError):
        fit_page(*A4, QRect(0, 0, 0, 0))


# ---------------------------------------------------------------------------
# 4. Orientation, per page
# ---------------------------------------------------------------------------

def test_orientation_follows_the_page_not_the_document():
    assert orientation_for(*A4) == QPageLayout.Orientation.Portrait
    assert orientation_for(*A1_LANDSCAPE) == QPageLayout.Orientation.Landscape
    assert orientation_for(100, 100) == QPageLayout.Orientation.Portrait


def test_orientation_reads_the_rotated_size(tmp_path):
    """A portrait page carrying /Rotate 90 is read as landscape.

    get_page_size uses bound(), which is the size AFTER rotation, so this needs
    no special case anywhere. The test is here because losing it would be
    silent: the page would just print sideways.
    """
    path = _make_pdf(tmp_path / "rot.pdf", [A4])
    raw = fitz.open(path)
    raw[0].set_rotation(90)
    raw.saveIncr()
    raw.close()

    doc = _open(path)
    try:
        width, height = doc.get_page_size(0)
        assert width > height
        assert orientation_for(width, height) == QPageLayout.Orientation.Landscape
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 5. The render resolution
# ---------------------------------------------------------------------------

def test_render_zoom_caps_the_paper_resolution():
    """1:1 with a 1200 DPI device is 139 megapixels for one A4 sheet."""
    paper = QRect(0, 0, 9916, 14033)                    # A4 at 1200 dpi
    fit = fit_page(*A4, paper)
    zoom = render_zoom(*A4, fit, 1200)
    assert zoom * 72 == pytest.approx(PRINT_DPI_CAP, rel=0.02)
    megapixels = (A4[0] * zoom) * (A4[1] * zoom) / 1e6
    assert megapixels < 10


def test_render_zoom_leaves_a_low_resolution_printer_alone():
    paper = QRect(0, 0, 1240, 1754)                     # A4 at 150 dpi
    fit = fit_page(*A4, paper)
    assert render_zoom(*A4, fit, 150) == pytest.approx(fit.scale)


def test_render_zoom_holds_the_megapixel_budget_on_big_paper():
    """A1 paper at 300 DPI is 70 megapixels, which is where the budget bites."""
    paper = QRect(0, 0, 9933, 7016)                     # A1 at 300 dpi
    fit = fit_page(*A1_LANDSCAPE, paper)
    zoom = render_zoom(*A1_LANDSCAPE, fit, 300)
    megapixels = (A1_LANDSCAPE[0] * zoom) * (A1_LANDSCAPE[1] * zoom) / 1e6
    assert megapixels == pytest.approx(MAX_RENDER_MEGAPIXELS, rel=0.01)
    assert zoom < fit.scale


def test_a_big_drawing_on_small_paper_costs_what_the_paper_costs():
    """The point of deriving the zoom from the DESTINATION rather than the page.

    An A1 drawing fitted onto A4 rasterises to an A4-sized image. Rendering it
    at 300 DPI of its own size would be eight times the pixels for no more ink
    on the paper.
    """
    paper = QRect(0, 0, 9916, 14033)                    # A4 at 1200 dpi
    fit = fit_page(*A1_LANDSCAPE, paper)
    zoom = render_zoom(*A1_LANDSCAPE, fit, 1200)
    megapixels = (A1_LANDSCAPE[0] * zoom) * (A1_LANDSCAPE[1] * zoom) / 1e6
    assert megapixels < 10


def test_render_zoom_never_returns_zero():
    """A zero zoom is an empty pixmap, which prints as a blank sheet in silence."""
    paper = QRect(0, 0, 1, 1)
    fit = fit_page(*A1_LANDSCAPE, paper)
    assert render_zoom(*A1_LANDSCAPE, fit, 72) > 0


# ---------------------------------------------------------------------------
# 6. The page loop: one page at a time, and cancellable
# ---------------------------------------------------------------------------

class _CountingDocument(PDFDocument):
    """A document that records every render, so the loop can be watched.

    Also records the LIVE count, which is what proves nothing is built up
    front: a loop that rendered every page before drawing any would leave the
    high-water mark at len(pages) rather than at 1.
    """

    def __init__(self):
        super().__init__()
        self.renders = []
        self.live = 0
        self.peak_live = 0

    def render_page(self, page_num, zoom=1.5):
        self.renders.append((page_num, zoom))
        self.live += 1
        self.peak_live = max(self.peak_live, self.live)
        pixmap = super().render_page(page_num, zoom)
        self.live -= 1
        return pixmap


def _counting_open(path) -> _CountingDocument:
    doc = _CountingDocument()
    assert doc.open(path)
    return doc


def test_the_loop_renders_each_page_once_in_order(qt_app, a4_pdf, tmp_path):
    doc = _counting_open(a4_pdf)
    printer = _pdf_printer(tmp_path / "out.pdf")
    try:
        result = print_document(doc, [0, 1, 2], printer)
    finally:
        doc.close()
    assert result.printed == 3
    assert [page for page, _ in doc.renders] == [0, 1, 2]
    assert doc.peak_live == 1, "more than one page was in memory at once"


def test_the_loop_reports_progress_after_every_page(qt_app, a4_pdf, tmp_path):
    doc = _open(a4_pdf)
    printer = _pdf_printer(tmp_path / "out.pdf")
    seen = []
    try:
        print_document(doc, [0, 1, 2], printer,
                       on_progress=lambda done, total: seen.append((done, total)))
    finally:
        doc.close()
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_cancelling_stops_the_loop_where_it_was(qt_app, a4_pdf, tmp_path):
    """Cancel is read BEFORE a page is rendered, so it costs nothing to obey."""
    doc = _counting_open(a4_pdf)
    printer = _pdf_printer(tmp_path / "out.pdf")
    state = {"pages": 0}

    def progress(done, _total):
        state["pages"] = done

    try:
        result = print_document(doc, [0, 1, 2], printer,
                                on_progress=progress,
                                is_cancelled=lambda: state["pages"] >= 2)
    finally:
        doc.close()
    assert result.cancelled
    assert result.printed == 2
    assert [page for page, _ in doc.renders] == [0, 1]


def test_cancelling_before_the_first_page_prints_nothing(qt_app, a4_pdf,
                                                         tmp_path):
    doc = _counting_open(a4_pdf)
    printer = _pdf_printer(tmp_path / "out.pdf")
    try:
        result = print_document(doc, [0, 1, 2], printer,
                                is_cancelled=lambda: True)
    finally:
        doc.close()
    assert result.cancelled
    assert result.printed == 0
    assert doc.renders == []


def test_an_empty_range_is_refused_rather_than_printed(qt_app, a4_pdf,
                                                       tmp_path):
    doc = _open(a4_pdf)
    printer = _pdf_printer(tmp_path / "out.pdf")
    try:
        result = print_document(doc, [], printer)
    finally:
        doc.close()
    assert result.printed == 0
    assert result.error


def test_printing_a_closed_document_is_an_error_not_a_crash(qt_app, tmp_path):
    printer = _pdf_printer(tmp_path / "out.pdf")
    assert print_document(PDFDocument(), [0], printer).error


# ---------------------------------------------------------------------------
# 7. End to end, into a real PDF
# ---------------------------------------------------------------------------

def test_a_real_print_produces_one_sheet_per_page(qt_app, a4_pdf, tmp_path):
    out = tmp_path / "printed.pdf"
    doc = _open(a4_pdf)
    try:
        result = print_document(doc, [0, 1, 2], _pdf_printer(out))
    finally:
        doc.close()
    assert result.error is None
    assert result.printed == 3

    printed = fitz.open(str(out))
    try:
        assert printed.page_count == 3
    finally:
        printed.close()


def test_a_range_prints_only_that_range(qt_app, a4_pdf, tmp_path):
    out = tmp_path / "printed.pdf"
    doc = _open(a4_pdf)
    try:
        print_document(doc, resolve_pages(RANGE_CUSTOM, 3, custom="2-3"),
                       _pdf_printer(out))
    finally:
        doc.close()
    printed = fitz.open(str(out))
    try:
        assert printed.page_count == 2
    finally:
        printed.close()


def test_each_sheet_takes_the_orientation_of_its_own_page(qt_app, mixed_pdf,
                                                          tmp_path):
    """A4, A1 landscape, A4 goes out as portrait, landscape, portrait.

    The whole reason a commissioning pack is printable in one go, and now
    permanent behaviour with no control in front of it, because the system
    dialog has one Orientation setting for the whole job and cannot say this.
    Asserted on the file rather than on a flag, because Qt has to carry a page
    layout change across newPage() for it to be true, and that is a property of
    Qt.
    """
    out = tmp_path / "printed.pdf"
    doc = _open(mixed_pdf)
    try:
        print_document(doc, [0, 1, 2], _pdf_printer(out))
    finally:
        doc.close()

    printed = fitz.open(str(out))
    try:
        shapes = [(page.rect.width > page.rect.height) for page in printed]
    finally:
        printed.close()
    assert shapes == [False, True, False]


def test_the_paper_size_is_the_printers_not_the_documents(qt_app, mixed_pdf,
                                                          tmp_path):
    """An A1 drawing printed on A4 comes out on A4, shrunk, not on A1."""
    out = tmp_path / "printed.pdf"
    doc = _open(mixed_pdf)
    try:
        print_document(doc, [1], _pdf_printer(out))
    finally:
        doc.close()
    printed = fitz.open(str(out))
    try:
        rect = printed[0].rect
        # A4 landscape, in points, to the nearest whole one.
        assert (round(rect.width), round(rect.height)) == (842, 595)
    finally:
        printed.close()


def _ink_bounds(pdf_path, page=0, dpi=72):
    """The bounding box of everything that is not white on a printed page."""
    doc = fitz.open(str(pdf_path))
    try:
        pix = doc[page].get_pixmap(dpi=dpi, alpha=False)
        left, top = pix.width, pix.height
        right = bottom = -1
        for y in range(pix.height):
            for x in range(pix.width):
                r, g, b = pix.pixel(x, y)
                if r < 240 or g < 240 or b < 240:
                    left, top = min(left, x), min(top, y)
                    right, bottom = max(right, x), max(bottom, y)
        return left, top, right, bottom, pix.width, pix.height
    finally:
        doc.close()


def test_a_fitted_page_is_centred_on_the_sheet(qt_app, tmp_path):
    """A 2:1 page on a 1.41:1 sheet leaves equal white above and below.

    Read off the printed pixels rather than off the arithmetic, so it covers
    the drawing as well as the fit: a page drawn at the wrong offset, or drawn
    into a stale viewport left over from the page before, fails here.
    """
    source = _inked_pdf(tmp_path / "wide.pdf", [(800.0, 400.0)])
    out = tmp_path / "printed.pdf"
    doc = _open(source)
    try:
        print_document(doc, [0], _pdf_printer(out))
    finally:
        doc.close()

    left, top, right, bottom, width, height = _ink_bounds(out)
    assert width > height, "the sheet should have turned landscape for it"
    assert left <= 1 and right >= width - 2, "the fit should fill the width"
    above, below = top, height - 1 - bottom
    assert abs(above - below) <= 2, f"not centred: {above} above, {below} below"


def test_the_second_page_fills_its_own_sheet_after_an_orientation_change(
        qt_app, tmp_path):
    """QPainter.viewport() goes stale across a page-layout change.

    It still reports the FIRST sheet's rectangle on the second sheet, so a loop
    that trusted it would fit the A1 landscape page to a portrait A4 rectangle
    and draw it into the middle of a landscape sheet with white down both
    sides. The destination is re-read from the page layout every page for
    exactly this reason, and the proof is that the ink reaches both ends.
    """
    source = _inked_pdf(tmp_path / "inked.pdf", [A4, A1_LANDSCAPE])
    out = tmp_path / "printed.pdf"
    doc = _open(source)
    try:
        print_document(doc, [0, 1], _pdf_printer(out))
    finally:
        doc.close()

    left, _top, right, _bottom, width, height = _ink_bounds(out, page=1)
    assert width > height, "sheet two should be landscape"
    assert left <= 2 and right >= width - 3, (
        f"the A1 page only reached {left}..{right} of {width}")


def test_printing_at_a_lower_printer_resolution_still_works(qt_app, a4_pdf,
                                                            tmp_path):
    """Not every device reports 1200. The maths must not assume one number."""
    out = tmp_path / "printed.pdf"
    doc = _open(a4_pdf)
    try:
        result = print_document(doc, [0], _pdf_printer(out, dpi=150))
    finally:
        doc.close()
    assert result.printed == 1
    printed = fitz.open(str(out))
    try:
        assert printed.page_count == 1
    finally:
        printed.close()


# ---------------------------------------------------------------------------
# 8. Unsaved markup reaches the paper
# ---------------------------------------------------------------------------

class _FakeCanvas:
    """A canvas holding one solid red rectangle on page 0, and nothing else.

    Stands in for PDFCanvas so this test is about the print path rather than
    about mouse events. test_the_live_canvas_is_what_gets_printed below uses
    the real one.
    """

    def __init__(self, rect=(50, 50, 300, 300)):
        self._rect = rect

    def get_all_annotation_dicts(self, page_num):
        if page_num != 0:
            return []
        return [{
            "type": "rect",
            "fitz_rect": self._rect,
            "stroke_color": (1.0, 0.0, 0.0),
            "fill_color": (1.0, 0.0, 0.0),
            "opacity": 1.0,
            "line_width": 3,
        }]


def _has_red(pdf_path, page=0):
    doc = fitz.open(str(pdf_path))
    try:
        pix = doc[page].get_pixmap(dpi=72, alpha=False)
        for y in range(pix.height):
            for x in range(pix.width):
                r, g, b = pix.pixel(x, y)
                if r > 180 and g < 90 and b < 90:
                    return True
        return False
    finally:
        doc.close()


def test_unsaved_markup_is_on_the_paper(qt_app, a4_pdf, tmp_path):
    """The requirement this feature is most likely to get wrong.

    Markup lives on the canvas until a save writes it, so printing the file
    would print a blank drawing. The clone is what closes that gap, and the
    only proof is a red pixel in the output.
    """
    doc = _open(a4_pdf)
    render = markup_baked_copy(doc, _FakeCanvas())
    out = tmp_path / "printed.pdf"
    try:
        result = print_document(render, [0], _pdf_printer(out))
    finally:
        close_render(render)
        doc.close()
    assert result.printed == 1
    assert _has_red(out), "the markup did not reach the paper"


def test_the_file_on_disk_is_still_unmarked(qt_app, a4_pdf, tmp_path):
    """Printing is not a save. The clone carries the markup; the file does not."""
    doc = _open(a4_pdf)
    render = markup_baked_copy(doc, _FakeCanvas())
    try:
        assert len(list(render.doc[0].annots())) == 1
        assert len(list(doc.doc[0].annots())) == 0
    finally:
        close_render(render)
        doc.close()
    on_disk = fitz.open(a4_pdf)
    try:
        assert len(list(on_disk[0].annots())) == 0
    finally:
        on_disk.close()


def test_a_canvas_that_raises_does_not_lose_the_print(qt_app, a4_pdf):
    """One unserialisable item must not cost the user the whole document."""

    class _Broken:
        def get_all_annotation_dicts(self, page_num):
            raise RuntimeError("no")

    doc = _open(a4_pdf)
    render = markup_baked_copy(doc, _Broken())
    try:
        assert render.page_count() == 3
    finally:
        close_render(render)
        doc.close()


def test_close_render_is_safe_to_call_twice(qt_app, a4_pdf):
    doc = _open(a4_pdf)
    render = markup_baked_copy(doc, None)
    try:
        close_render(render)
        close_render(render)
        close_render(None)
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 9. The window: the menu entry and the source
# ---------------------------------------------------------------------------

def _build(path=None):
    window = MainWindow()
    if path is not None:
        window.open_paths([path])
    window.view._canvas.resize(600, 700)
    window.view._canvas._flush_pending_render()
    return window


def _dispose(window):
    window.view.clear_document()
    window.view.teardown()
    window.deleteLater()


@pytest.fixture
def win(qt_app, a4_pdf):
    window = _build(a4_pdf)
    yield window
    _dispose(window)


def test_ctrl_p_is_in_the_file_menu(win):
    file_menu = next(m for m in win.menuBar().findChildren(QMenu)
                     if m.title() == "File")
    entries = {a.text(): a for a in file_menu.actions() if a.text()}
    assert "Print…" in entries
    assert entries["Print…"].shortcut().toString() == "Ctrl+P"


def test_the_source_reads_the_document_the_page_and_the_canvas(win):
    win.view.jump_to_page(2)
    source = source_from_view(win.view)
    assert source is not None
    assert source.page_count == 3
    assert source.current_page == win.view.current_page()
    assert source.canvas is win.view._canvas


def test_the_source_no_longer_reads_the_page_strips_ticks(win):
    """Printing a selection went with the options dialog.

    The system dialog has no field that could carry a set of ticked pages, so
    nothing reads them any more and PrintSource does not carry them.
    """
    win.view._page_panel._list.selectAll()
    source = source_from_view(win.view)
    assert not hasattr(source, "selection")
    assert not hasattr(source, "has_selection")


def test_the_source_is_none_for_an_empty_tab(qt_app):
    window = _build()
    try:
        assert source_from_view(window.view) is None
    finally:
        _dispose(window)


def test_printing_an_empty_tab_says_so_instead_of_opening_a_dialog(qt_app,
                                                                   monkeypatch):
    """No document, no dialogs. Nothing here should reach a modal loop."""
    window = _build()
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    try:
        result = window.print_pdf()
    finally:
        _dispose(window)
    assert result.error


def test_the_live_canvas_is_what_gets_printed(win, tmp_path):
    """End to end from the real widgets: draw, print, find the ink.

    Everything above uses a stand-in canvas. This one draws through the real
    PDFCanvas and asserts the mark is on the paper without a save anywhere in
    between, which is the whole claim.
    """
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import Qt as _Qt

    canvas = win.view._canvas
    win.view.trigger_tool("rect")

    def _mouse(kind, scene_pt, button=_Qt.MouseButton.LeftButton):
        vp = canvas.mapFromScene(scene_pt)
        held = (_Qt.MouseButton.NoButton
                if kind == QMouseEvent.Type.MouseButtonRelease else button)
        return QMouseEvent(kind, QPointF(vp), QPointF(vp), button, held,
                           _Qt.KeyboardModifier.NoModifier)

    start = QPointF(60 * canvas._zoom, 60 * canvas._zoom)
    end = QPointF(300 * canvas._zoom, 250 * canvas._zoom)
    canvas.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, start))
    canvas.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, end))
    canvas.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, end))
    assert canvas.get_all_annotation_dicts(0), "the drag drew nothing"
    assert win.view.is_dirty(), "the markup is unsaved, which is the point"
    assert not list(win.view._doc.doc[0].annots()), (
        "the live document must not carry the markup yet")

    source = source_from_view(win.view)
    render = markup_baked_copy(source.doc, source.canvas)
    out = tmp_path / "printed.pdf"
    try:
        result = print_document(render, [0], _pdf_printer(out))
    finally:
        close_render(render)
    assert result.printed == 1

    left, _top, right, _bottom, width, _height = _ink_bounds(out)
    # The drawn rectangle is well inside the page, so the ink now covers more
    # than the little "page 1" label in the corner did.
    assert right - left > width // 4


# ---------------------------------------------------------------------------
# 10. Ctrl+P opens ONE dialog, and it is the system's
# ---------------------------------------------------------------------------

@pytest.fixture
def no_other_dialogs(monkeypatch):
    """Booby-trap every modal exec except QPrintDialog's.

    QPrintDialog defines its own `exec`, so patching QDialog.exec here does not
    reach it: anything else in the print path that tried to open would land in
    the trap instead, which is the assertion this fixture exists to make.
    """
    def trap(self, *args, **kwargs):
        raise AssertionError(
            f"a second dialog opened during a print: {type(self).__name__}")

    monkeypatch.setattr(QDialog, "exec", trap, raising=False)
    monkeypatch.setattr(QPrintPreviewDialog, "exec", trap, raising=False)


def _accept_into(out, opened, page_range=None):
    """A stand-in for the system dialog: record it, aim it at a file, accept."""
    def accept(self):
        opened.append(self)
        printer = self.printer()
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(out))
        if page_range is not None:
            printer.setPrintRange(QPrinter.PrintRange.PageRange)
            printer.setFromTo(*page_range)
        return QDialog.DialogCode.Accepted
    return accept


def test_ctrl_p_opens_exactly_one_dialog(win, monkeypatch, tmp_path,
                                         no_other_dialogs):
    """The whole point of the change. One window, and it is Windows' own."""
    opened = []
    monkeypatch.setattr(ps.QPrintDialog, "exec",
                        _accept_into(tmp_path / "one.pdf", opened))

    result = win.print_pdf()

    assert result.error is None
    assert len(opened) == 1, f"{len(opened)} dialogs opened, expected 1"
    assert isinstance(opened[0], QPrintDialog)


def test_no_options_dialog_is_constructed_anywhere_in_the_path():
    """Not just unopened. Gone, along with everything that fed it."""
    for name in ("PrintOptionsDialog", "PrintOptions", "PrintChoice",
                 "RANGE_SELECTION", "FIT_PAGE", "ACTUAL_SIZE"):
        assert not hasattr(ps, name), f"{name} is still in ui/print_support.py"

    ui_dir = Path(ps.__file__).resolve().parent
    for source in sorted(ui_dir.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        assert "PrintOptionsDialog" not in text, f"{source.name} still names it"
        assert "PrintChoice" not in text, f"{source.name} still names it"


def test_the_window_remembers_no_print_state(win):
    """There is nothing left to remember: the system dialog keeps its own."""
    for name in ("_print_options", "_print_range", "_print_custom"):
        assert not hasattr(win, name), f"{name} is still on the window"


def test_the_dialog_is_bounded_by_the_document_and_offers_the_current_page(
        win, monkeypatch, no_other_dialogs):
    """The range boxes cannot ask for page 9999 of a three-page document."""
    seen = {}

    def inspect(self):
        opt = QAbstractPrintDialog.PrintDialogOption
        seen["min"] = self.minPage()
        seen["max"] = self.maxPage()
        seen["range"] = self.testOption(opt.PrintPageRange)
        seen["current"] = self.testOption(opt.PrintCurrentPage)
        seen["selection"] = self.testOption(opt.PrintSelection)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ps.QPrintDialog, "exec", inspect)
    win.print_pdf()

    assert (seen["min"], seen["max"]) == (1, 3)
    assert seen["range"] is True
    assert seen["current"] is True
    assert seen["selection"] is False, "a selection cannot be expressed here"


def test_cancelling_the_printer_dialog_prints_nothing(win, monkeypatch,
                                                      no_other_dialogs):
    monkeypatch.setattr(ps.QPrintDialog, "exec",
                        lambda self: QDialog.DialogCode.Rejected)
    result = print_active_document(win)
    assert result.cancelled
    assert result.printed == 0


def test_the_gesture_prints_the_whole_document_by_default(win, monkeypatch,
                                                          tmp_path,
                                                          no_other_dialogs):
    out = tmp_path / "all.pdf"
    monkeypatch.setattr(ps.QPrintDialog, "exec", _accept_into(out, []))

    result = win.print_pdf()

    assert result.error is None
    assert result.printed == 3
    printed = fitz.open(str(out))
    try:
        assert printed.page_count == 3
    finally:
        printed.close()


def test_the_gesture_prints_the_range_the_system_dialog_came_back_with(
        win, monkeypatch, tmp_path, no_other_dialogs):
    """Ctrl+P end to end: the dialog's own page range, a real file at the end."""
    out = tmp_path / "gesture.pdf"
    monkeypatch.setattr(ps.QPrintDialog, "exec",
                        _accept_into(out, [], page_range=(1, 2)))

    result = win.print_pdf()

    assert result.error is None
    assert result.printed == 2
    assert out.exists()
    printed = fitz.open(str(out))
    try:
        assert printed.page_count == 2
    finally:
        printed.close()


def test_a_cancelled_print_never_builds_the_clone(win, monkeypatch,
                                                  no_other_dialogs):
    """A full copy of an A1 pack is not a thing to build and throw away."""
    built = []
    monkeypatch.setattr(ps, "markup_baked_copy",
                        lambda doc, canvas: built.append(1))
    monkeypatch.setattr(ps.QPrintDialog, "exec",
                        lambda self: QDialog.DialogCode.Rejected)

    assert print_active_document(win).cancelled
    assert built == [], "the clone was built for a print that never happened"


def test_the_result_reads_as_one_status_line():
    assert PrintResult(printed=1).message() == "Printed 1 page"
    assert PrintResult(printed=7).message() == "Printed 7 pages"
    assert "cancelled" in PrintResult(printed=2, cancelled=True).message()
    assert PrintResult(error="broken").message() == "broken"

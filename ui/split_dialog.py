"""Split and Extract: the inverse of Combine.

Three things the user can ask for, one dialog, because they are the same
question with different arithmetic behind it:

  - EXTRACT the pages I picked into one new file;
  - SPLIT this document every N pages;
  - SPLIT it at these page numbers, each one starting a new file.

WHAT IS IN HERE AND WHAT IS NOT. Every page-group decision, every file name and
every collision check is in `core/pdf_document.py` (`every_n_groups`,
`groups_at_cuts`, `plan_split`, `write_extract`, `run_split`), because the
commissioning feature that cuts a combined scan into one file per certificate
needs all of it and must never import a dialog to get it. What is here is the
widgets, the parsing of what the user typed, and the two questions only a
person can answer: where the files go, and whether an existing one may be
overwritten.

DRIVABLE WITHOUT exec(). The audit found `ui/combine_dialog.py` is never
imported or instantiated by any test in the repo, and every combine path is
monkeypatched away, so its 363 lines are covered by nothing. That is not copied
here. The dialog is built, filled and RUN by tests/test_split_dialog.py through
the same methods the buttons call: `set_mode`, `set_pages_text`, `set_every`,
`set_out_dir`, `plans()` and `run()`. Nothing needs a modal loop, and the one
place that would ask a question (`run`) takes the answer as a parameter.
"""

from __future__ import annotations

import os
import re

from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox, QPushButton, QRadioButton, QSpinBox, QVBoxLayout,
)

from core.pdf_document import (
    SplitPlan,
    every_n_groups,
    groups_at_cuts,
    page_range_label,
)
from core.settings import dialog_start_dir, remember_dialog_dir

MODE_EXTRACT = "extract"
MODE_EVERY = "every"
MODE_AT = "at"

#: A page spec is one-based and human: "1-3, 7, 10-". Nothing else is accepted,
#: and an unparseable piece is dropped rather than guessed at.
_PIECE = re.compile(r"^\s*(\d+)?\s*(?:(-)\s*(\d+)?)?\s*$")


def parse_page_spec(text: str, page_count: int) -> list[int]:
    """"1-3, 7, 10-" to zero-based page indices, in the order written.

    ONE-BASED IN, ZERO-BASED OUT, because the user counts from 1 and the
    document counts from 0, and the conversion has exactly one home.

    Order is kept and duplicates are kept: "5, 1" really does mean page 5 then
    page 1, and a user who asks for the same page twice gets it twice. Out of
    range numbers are dropped silently: a spec typed against a 10 page document
    that says "1-99" means "the rest of it", which is what an open range does
    anyway.
    """
    out: list[int] = []
    count = int(page_count)
    if count <= 0:
        return out
    for piece in str(text or "").replace(";", ",").split(","):
        if not piece.strip():
            continue
        match = _PIECE.match(piece)
        if not match:
            continue
        first, dash, last = match.groups()
        if not dash:
            if first is None:
                continue
            index = int(first) - 1
            if 0 <= index < count:
                out.append(index)
            continue
        start = int(first) - 1 if first else 0
        end = int(last) - 1 if last else count - 1
        if start > end:
            start, end = end, start
        for index in range(max(0, start), min(count - 1, end) + 1):
            out.append(index)
    return out


def format_page_spec(pages) -> str:
    """Zero-based indices back to the one-based text a user would have typed."""
    rows = sorted({int(p) + 1 for p in pages})
    if not rows:
        return ""
    runs, start, previous = [], rows[0], rows[0]
    for row in rows[1:]:
        if row == previous + 1:
            previous = row
            continue
        runs.append((start, previous))
        start = previous = row
    runs.append((start, previous))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


class SplitDialog(QDialog):
    """Pick what to pull out, see the files it would make, then make them.

    `pdf` is the open PDFDocument. `selected_pages` are zero-based indices the
    user already had picked in the page panel or organizer, used to prefill the
    extract box; None or empty means "the whole document".
    """

    def __init__(self, pdf, selected_pages=None, parent=None):
        super().__init__(parent)
        self._pdf = pdf
        self._report = None
        self.setWindowTitle("Split or Extract Pages")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._build()
        prefill = list(selected_pages or [])
        if prefill:
            self._pages_edit.setText(format_page_spec(prefill))
        else:
            self._pages_edit.setText(f"1-{max(1, pdf.page_count())}")
        self.refresh()

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)

        name = os.path.basename(self._pdf.path or "") or "Untitled document"
        header = QLabel(f"{name}  ({self._pdf.page_count()} pages)")
        header.setStyleSheet("font-weight: 600;")
        layout.addWidget(header)

        # What the new files cannot carry over: a signature, encryption, the
        # owner's restrictions. Said before the user commits, not after.
        warnings = self._pdf.split_warnings()
        self._warning_label = QLabel("\n\n".join(warnings))
        self._warning_label.setWordWrap(True)
        self._warning_label.setVisible(bool(warnings))
        layout.addWidget(self._warning_label)

        self._modes = QButtonGroup(self)
        self._extract_radio = QRadioButton("Extract these pages into one file")
        self._every_radio = QRadioButton("Split every")
        self._at_radio = QRadioButton("Split into a new file at pages")
        for index, button in enumerate(
                (self._extract_radio, self._every_radio, self._at_radio)):
            self._modes.addButton(button, index)
            button.toggled.connect(self.refresh)
        self._extract_radio.setChecked(True)

        extract_row = QHBoxLayout()
        extract_row.addWidget(self._extract_radio)
        self._pages_edit = QLineEdit()
        self._pages_edit.setPlaceholderText("1-3, 7, 10-12")
        self._pages_edit.textChanged.connect(self.refresh)
        extract_row.addWidget(self._pages_edit, 1)
        layout.addLayout(extract_row)

        every_row = QHBoxLayout()
        every_row.addWidget(self._every_radio)
        self._every_spin = QSpinBox()
        self._every_spin.setRange(1, max(1, self._pdf.page_count()))
        self._every_spin.setValue(1)
        self._every_spin.valueChanged.connect(self.refresh)
        every_row.addWidget(self._every_spin)
        every_row.addWidget(QLabel("pages"))
        every_row.addStretch()
        layout.addLayout(every_row)

        at_row = QHBoxLayout()
        at_row.addWidget(self._at_radio)
        self._cuts_edit = QLineEdit()
        self._cuts_edit.setPlaceholderText("4, 9, 15")
        self._cuts_edit.textChanged.connect(self.refresh)
        at_row.addWidget(self._cuts_edit, 1)
        layout.addLayout(at_row)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Save into"))
        self._folder_edit = QLineEdit(self._default_dir())
        self._folder_edit.textChanged.connect(self.refresh)
        folder_row.addWidget(self._folder_edit, 1)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        self._preview = QListWidget()
        self._preview.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._preview.setMinimumHeight(150)
        layout.addWidget(self._preview, 1)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self._go = QPushButton("Split")
        self._go.setDefault(True)
        self._go.clicked.connect(self._on_go)
        buttons.addWidget(self._go)
        layout.addLayout(buttons)

    def _default_dir(self) -> str:
        try:
            return dialog_start_dir(self._pdf.path)
        except Exception:
            return os.path.dirname(os.path.abspath(self._pdf.path or "")) or os.getcwd()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Save the new files into", self._folder_edit.text()
            or self._default_dir())
        if folder:
            self._folder_edit.setText(folder)
            try:
                remember_dialog_dir(os.path.join(folder, "x.pdf"))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # The state a test drives, and the buttons drive too
    # ------------------------------------------------------------------

    def mode(self) -> str:
        if self._every_radio.isChecked():
            return MODE_EVERY
        if self._at_radio.isChecked():
            return MODE_AT
        return MODE_EXTRACT

    def set_mode(self, mode: str):
        {MODE_EVERY: self._every_radio,
         MODE_AT: self._at_radio}.get(mode, self._extract_radio).setChecked(True)

    def set_pages_text(self, text: str):
        self._pages_edit.setText(text)

    def set_cuts_text(self, text: str):
        self._cuts_edit.setText(text)

    def set_every(self, n: int):
        self._every_spin.setValue(int(n))

    def set_out_dir(self, path: str):
        self._folder_edit.setText(path)

    def out_dir(self) -> str:
        return self._folder_edit.text().strip() or self._default_dir()

    def groups(self) -> list[tuple]:
        """The page groups the current settings ask for, one tuple per file."""
        count = self._pdf.page_count()
        mode = self.mode()
        if mode == MODE_EVERY:
            return every_n_groups(count, self._every_spin.value())
        if mode == MODE_AT:
            cuts = parse_page_spec(self._cuts_edit.text(), count)
            return groups_at_cuts(count, cuts)
        pages = parse_page_spec(self._pages_edit.text(), count)
        return [tuple(pages)] if pages else []

    def plans(self) -> list:
        """The exact files the current settings would write. Never writes."""
        groups = self.groups()
        if not groups:
            return []
        if self.mode() == MODE_EXTRACT:
            # One file, and its name says which pages are in it rather than
            # calling a single extract "part 1 of 1".
            path = self._pdf.suggest_extract_path(groups[0], self.out_dir())
            return [SplitPlan(path, tuple(groups[0]),
                              page_range_label(groups[0]),
                              os.path.exists(path))]
        return self._pdf.plan_split(groups, out_dir=self.out_dir())

    def refresh(self):
        """Redraw the preview from the current settings."""
        plans = self.plans()
        self._preview.clear()
        for plan in plans:
            row = f"{os.path.basename(plan.path)}      {plan.label}"
            if plan.exists:
                row += "   (a file with this name is already there)"
            self._preview.addItem(row)
        if not plans:
            self._summary.setText("Nothing to write yet. Pick some pages.")
        else:
            clashes = sum(1 for p in plans if p.exists)
            files = f"{len(plans)} file" + ("" if len(plans) == 1 else "s")
            pages = sum(p.page_count for p in plans)
            line = f"{files}, {pages} page" + ("" if pages == 1 else "s") + " in total."
            if clashes:
                line += f" {clashes} would replace a file that is already there."
            self._summary.setText(line)
        self._go.setEnabled(bool(plans))
        self._go.setText("Extract" if self.mode() == MODE_EXTRACT else "Split")

    # ------------------------------------------------------------------
    # Doing it
    # ------------------------------------------------------------------

    def run(self, confirm_overwrite=None):
        """Write the planned files and return the SplitReport, or None.

        `confirm_overwrite` is asked only when a planned file is already on
        disk, and it is a parameter so a test can answer it without a modal.
        It takes the list of colliding paths and answers True to go ahead.
        None means "never overwrite", which leaves the collisions in the
        report's `skipped` list rather than replacing anything.
        """
        plans = self.plans()
        if not plans:
            return None
        clashes = [p.path for p in plans if os.path.exists(p.path)]
        overwrite = False
        if clashes:
            overwrite = bool(confirm_overwrite(clashes)) if confirm_overwrite else False
        report = self._pdf.run_split(plans, overwrite=overwrite)
        self._report = report
        return report

    def report(self):
        """The SplitReport from the last run, for the caller's status line."""
        return self._report

    def _on_go(self):
        report = self.run(confirm_overwrite=self._ask_overwrite)
        if report is None:
            return
        if report.failed:
            QMessageBox.critical(
                self, "Split Error",
                "These files could not be written:\n\n"
                + "\n".join(f"{os.path.basename(path)}: {why}"
                            for path, why in report.failed))
            if not report.written:
                return
        self.accept()

    def _ask_overwrite(self, paths) -> bool:
        listed = "\n".join(os.path.basename(p) for p in paths[:10])
        if len(paths) > 10:
            listed += f"\nand {len(paths) - 10} more"
        answer = QMessageBox.question(
            self, "Replace existing files?",
            f"{len(paths)} of the files this would write already exist:\n\n"
            f"{listed}\n\nReplace them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def summary_line(self) -> str:
        """One line for the window's status bar after the dialog closes."""
        report = self._report
        if report is None:
            return ""
        parts = []
        if report.written:
            parts.append(f"Wrote {len(report.written)} file"
                         + ("" if len(report.written) == 1 else "s") + ".")
        if report.skipped:
            parts.append(f"{len(report.skipped)} left alone (already there).")
        if report.failed:
            parts.append(f"{len(report.failed)} could not be written.")
        return " ".join(parts)

"""The status-bar page box: type a page number, press Enter, land on it.

The page position was already in the status bar, as text in the message
("page 12 of 887"). This makes that number the one you can type into rather
than adding a second place to look for it. It sits as a permanent widget next
to the Fit button, at the right-hand end of the bar.

A line edit, not a spin box: nobody hunts for a page in an 887-page manual by
clicking an up arrow, the arrows are noise at status-bar height, and a spin box
commits on every keystroke, so typing "88" on the way to "887" would jump twice
before you finished. Here the jump happens when you press Enter, once.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QWidget

# Wide enough for four digits, which covers anything anyone opens here.
_BOX_W = 48


class PageJump(QWidget):
    """Current page, editable, with the total beside it."""

    # Requested page, ZERO-based, to match everything else that moves pages.
    page_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._total = 0
        self._current = 0

        self._edit = QLineEdit()
        self._edit.setFixedWidth(_BOX_W)
        self._edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._edit.setToolTip("Go to page  (Ctrl+G)")
        # Blocks letters and the minus sign at the keystroke, so "abc" and "-4"
        # never reach the box. The range is re-set whenever a document loads.
        self._validator = QIntValidator(1, 1, self)
        self._edit.setValidator(self._validator)
        self._edit.returnPressed.connect(self._commit)
        self._edit.editingFinished.connect(self._on_editing_finished)

        self._total_label = QLabel()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(4)
        layout.addWidget(self._edit)
        layout.addWidget(self._total_label)

        self.set_total(0)

    # ------------------------------------------------------------------
    # State from the host
    # ------------------------------------------------------------------

    def set_total(self, total: int):
        """Set the page count. 0 (no document open) empties and disables the box."""
        self._total = max(0, int(total))
        has_pages = self._total > 0
        self._validator.setRange(1, max(1, self._total))
        self._total_label.setText(f"of {self._total}" if has_pages else "")
        self._edit.setEnabled(has_pages)
        self.setVisible(has_pages)
        if not has_pages:
            self._current = 0
            self._edit.clear()
        else:
            self._show_current()

    def set_current_page(self, page_num: int):
        """Mirror the page the viewer is on (zero-based).

        Skipped while the box has focus: the user is typing a number and a scroll
        underneath them must not overwrite it mid-keystroke. Whatever they leave
        behind is reconciled on focus-out.
        """
        self._current = max(0, int(page_num))
        if not self._edit.hasFocus():
            self._show_current()

    def current_text(self) -> str:
        """What the box is showing. For tests and for the host's own checks."""
        return self._edit.text()

    def total_text(self) -> str:
        return self._total_label.text()

    # ------------------------------------------------------------------
    # Focus (Ctrl+G)
    # ------------------------------------------------------------------

    def focus_box(self):
        """Put the cursor in the box with the number selected, so typing replaces it."""
        if self._total <= 0:
            return
        self._edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._edit.selectAll()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _show_current(self):
        self._edit.setText(str(self._current + 1) if self._total else "")

    def _on_editing_finished(self):
        """Focus left the box. Put back the page actually being shown.

        Without this, a number typed and then abandoned (clicked away from, or
        left over from a jump that got clamped) would sit there disagreeing with
        the page on screen.
        """
        self._show_current()

    def _commit(self):
        """Enter pressed. Jump, clamping anything out of range into it.

        Bad input never does nothing silently: an empty or unparseable box snaps
        back to the current page so you can see it was rejected, and a number
        past either end clamps to the first or last page and goes there, with the
        box showing the number it settled on.
        """
        if self._total <= 0:
            return
        text = self._edit.text().strip()
        try:
            wanted = int(text)
        except ValueError:
            # Empty, or something the validator let through as an intermediate
            # state (a lone "-", say). Nothing to act on.
            self._show_current()
            return
        page = min(max(wanted, 1), self._total) - 1
        self._current = page
        self._show_current()
        self.page_requested.emit(page)

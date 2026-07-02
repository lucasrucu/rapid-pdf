"""Find-in-document bar (Ctrl+F): proves an OCRed document is actually
searchable without leaving the app.

Dumb widget: it owns the line edit / next / prev / counter UI and emits
signals; MainWindow runs the actual search against PDFDocument.search_text
and drives the canvas highlights.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLineEdit, QToolButton, QLabel,
)


class SearchBar(QWidget):
    search_changed = Signal(str)   # text edited → (re)run the search
    next_requested = Signal()
    prev_requested = Signal()
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Find in document…")
        self._edit.setClearButtonEnabled(True)
        self._edit.setMaximumWidth(320)
        self._edit.textChanged.connect(self.search_changed)
        self._edit.returnPressed.connect(self.next_requested)
        layout.addWidget(self._edit)

        self._prev = QToolButton()
        self._prev.setText("‹")   # single left angle
        self._prev.setToolTip("Previous match (Shift+Enter)")
        self._prev.clicked.connect(self.prev_requested)
        layout.addWidget(self._prev)

        self._next = QToolButton()
        self._next.setText("›")
        self._next.setToolTip("Next match (Enter)")
        self._next.clicked.connect(self.next_requested)
        layout.addWidget(self._next)

        self._count = QLabel("")
        self._count.setMinimumWidth(110)
        layout.addWidget(self._count)

        layout.addStretch()

        close = QToolButton()
        close.setText("✕")
        close.setToolTip("Close search (Esc)")
        close.clicked.connect(self._on_close)
        layout.addWidget(close)

        self.hide()

    # ------------------------------------------------------------------

    def open_and_focus(self):
        self.show()
        self._edit.setFocus()
        self._edit.selectAll()

    def set_count_text(self, text: str):
        self._count.setText(text)

    def term(self) -> str:
        return self._edit.text()

    def _on_close(self):
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._on_close()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.prev_requested.emit()
            return
        super().keyPressEvent(event)

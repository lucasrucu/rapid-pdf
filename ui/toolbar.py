from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider,
    QLabel, QColorDialog, QFrame, QSpinBox,
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QColor


PRESETS = [
    QColor(255, 255, 0),    # Yellow
    QColor(0, 230, 0),      # Green
    QColor(0, 200, 255),    # Cyan
    QColor(255, 130, 0),    # Orange
    QColor(255, 80, 80),    # Red
    QColor(180, 90, 255),   # Purple
    QColor(255, 255, 255),  # White
    QColor(0, 0, 0),        # Black
]

_STYLE = """
QWidget#ToolBar {
    background-color: #1e1e1e;
    border-left: 1px solid #333;
}
QPushButton {
    background-color: #2d2d2d;
    border: 1px solid #444;
    border-radius: 3px;
    color: #d4d4d4;
    padding: 3px 4px;
    text-align: left;
}
QPushButton:hover {
    background-color: #3a3a3a;
    border-color: #666;
    color: #fff;
}
QPushButton:checked {
    background-color: #0078d4;
    border-color: #005fa8;
    color: #fff;
    font-weight: bold;
}
QPushButton:pressed {
    background-color: #005a9e;
}
QSlider::groove:horizontal {
    height: 4px;
    background: #444;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -5px 0;
    background: #0078d4;
    border-radius: 7px;
    border: 1px solid #005fa8;
}
QSlider::sub-page:horizontal {
    background: #0078d4;
    border-radius: 2px;
}
QSpinBox {
    background-color: #2d2d2d;
    border: 1px solid #444;
    border-radius: 3px;
    color: #d4d4d4;
    padding: 2px 4px;
}
QSpinBox:focus {
    border-color: #0078d4;
}
QLabel {
    color: #aaa;
    background: transparent;
}
QLabel#section {
    color: #888;
    font-size: 9px;
    font-weight: bold;
    letter-spacing: 1px;
    text-transform: uppercase;
}
QFrame[frameShape="4"] {
    color: #333;
}
"""


def _luminance(c: QColor) -> float:
    return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()


class ToolBar(QWidget):
    tool_changed = Signal(str)
    color_changed = Signal(QColor)
    opacity_changed = Signal(float)
    fill_toggled = Signal(bool)
    line_width_changed = Signal(float)
    font_size_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ToolBar")
        self._color = PRESETS[0]
        self._opacity = 0.5
        self._tool_btns: dict[str, QPushButton] = {}
        self._setup_ui()
        self.setFixedWidth(128)
        self.setStyleSheet(_STYLE)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 10, 6, 10)
        layout.setSpacing(5)

        # --- Tools ---
        layout.addWidget(self._section_label("TOOLS"))
        for tid, label in [
            ("select",    "Select  V"),
            ("highlight", "Highlight  H"),
            ("rect",      "Rectangle  R"),
            ("line",      "Line  L"),
            ("text",      "Text  T"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda _, t=tid: self._on_tool(t))
            self._tool_btns[tid] = btn
            layout.addWidget(btn)
        self._tool_btns["select"].setChecked(True)

        # Fill toggle
        self._fill_btn = QPushButton("Fill Shape")
        self._fill_btn.setCheckable(True)
        self._fill_btn.setFixedHeight(26)
        self._fill_btn.setToolTip("Fill rectangle with current color (40% opacity)")
        self._fill_btn.toggled.connect(self.fill_toggled)
        layout.addWidget(self._fill_btn)

        layout.addWidget(self._divider())

        # --- Stroke width + font size ---
        layout.addWidget(self._section_label("SIZE"))

        width_row = QWidget()
        width_layout = QHBoxLayout(width_row)
        width_layout.setContentsMargins(0, 0, 0, 0)
        width_layout.setSpacing(4)
        width_layout.addWidget(QLabel("Width"))
        self._width_spin = QSpinBox()
        self._width_spin.setRange(1, 20)
        self._width_spin.setValue(2)
        self._width_spin.setSuffix(" px")
        self._width_spin.setFixedHeight(24)
        self._width_spin.setToolTip("Stroke width for rectangles and lines")
        self._width_spin.valueChanged.connect(lambda v: self.line_width_changed.emit(float(v)))
        width_layout.addWidget(self._width_spin)
        layout.addWidget(width_row)

        font_row = QWidget()
        font_layout = QHBoxLayout(font_row)
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_layout.setSpacing(4)
        font_layout.addWidget(QLabel("Font"))
        self._font_spin = QSpinBox()
        self._font_spin.setRange(4, 144)
        self._font_spin.setValue(12)
        self._font_spin.setSuffix(" pt")
        self._font_spin.setFixedHeight(24)
        self._font_spin.setToolTip("Font size for text labels")
        self._font_spin.valueChanged.connect(lambda v: self.font_size_changed.emit(v))
        font_layout.addWidget(self._font_spin)
        layout.addWidget(font_row)

        layout.addWidget(self._divider())

        # --- Color ---
        layout.addWidget(self._section_label("COLOR"))

        for row_colors in [PRESETS[:4], PRESETS[4:]]:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(3)
            for c in row_colors:
                btn = QPushButton()
                btn.setFixedSize(24, 24)
                btn.setToolTip(c.name())
                btn.setStyleSheet(self._swatch_style(c))
                btn.clicked.connect(lambda _, col=c: self._on_color(col))
                row_layout.addWidget(btn)
            layout.addWidget(row)

        # Custom color button — shows current color as its background
        self._custom_btn = QPushButton("Custom…")
        self._custom_btn.setFixedHeight(28)
        self._custom_btn.setToolTip("Open color picker")
        self._custom_btn.clicked.connect(self._open_color_dialog)
        layout.addWidget(self._custom_btn)
        self._update_custom_btn()

        layout.addWidget(self._divider())

        # --- Opacity ---
        layout.addWidget(self._section_label("OPACITY"))
        self._opacity_label = QLabel(f"{int(self._opacity * 100)}%")
        self._opacity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._opacity_label)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(5, 100)
        self._opacity_slider.setValue(int(self._opacity * 100))
        self._opacity_slider.setToolTip("Annotation opacity")
        self._opacity_slider.valueChanged.connect(self._on_opacity)
        layout.addWidget(self._opacity_slider)

        layout.addStretch()

        # --- Keyboard hints ---
        layout.addWidget(self._divider())
        hints = QLabel(
            "V/H/R/L/T — tools\n"
            "Del — delete\n"
            "Dbl-click — text\n"
            "Ctrl+drag — copy\n"
            "Ctrl+scroll — zoom\n"
            "Shift+draw — constrain"
        )
        hints.setWordWrap(True)
        hints.setStyleSheet("font-size: 9px; color: #555; line-height: 1.4;")
        layout.addWidget(hints)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("section")
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        return lbl

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        return line

    @staticmethod
    def _swatch_style(c: QColor) -> str:
        return (
            f"QPushButton {{ background-color: {c.name()}; border: 1px solid #555; border-radius: 3px; }}"
            f"QPushButton:hover {{ border: 2px solid #fff; }}"
            f"QPushButton:pressed {{ border: 2px solid #0078d4; }}"
        )

    def _update_custom_btn(self):
        text_color = "#000" if _luminance(self._color) > 128 else "#fff"
        self._custom_btn.setStyleSheet(
            f"QPushButton {{ background-color: {self._color.name()}; color: {text_color}; "
            f"border: 1px solid #666; border-radius: 3px; padding: 3px 4px; }}"
            f"QPushButton:hover {{ border: 1px solid #fff; }}"
        )

    def _on_tool(self, tool: str):
        for tid, btn in self._tool_btns.items():
            btn.setChecked(tid == tool)
        self.tool_changed.emit(tool)

    def _on_color(self, color: QColor):
        self._color = color
        self._update_custom_btn()
        self.color_changed.emit(color)

    def _open_color_dialog(self):
        color = QColorDialog.getColor(
            self._color, self, "Pick Color",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if color.isValid():
            self._on_color(color)

    def _on_opacity(self, value: int):
        self._opacity = value / 100.0
        self._opacity_label.setText(f"{value}%")
        self.opacity_changed.emit(self._opacity)

    def trigger_tool(self, tool: str):
        if tool in self._tool_btns:
            self._on_tool(tool)

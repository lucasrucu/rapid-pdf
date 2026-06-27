import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

from ui.main_window import MainWindow
from ui.theme import apply_theme
from core.resources import app_icon_path


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Rapid PDF")
    app.setOrganizationName("Lucas")

    icon_path = app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))

    # Qori theme (Sovereign light by default, dark toggle available). Sets the
    # Fusion style + palette + global QSS; returns the manager for the window.
    theme = apply_theme(app)

    window = MainWindow(theme=theme)
    if len(sys.argv) > 1:
        window._doc.open(sys.argv[1])
        window._canvas.set_document(window._doc)
        window._page_panel.set_document(window._doc)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

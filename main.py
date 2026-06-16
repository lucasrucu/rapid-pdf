import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Rapid PDF")
    app.setOrganizationName("Lucas")
    app.setStyle("Fusion")

    # Open file passed as CLI argument
    window = MainWindow()
    if len(sys.argv) > 1:
        window._doc.open(sys.argv[1])
        window._canvas.set_document(window._doc)
        window._page_panel.set_document(window._doc)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

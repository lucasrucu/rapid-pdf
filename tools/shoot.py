"""Render the real MainWindow with a sample PDF and grab PNGs of the Editor and
Organizer tabs in each theme.
Usage: .venv\\Scripts\\python.exe tools\\shoot.py <pdf> <out_dir>
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

pdf = sys.argv[1]
out_dir = sys.argv[2]
os.makedirs(out_dir, exist_ok=True)

app = QApplication(["shoot"])
app.setApplicationName("Rapid PDF")
app.setOrganizationName("Lucas")

from ui.theme import apply_theme, ThemeMode
from ui.main_window import MainWindow

theme = apply_theme(app)
theme.set_mode(ThemeMode.LIGHT)  # start from a known light state for the shots

win = MainWindow(theme=theme)
win._doc.open(pdf)
win._canvas.set_document(win._doc)
win._page_panel.set_document(win._doc)
win._toolbar.trigger_tool("rect")  # show an active tool + appearance controls
win.resize(1200, 800)
win.show()


def grab(tag):
    app.processEvents()
    win.grab().save(os.path.join(out_dir, f"rapid-pdf-{tag}.png"), "PNG")
    print("wrote", tag)


# Queue of (action, delay) steps. Selecting page 2 in the panel shows the
# selection highlight; switching to the Organizer tab shows that grid.
def run():
    grab("light-editor")
    win._page_panel.set_current_page(1)  # select page 2 to show selection backing
    app.processEvents(); grab("light-editor-sel")
    win._tabs.setCurrentIndex(1)
    QTimer.singleShot(500, after_org_light)


def after_org_light():
    win._organizer._list.setCurrentRow(1)  # select a page in the grid
    app.processEvents(); grab("light-organizer")
    theme.set_mode(ThemeMode.DARK)
    QTimer.singleShot(400, dark_org)


def dark_org():
    grab("dark-organizer")
    win._tabs.setCurrentIndex(0)
    QTimer.singleShot(400, dark_editor)


def dark_editor():
    win._page_panel.set_current_page(1)
    app.processEvents(); grab("dark-editor-sel")
    print("done")
    os._exit(0)


QTimer.singleShot(700, run)
sys.exit(app.exec())

import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

from ui.main_window import MainWindow
from ui.theme import apply_theme
from core.resources import app_icon_path
from core.settings import settings
from core.single_instance import InstanceServer, forward_to_primary, parse_cli


def main():
    files, combine = parse_cli(sys.argv)

    # The QApplication must exist BEFORE forwarding: QLocalSocket's async
    # pipe writer only ships bytes while events are processed (see
    # core/single_instance.forward_to_primary).
    app = QApplication(sys.argv)
    app.setApplicationName("Rapid PDF")
    app.setOrganizationName("Lucas")

    # Single instance: if Rapid PDF is already running, hand it this launch
    # and leave. Explorer context-menu verbs fire once per selected file, so
    # a multi-select "Combine with Rapid PDF" becomes several rapid launches;
    # the running instance aggregates them (see core/single_instance.py).
    if forward_to_primary(files, combine):
        sys.exit(0)

    # Settings live in %LOCALAPPDATA%\Rapid PDF\settings.json (see
    # core/settings.py). Built here, after the org/app names are set, because
    # those decide the path. Writes are debounced, so a last flush on the way
    # out catches a change made in the final quarter-second.
    app.aboutToQuit.connect(settings().flush)

    icon_path = app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))

    # Qori theme (Sovereign light by default, dark toggle available). Sets the
    # Fusion style + palette + global QSS; returns the manager for the window.
    theme = apply_theme(app)

    window = MainWindow(theme=theme)

    server = InstanceServer(app)
    server.batch_ready.connect(window.handle_cli_files)
    if not server.listen():
        # No pipe (rare): the app still works, just without launch forwarding.
        print("single-instance listen failed; launch forwarding disabled")
    if files or combine:
        # The primary's own command line joins the same aggregation window as
        # any forwarded launches that arrive right behind it.
        server.add_launch(files, combine)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

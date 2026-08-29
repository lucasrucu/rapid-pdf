import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

from ui.theme import apply_theme
from ui.window_registry import WindowRegistry
from core.resources import app_icon_path
from core.settings import settings
from core.single_instance import (
    InstanceServer,
    claim_primary,
    forward_to_primary,
    parse_cli,
)


def main():
    files, combine = parse_cli(sys.argv)

    # The QApplication must exist BEFORE forwarding: QLocalSocket's async
    # pipe writer only ships bytes while events are processed (see
    # core/single_instance.forward_to_primary).
    app = QApplication(sys.argv)
    app.setApplicationName("Rapid PDF")
    app.setOrganizationName("Lucas")

    # App lifetime moves to the WindowRegistry (see ui/window_registry.py).
    # Qt's own rule counts every top-level widget, which is the wrong count in
    # both directions once there is more than one window: a Preferences dialog
    # or a message box left over from a closing window keeps the app alive with
    # nothing to look at, and a window that exists but has not been shown does
    # not count at all. The registry quits when the last WINDOW leaves it.
    app.setQuitOnLastWindowClosed(False)

    # Single instance. Explorer context-menu verbs fire once per selected
    # file, so a multi-select "Combine with Rapid PDF" becomes several rapid
    # launches; exactly one of them must become the primary and aggregate the
    # rest (see core/single_instance.py).
    #
    # The election has to happen HERE, before anything slow, and it cannot be
    # QLocalServer.listen(): that is not exclusive on Windows, so several
    # processes used to win it at once and each opened its own window.
    if not claim_primary():
        if forward_to_primary(files, combine):
            sys.exit(0)
        # Either the previous primary died mid-handshake (we hold the claim
        # now) or it never answered. Carry on and open a window.

    # Listen BEFORE building the window: the gap between claiming the slot and
    # accepting connections is exactly the window in which siblings pile up.
    server = InstanceServer(app)
    if not server.listen():
        # No pipe (rare): the app still works, just without launch forwarding.
        print("single-instance listen failed; launch forwarding disabled")
    if files or combine:
        # The primary's own command line joins the same aggregation window as
        # any forwarded launches that arrive right behind it.
        server.add_launch(files, combine)

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

    registry = WindowRegistry.instance()
    registry.set_theme(theme)
    window = registry.create_window(theme=theme, show=False)

    # Not window.handle_cli_files. A launch is aimed at the APPLICATION, and
    # with several windows open the question of which one it lands in is the
    # registry's: a file already open anywhere raises its own tab, and anything
    # else goes to the window last touched. See WindowRegistry.route_open.
    server.batch_ready.connect(registry.route_open)
    server.arm()      # nothing is emitted until the window is wired up

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

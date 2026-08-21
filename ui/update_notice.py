"""The update UI: one strip across the top of the window, and nothing else.

WHAT IT IS ALLOWED TO DO. Appear, say what is on offer, and be dismissed. That
is the whole brief, and it is why this is a strip rather than a dialog. Rapid
PDF is opened to do one thing to one PDF, often from an Explorer context menu,
and a modal box in front of that is an interruption to somebody who did not
ask for one. A strip is ignorable, and being ignorable is the feature.

THE THREE RULES IT KEEPS:

  1. NOTHING BLOCKS STARTUP. The check runs on a background thread, after the
     window is up. client.check() cannot raise, so the worst case offline is
     that this never appears and nobody learns anything happened.
  2. NOTHING IS LOST. Applying the update closes the app, so the window is
     asked first (the same unsaved-changes prompt as Quit), and a cancelled
     prompt leaves the staged update sitting there with the button still
     offering it.
  3. LATER MEANS LATER. Dismissing hides it for this session. The next launch
     checks again, which is the right amount of nagging for a free upgrade.

FROM SOURCE THERE IS NO EXE TO SWAP, so the button says "View release" and
opens the page in a browser instead. Honest, and it means the check is still
exercised in development rather than being dead code until the next build.

WHERE THE WORK HAPPENS: not here. Nothing in this file knows how to compare a
version, fetch an asset or replace an exe. It calls core/update/, which is
Qt-free and tested on its own. This is two threads and three buttons.

The look follows ui/theme.py the same way every other widget does: an
apply_palette() the main window calls on construction and on every light/dark
toggle. Accent on the left edge (this is the thing you are being asked to act
on), surface behind, nothing else colored.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
)

from core.update import client, swap
from core.update.release import human_size


class _CheckWorker(QObject):
    """Runs client.check() off the GUI thread and hands back the answer.

    No try/except. client.check() is contractually incapable of raising (see
    its docstring, and tests/test_update.py), so a guard here would be guarding
    nothing and would hide it if that ever stopped being true.
    """

    done = Signal(object)

    def __init__(self, current_version: str | None = None) -> None:
        super().__init__()
        self._current = current_version

    def run(self) -> None:
        self.done.emit(client.check(self._current))


class _StageWorker(QObject):
    """Downloads, verifies and unpacks, reporting bytes as they land.

    Progress is emitted from this thread and connected across, so the bar is
    repainted by the GUI thread as usual. The asset is 67 MB, which is why
    there is a bar at all rather than a spinner.
    """

    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, info, install_dir) -> None:
        super().__init__()
        self._info = info
        self._install = install_dir
        self._cancelled = False

    def cancel(self) -> None:
        """Stop at the next chunk. Set from the GUI thread when the app closes.

        A download of 67 MB cannot be interrupted by quitting a thread's event
        loop, because it is in a read loop and not in an event loop. Raising
        out of the progress callback is what stops it, and it stops it through
        client.stage()'s own cleanup, so the half-written staging folder goes
        with it rather than being left beside the install.
        """
        self._cancelled = True

    def _tick(self, done: int, total: int, phase: str) -> None:
        if self._cancelled:
            raise client.UpdateError(
                "The update was stopped because Rapid PDF is closing. "
                "Nothing has been changed.")
        self.progress.emit(int(done), int(total), str(phase))

    def run(self) -> None:
        try:
            staged = client.stage(self._info, self._install, progress=self._tick)
        except client.UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - a bug here must not hang the UI
            self.failed.emit(
                f"The update stopped on an error it did not handle: "
                f"{type(exc).__name__}: {exc}. Nothing has been changed."
            )
        else:
            self.done.emit(staged)


class UpdateNotice(QFrame):
    """A hidden strip that shows itself when GitHub has a newer release.

    Built and added to the layout at startup, hidden. Nothing in the window
    moves unless an update is actually offered.
    """

    #: A verified update is staged and the app has to close for the swap. The
    #: main window decides when that is safe (unsaved changes) and calls
    #: launch_swap() when it is.
    staged_ready = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("UpdateNotice")
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._info = None
        self._staged = None
        self._manual = False          # this check came from the Help menu
        self._thread: QThread | None = None
        self._worker = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 7, 8, 7)
        layout.setSpacing(10)

        self._label = QLabel("")
        self._label.setObjectName("UpdateNoticeText")
        layout.addWidget(self._label, stretch=1)

        self._bar = QProgressBar()
        self._bar.setFixedWidth(180)
        self._bar.setTextVisible(False)
        self._bar.hide()
        layout.addWidget(self._bar)

        self._action = QPushButton("Update now")
        self._action.clicked.connect(self._on_action)
        layout.addWidget(self._action)

        self._later = QPushButton("Later")
        self._later.clicked.connect(self._dismiss)
        layout.addWidget(self._later)

        self.hide()

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def apply_palette(self, palette) -> None:
        """Re-tint the strip. Called on construction and on every theme toggle.

        The accent is on the left edge only: this is the one thing on screen
        asking to be acted on, which is exactly what the accent means
        everywhere else in the app (see ui/theme.py).
        """
        self.setStyleSheet(f"""
QFrame#UpdateNotice {{
    background-color: {palette.surface};
    border: none;
    border-left: 3px solid {palette.accent};
    border-bottom: 1px solid {palette.border};
}}
QLabel#UpdateNoticeText {{
    color: {palette.text};
    background: transparent;
}}
""")

    # ------------------------------------------------------------------
    # Checking
    # ------------------------------------------------------------------

    def start_check(self, manual: bool = False) -> None:
        """Ask GitHub, on a background thread. Never blocks, never raises.

        `manual` is the Help menu asking, which is the one case where "you are
        up to date" is worth saying out loud. The startup check says nothing
        when there is nothing to say.
        """
        if self._thread is not None:
            return                      # a check or a download is already running
        if self._staged is not None:
            return                      # already fetched and waiting to be applied
        self._manual = manual
        worker = _CheckWorker()
        worker.done.connect(self._on_check_done)
        self._run(worker)

    def _on_check_done(self, info) -> None:
        self._finish_thread()
        if info is None:
            if self._manual:
                QMessageBox.information(
                    self.window(), "Rapid PDF",
                    "You are on the latest version.\n\n"
                    "(If you are offline or GitHub cannot be reached, this "
                    "says the same thing. Nothing has been changed either "
                    "way.)")
            return
        self._info = info
        self._label.setText(info.headline())
        self._action.setText("Update now" if client.install_dir() is not None
                             else "View release")
        self._action.setEnabled(True)
        self._later.setEnabled(True)
        self._bar.hide()
        self.show()

    # ------------------------------------------------------------------
    # Downloading and applying
    # ------------------------------------------------------------------

    def _on_action(self) -> None:
        if self._staged is not None:
            self.staged_ready.emit(self._staged)
            return
        if self._info is None:
            return

        target = client.install_dir()
        if target is None:
            # Running from source: there is no exe to replace, so the honest
            # thing is the release page rather than a swap that cannot work.
            url = self._info.release.html_url
            if url:
                QDesktopServices.openUrl(QUrl(url))
            self._dismiss()
            return

        self._action.setEnabled(False)
        self._later.setEnabled(False)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.show()
        self._label.setText(f"Downloading Rapid PDF {self._info.version}...")

        worker = _StageWorker(self._info, target)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_stage_done)
        worker.failed.connect(self._on_stage_failed)
        self._run(worker)

    def _on_progress(self, done: int, total: int, phase: str) -> None:
        if phase == "unpacking":
            # No byte count to report through the unpack, and a bar frozen at
            # 100% reads as a hang. A busy bar says it is still working.
            self._bar.setRange(0, 0)
            self._label.setText("Checking and unpacking the download...")
            return
        if total > 0:
            self._bar.setValue(int(done * 100 / total))
        self._label.setText(
            f"Downloading Rapid PDF {self._info.version}...  "
            f"{human_size(done)} of {human_size(total)}")

    def _on_stage_done(self, staged) -> None:
        self._finish_thread()
        self._staged = staged
        self._bar.hide()
        self._label.setText(
            f"Rapid PDF {staged.info.version} is ready. Rapid PDF will close "
            "and reopen on the new version.")
        self._action.setText("Restart now")
        self._action.setEnabled(True)
        self._later.setEnabled(True)
        self.staged_ready.emit(staged)

    def _on_stage_failed(self, message: str) -> None:
        self._finish_thread()
        self._bar.hide()
        self._bar.setRange(0, 100)
        self._action.setText("Try again")
        self._action.setEnabled(True)
        self._later.setEnabled(True)
        self._label.setText(f"The update did not complete. "
                            f"You are still on {self._info.running}.")
        QMessageBox.warning(self.window(), "Update", message
                            or "The update stopped. Nothing has been changed.")

    def launch_swap(self, staged) -> bool:
        """Start the helper that does the swap. True when it is running.

        The caller must exit the app straight after a True: the helper is
        sitting in a wait loop watching for this process to go, and nothing on
        disk has changed until it does.
        """
        try:
            swap.apply(staged)
        except swap.SwapNotStarted as exc:
            QMessageBox.warning(self.window(), "Update", str(exc))
            self._action.setEnabled(True)
            self._later.setEnabled(True)
            return False
        return True

    def apply_cancelled(self) -> None:
        """The window would not close, so the offer stays on the strip."""
        self._action.setEnabled(True)
        self._later.setEnabled(True)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _dismiss(self) -> None:
        """Later. Hidden for this session; the next launch asks again.

        Anything already downloaded is thrown away rather than left in a
        folder beside the install for nobody to find. It is a 67 MB folder and
        the next launch would re-check anyway.
        """
        if self._staged is not None:
            self._staged.discard()
            self._staged = None
        self.hide()

    def _run(self, worker) -> None:
        """Put a worker on its own thread and start it.

        The thread and the worker are both held on self, because a QThread
        that goes out of scope while running takes the app down with it.
        """
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        self._thread, self._worker = thread, worker
        thread.start()

    def _finish_thread(self, wait_ms: int = 5000) -> None:
        """Stop the thread the signal just arrived from.

        Called from the slots, which run on the GUI thread, so the worker has
        already returned by the time this quits its event loop.
        """
        thread, self._thread, self._worker = self._thread, None, None
        if thread is not None:
            thread.quit()
            thread.wait(wait_ms)
            thread.deleteLater()

    def shutdown(self) -> None:
        """Stop anything still running, for the window's closeEvent.

        A worker thread outliving the widget it emits into is a crash on exit,
        so this waits. What it is waiting for is bounded either way: a check
        is one request with a ten second socket timeout, and a download stops
        at the next chunk once cancel() is set.

        The signals are disconnected first. A worker that finishes DURING the
        wait would otherwise land _on_check_done or _on_stage_done on a window
        that is halfway through closing, and neither of those has any business
        running then.
        """
        worker, thread = self._worker, self._thread
        if worker is None and thread is None:
            return
        if worker is not None:
            if hasattr(worker, "cancel"):
                worker.cancel()
            try:
                worker.disconnect()
            except RuntimeError:
                pass          # nothing was connected, or it is already gone
        self._finish_thread(wait_ms=15000)

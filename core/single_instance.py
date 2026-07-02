"""Single-instance plumbing + Explorer launch aggregation.

Why this exists: an Explorer context-menu verb fires ONCE PER SELECTED FILE.
Right-clicking three PDFs and picking "Combine with Rapid PDF" launches three
processes, each with one path. So the first process becomes the primary and
owns a QLocalServer; every later launch connects as a client, forwards its
arguments as one JSON line, and exits immediately. The primary collects
everything that arrives within a short window (plus its own command line) and
hands the batch to the window in one call: several files together (or any
--combine launch) open the staged Combine dialog as whole-file cards, a lone
file just opens.

The server name is per-user so two Windows sessions can't cross wires.
"""

import getpass
import json
import os

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = f"rapid-pdf-instance-{getpass.getuser()}"

# How long to wait for more forwarded launches before acting. Explorer fires
# the per-file verbs within a few hundred ms of each other; each arrival
# restarts the timer, so the window only needs to cover the gap BETWEEN two
# launches, not the whole burst.
AGGREGATE_MS = 700

_CONNECT_TIMEOUT_MS = 1500


def forward_to_primary(files: list, combine: bool) -> bool:
    """Try to hand this launch to an already-running instance.

    Returns True when a primary accepted the payload (caller should exit),
    False when there is no primary (caller becomes it).

    Requires a QCoreApplication to exist: QLocalSocket's Windows pipe writer
    performs the write asynchronously, so the payload only actually leaves
    this process while events are being processed. Verified empirically:
    write + flush + close WITHOUT pumping delivers zero bytes to the server;
    pumping until bytesToWrite() hits 0 delivers reliably.
    """
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(_CONNECT_TIMEOUT_MS):
        return False
    payload = json.dumps({
        "files": [os.path.abspath(f) for f in files],
        "combine": bool(combine),
    }).encode("utf-8") + b"\n"
    sock.write(payload)
    app = QCoreApplication.instance()
    timer = QElapsedTimer()
    timer.start()
    while sock.bytesToWrite() > 0 and timer.elapsed() < _CONNECT_TIMEOUT_MS:
        if app is not None:
            app.processEvents()
        else:
            sock.waitForBytesWritten(50)
    delivered = sock.bytesToWrite() == 0
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(_CONNECT_TIMEOUT_MS)
    return delivered


class InstanceServer(QObject):
    """Primary-side listener: emits one aggregated batch per launch burst."""

    # (files, combine) after the aggregation window closes.
    batch_ready = Signal(list, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending_files: list[str] = []
        self._pending_combine = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(AGGREGATE_MS)
        self._timer.timeout.connect(self._flush)
        # A stale socket file/pipe survives a crashed primary; clear it or
        # listen() fails forever and every launch thinks a primary exists.
        QLocalServer.removeServer(SERVER_NAME)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)

    def listen(self) -> bool:
        return self._server.listen(SERVER_NAME)

    def add_launch(self, files: list, combine: bool):
        """Queue a launch (the primary's own argv, or a forwarded one) into
        the current aggregation window."""
        self._pending_files.extend(f for f in files if f)
        self._pending_combine = self._pending_combine or combine
        self._timer.start()   # restart: extend the window for stragglers

    def _on_connection(self):
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        sock.readyRead.connect(lambda s=sock: self._read(s))
        # The client typically writes one line and closes immediately. Drain
        # on every signal that can carry the data: readyRead may never fire if
        # the bytes were already buffered when this handler ran (drain now) or
        # if they arrive together with the EOF (drain on disconnected, BEFORE
        # the deferred delete).
        sock.disconnected.connect(lambda s=sock: (self._read(s), s.deleteLater()))
        if sock.bytesAvailable():
            self._read(sock)

    def _read(self, sock):
        while sock.canReadLine():
            line = bytes(sock.readLine()).decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.add_launch(list(msg.get("files", [])), bool(msg.get("combine")))

    def _flush(self):
        files = [f for f in self._pending_files if os.path.exists(f)]
        # De-duplicate while keeping order (the same file forwarded twice).
        seen = set()
        files = [f for f in files if not (f in seen or seen.add(f))]
        combine = self._pending_combine
        self._pending_files = []
        self._pending_combine = False
        if files:
            self.batch_ready.emit(files, combine)


def parse_cli(argv: list) -> tuple[list, bool]:
    """(pdf_paths, combine_flag) from a raw argv (argv[0] excluded)."""
    files = [a for a in argv[1:] if not a.startswith("-")]
    combine = "--combine" in argv[1:]
    return files, combine

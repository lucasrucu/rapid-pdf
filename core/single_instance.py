"""Single-instance plumbing + Explorer launch aggregation.

Why this exists: an Explorer context-menu verb fires ONCE PER SELECTED FILE.
Right-clicking three PDFs and picking "Combine with Rapid PDF" launches three
processes, each with one path. So one process becomes the primary and owns a
QLocalServer; every later launch connects as a client, forwards its arguments
as one JSON line, and exits immediately. The primary collects everything that
arrives within a short window (plus its own command line) and hands the batch
to the window in one call: several files together (or any --combine launch)
open the staged Combine dialog as whole-file cards, a lone file just opens.

Electing that primary is the delicate part, and it is NOT something
QLocalServer can do on Windows -- see claim_primary() below.

The server name is per-user so two Windows sessions can't cross wires.
"""

import getpass
import json
import os

from PySide6.QtCore import (
    QCoreApplication,
    QElapsedTimer,
    QObject,
    QSharedMemory,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = f"rapid-pdf-instance-{getpass.getuser()}"

# How long to wait for more forwarded launches before acting. Explorer fires
# the per-file verbs within a few hundred ms of each other; each arrival
# restarts the timer, so the window only needs to cover the gap BETWEEN two
# launches, not the whole burst.
AGGREGATE_MS = 700

# Once a second launch has landed we KNOW this is a multi-file burst, and the
# user is going to the Combine dialog either way. Widen the per-arrival window
# so a straggler that lost the CPU race to a dozen cold-starting siblings
# still makes it into the same batch. Measured: a 15-process burst spreads
# arrivals over ~1s on a busy laptop, which a flat 700 ms window would split.
BURST_AGGREGATE_MS = 1500

# Absolute ceiling from the first arrival, so a slow trickle of launches can
# never hold the batch open indefinitely.
MAX_AGGREGATE_MS = 10_000

# How long a non-primary launch keeps trying to reach the primary. The primary
# claims its slot BEFORE it builds its window, so there is a real gap between
# "a primary exists" and "the primary is listening"; a secondary that gave up
# during that gap would open a window of its own, which is exactly the bug
# this retry loop closes.
WAIT_FOR_PRIMARY_MS = 10_000

_CONNECT_TIMEOUT_MS = 1500
_RETRY_SLEEP_MS = 40

# Held for the life of the process once we win the election. Module-level on
# purpose: dropping the QSharedMemory would release the claim.
_primary_claim: QSharedMemory | None = None


def _claim_key() -> str:
    return f"{SERVER_NAME}-primary"


def claim_primary() -> bool:
    """Try to become THE primary instance. True means we won.

    Why this is not just QLocalServer.listen(): on Windows, Qt creates the
    named pipe WITHOUT FILE_FLAG_FIRST_PIPE_INSTANCE, so listen() is not
    exclusive. Two QLocalServers on the same name both return True, and
    Windows then spreads incoming clients across the pipe instances.

    Measured on Windows 11 with a 5-process "--combine" burst: three
    processes all got listen() == True and produced three separate windows,
    the batch split 1 / 1 / 3 between them. That is the "it opened all the
    PDFs in separate windows instead of combining" bug.

    A shared-memory segment IS exclusive: create() fails with AlreadyExists
    while any process holds it, and Windows destroys the mapping when the
    last handle closes, so a crashed primary leaves nothing stale behind.
    """
    global _primary_claim
    if _primary_claim is not None:
        return True
    seg = QSharedMemory(_claim_key())
    if seg.create(1):
        _primary_claim = seg      # keep alive: the claim dies with the object
        return True
    return False


def release_primary_claim():
    """Drop the claim (tests, and an orderly shutdown)."""
    global _primary_claim
    if _primary_claim is not None:
        _primary_claim.detach()
        _primary_claim = None


def forward_to_primary(files: list, combine: bool,
                       wait_ms: int = WAIT_FOR_PRIMARY_MS) -> bool:
    """Try to hand this launch to the primary instance.

    Returns True when a primary accepted the payload (caller should exit),
    False when the caller should carry on and open a window itself.

    Call this only after claim_primary() has returned False, i.e. somebody
    else owns the claim. The primary takes its claim before it builds its
    window, so the pipe may not exist yet; rather than give up (and become a
    second window), keep retrying until either the pipe answers or the claim
    becomes free, which means the primary died and we take over.

    Requires a QCoreApplication to exist: QLocalSocket's Windows pipe writer
    performs the write asynchronously, so the payload only actually leaves
    this process while events are being processed. Verified empirically:
    write + flush + close WITHOUT pumping delivers zero bytes to the server;
    pumping until bytesToWrite() hits 0 delivers reliably.
    """
    payload = json.dumps({
        "files": [os.path.abspath(f) for f in files],
        "combine": bool(combine),
    }).encode("utf-8") + b"\n"

    overall = QElapsedTimer()
    overall.start()
    while True:
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if sock.waitForConnected(_CONNECT_TIMEOUT_MS):
            return _write_payload(sock, payload)
        if overall.elapsed() >= wait_ms:
            return False
        # The primary vanished before it ever listened: the claim is free
        # again, so take it and become the primary ourselves.
        if claim_primary():
            return False
        QThread.msleep(_RETRY_SLEEP_MS)


def _write_payload(sock: QLocalSocket, payload: bytes) -> bool:
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
        self._launches = 0
        self._armed = False
        self._window = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(AGGREGATE_MS)
        self._timer.timeout.connect(self._flush)
        # A stale socket file/pipe survives a crashed primary; clear it or
        # listen() fails forever and every launch thinks a primary exists.
        # (On Windows this is a no-op -- see claim_primary for why listen()
        # cannot be trusted as the election.)
        QLocalServer.removeServer(SERVER_NAME)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)

    def listen(self) -> bool:
        return self._server.listen(SERVER_NAME)

    def arm(self):
        """Allow batches to be emitted.

        listen() now runs BEFORE the main window is built, so connections can
        land before anything is subscribed to batch_ready. Nothing is emitted
        until the caller has wired the window up and armed us; a window that
        expired in the meantime just re-opens.
        """
        self._armed = True
        if self._pending_files or self._pending_combine:
            self._timer.start(self._interval())

    def add_launch(self, files: list, combine: bool):
        """Queue a launch (the primary's own argv, or a forwarded one) into
        the current aggregation window."""
        if not self._pending_files and not self._pending_combine:
            self._window.start()
        self._pending_files.extend(f for f in files if f)
        self._pending_combine = self._pending_combine or combine
        self._launches += 1
        self._timer.start(self._interval())   # restart: wait for stragglers

    def _interval(self) -> int:
        """Per-arrival wait, clamped by the absolute ceiling."""
        base = BURST_AGGREGATE_MS if self._launches > 1 else AGGREGATE_MS
        if self._window.isValid():
            left = MAX_AGGREGATE_MS - self._window.elapsed()
            base = max(0, min(base, int(left)))
        return base

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
        if not self._armed:
            # Nothing is listening yet; hold the batch rather than lose it.
            self._timer.start(AGGREGATE_MS)
            return
        files = [f for f in self._pending_files if os.path.exists(f)]
        # De-duplicate while keeping order (the same file forwarded twice).
        seen = set()
        files = [f for f in files if not (f in seen or seen.add(f))]
        combine = self._pending_combine
        self._pending_files = []
        self._pending_combine = False
        self._launches = 0
        self._window.invalidate()
        if files:
            self.batch_ready.emit(files, combine)


def parse_cli(argv: list) -> tuple[list, bool]:
    """(pdf_paths, combine_flag) from a raw argv (argv[0] excluded)."""
    files = [a for a in argv[1:] if not a.startswith("-")]
    combine = "--combine" in argv[1:]
    return files, combine

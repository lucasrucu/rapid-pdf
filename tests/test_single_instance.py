"""The Explorer multi-select "Combine with Rapid PDF" bug, and its fix.

Reported: selecting several PDFs and picking Combine opened them in separate
windows instead of combining them.

Root cause, reproduced with a 5-process burst on Windows 11: Qt creates its
QLocalServer named pipe WITHOUT FILE_FLAG_FIRST_PIPE_INSTANCE, so listen() is
NOT exclusive. Several launches each got listen() == True, Windows spread the
forwarded clients across those pipe instances, and the batch split three ways
into three windows.

The election therefore needs a primitive that really is exclusive
(QSharedMemory), taken BEFORE the slow window build. These tests pin both the
broken assumption and the fix.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

import core.single_instance as si
from core.single_instance import InstanceServer, parse_cli


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def unique_name(monkeypatch):
    """Never touch the user's real running instance."""
    name = f"rapid-pdf-test-{os.getpid()}-{id(object())}"
    monkeypatch.setattr(si, "SERVER_NAME", name)
    monkeypatch.setattr(si, "_primary_claim", None)
    yield name
    si.release_primary_claim()


@pytest.fixture
def pdfs(tmp_path):
    import fitz
    paths = []
    for i in range(4):
        doc = fitz.open()
        doc.new_page()
        p = tmp_path / f"doc{i}.pdf"
        doc.save(str(p))
        doc.close()
        paths.append(str(p))
    return paths


def _pump(qapp, ms):
    """Spin the event loop for ms, so QTimer-driven aggregation actually runs."""
    t = QElapsedTimer()
    t.start()
    while t.elapsed() < ms:
        qapp.processEvents()


# ---------------------------------------------------------------------------
# parse_cli
# ---------------------------------------------------------------------------

def test_parse_cli_reads_combine_and_paths():
    files, combine = parse_cli(["rapid-pdf.exe", "--combine", r"C:\a.pdf"])
    assert files == [r"C:\a.pdf"]
    assert combine is True


def test_parse_cli_plain_open():
    files, combine = parse_cli(["rapid-pdf.exe", r"C:\a.pdf"])
    assert files == [r"C:\a.pdf"]
    assert combine is False


# ---------------------------------------------------------------------------
# The broken assumption, documented so nobody "simplifies" the election away.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows named-pipe behaviour")
def test_qlocalserver_listen_is_not_exclusive(qapp, unique_name):
    """listen() cannot be used to elect a primary: two servers both win it."""
    a, b = QLocalServer(), QLocalServer()
    try:
        assert a.listen(unique_name) is True
        assert b.listen(unique_name) is True     # <- the whole bug, in one line
    finally:
        a.close()
        b.close()


def test_claim_primary_is_idempotent_in_one_process(unique_name):
    assert si.claim_primary() is True
    assert si.claim_primary() is True          # already ours, still ours


def test_claim_primary_is_exclusive_across_processes(unique_name, tmp_path):
    """The regression test: only ONE of a launch burst may become primary.

    Runs the election in three real subprocesses, the way Explorer fires the
    verb once per selected file. Against the pre-fix code (election by
    QLocalServer.listen) this returns three winners.
    """
    script = textwrap.dedent("""
        import json, os, sys
        sys.path.insert(0, sys.argv[1])
        from PySide6.QtCore import QCoreApplication
        import core.single_instance as si
        si.SERVER_NAME = sys.argv[2]
        app = QCoreApplication(sys.argv)
        won = si.claim_primary()
        print(json.dumps({"won": won}))
        sys.stdout.flush()
        if won:
            import time
            time.sleep(4)      # hold the claim while the siblings try
    """)
    runner = tmp_path / "rp_election_probe.py"
    runner.write_text(script, encoding="utf-8")

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    procs = [
        subprocess.Popen(
            [sys.executable, str(runner), REPO, unique_name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for _ in range(3)
    ]
    outs = [p.communicate(timeout=120)[0] for p in procs]
    wins = [json.loads(o.strip().splitlines()[-1])["won"] for o in outs]
    assert sum(wins) == 1, f"expected exactly one primary, got {wins}"


# ---------------------------------------------------------------------------
# Aggregation: one burst, one batch.
# ---------------------------------------------------------------------------

def test_burst_becomes_one_batch(qapp, unique_name, pdfs):
    server = InstanceServer()
    batches = []
    server.batch_ready.connect(lambda f, c: batches.append((list(f), c)))
    server.arm()
    for p in pdfs:
        server.add_launch([p], True)
    _pump(qapp, si.BURST_AGGREGATE_MS + 400)
    assert len(batches) == 1
    files, combine = batches[0]
    assert sorted(files) == sorted(pdfs)
    assert combine is True


def test_straggler_past_the_single_file_window_still_joins(qapp, unique_name, pdfs):
    """A launch that lands after AGGREGATE_MS must not start a second batch.

    Explorer's per-file processes cold-start against each other, so arrivals
    spread well past 700 ms. Measured on a 15-file burst: ~5 s between first
    and last. Once a second launch has arrived the window widens; this pins
    that, and fails against a flat 700 ms window.
    """
    server = InstanceServer()
    batches = []
    server.batch_ready.connect(lambda f, c: batches.append((list(f), c)))
    server.arm()
    server.add_launch([pdfs[0]], True)
    server.add_launch([pdfs[1]], True)
    _pump(qapp, si.AGGREGATE_MS + 250)          # past the single-file window
    assert batches == [], "batch closed before the straggler arrived"
    server.add_launch([pdfs[2]], True)
    _pump(qapp, si.BURST_AGGREGATE_MS + 400)
    assert len(batches) == 1
    assert sorted(batches[0][0]) == sorted(pdfs[:3])


def test_nothing_is_emitted_before_arm(qapp, unique_name, pdfs):
    """listen() now runs before the window is built, so batches must wait."""
    server = InstanceServer()
    batches = []
    server.batch_ready.connect(lambda f, c: batches.append((list(f), c)))
    server.add_launch(pdfs[:2], True)
    _pump(qapp, si.AGGREGATE_MS + 400)
    assert batches == [], "emitted with nothing wired up to receive it"
    server.arm()
    _pump(qapp, si.BURST_AGGREGATE_MS + 400)
    assert len(batches) == 1
    assert sorted(batches[0][0]) == sorted(pdfs[:2])


def test_batch_dedupes_and_drops_missing(qapp, unique_name, pdfs, tmp_path):
    server = InstanceServer()
    batches = []
    server.batch_ready.connect(lambda f, c: batches.append((list(f), c)))
    server.arm()
    server.add_launch([pdfs[0], pdfs[0]], False)
    server.add_launch([str(tmp_path / "gone.pdf")], False)
    _pump(qapp, si.BURST_AGGREGATE_MS + 400)
    assert len(batches) == 1
    assert batches[0][0] == [pdfs[0]]


def test_window_cannot_be_held_open_forever(qapp, unique_name, pdfs, monkeypatch):
    """A slow trickle of launches still flushes at the hard ceiling."""
    monkeypatch.setattr(si, "MAX_AGGREGATE_MS", 900)
    monkeypatch.setattr(si, "BURST_AGGREGATE_MS", 600)
    server = InstanceServer()
    batches = []
    server.batch_ready.connect(lambda f, c: batches.append((list(f), c)))
    server.arm()
    for p in pdfs[:3]:
        server.add_launch([p], True)
        _pump(qapp, 400)                        # keeps restarting the timer
    _pump(qapp, 800)
    assert len(batches) >= 1, "the ceiling never closed the window"


# ---------------------------------------------------------------------------
# End to end: a real multi-process burst lands as ONE combine batch.
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_multiprocess_combine_burst_lands_as_one_batch(unique_name, pdfs, tmp_path):
    """The user-visible bug, end to end.

    Five processes launched at once with --combine, exactly as Explorer fires
    the verb. Pre-fix this produced several windows and a split batch.
    """
    log = tmp_path / "burst.jsonl"
    script = textwrap.dedent("""
        import json, os, sys, time
        sys.path.insert(0, sys.argv[1])
        from PySide6.QtCore import QCoreApplication, QTimer
        import core.single_instance as si
        si.SERVER_NAME = sys.argv[2]
        log = sys.argv[3]

        def ev(**kw):
            kw["pid"] = os.getpid()
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(kw) + "\\n")

        files, combine = si.parse_cli(sys.argv[3:])
        app = QCoreApplication(sys.argv)
        if not si.claim_primary():
            if si.forward_to_primary(files, combine):
                sys.exit(0)
        server = si.InstanceServer()
        server.listen()
        if files or combine:
            server.add_launch(files, combine)
        server.batch_ready.connect(
            lambda f, c: ev(batch=sorted(os.path.basename(x) for x in f), combine=c))
        server.arm()
        QTimer.singleShot(9000, app.quit)
        app.exec()
    """)
    runner = tmp_path / "burst_probe.py"
    runner.write_text(script, encoding="utf-8")

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    procs = [
        subprocess.Popen(
            [sys.executable, str(runner), REPO, unique_name, str(log),
             "--combine", p],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for p in pdfs
    ]
    for p in procs:
        p.communicate(timeout=180)

    lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l]
    batches = [l for l in lines if "batch" in l]
    assert len(batches) == 1, f"burst split into {len(batches)} batches: {batches}"
    assert batches[0]["combine"] is True
    assert batches[0]["batch"] == sorted(os.path.basename(p) for p in pdfs)

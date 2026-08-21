"""The part a running app cannot do to itself: replace its own exe.

WHY THERE IS A BATCH FILE HERE. Windows holds an open handle on the image of a
running process, so `rapid-pdf.exe` cannot be overwritten by the code inside
`rapid-pdf.exe`. Something else has to do it, after the app has gone, and then
start the app again. That something has to outlive the app's exit, which rules
out a thread.

WHY A .cmd AND NOT THE TWO OBVIOUS ALTERNATIVES:

  * PowerShell. Execution policy. An unsigned .ps1 is refused outright under
    RemoteSigned or AllSigned, and the failure would land at the worst possible
    moment: after the app has exited, with the user looking at a closed app and
    no message. cmd.exe has no equivalent gate and is on every Windows there is.
  * A second frozen exe. It would grow the build and it would be a SECOND
    unsigned binary. rapid-pdf.exe is already unsigned; adding another one
    whose entire job is to overwrite an exe and relaunch it is the exact shape
    of a thing endpoint protection exists to stop.

A batch file is plain text, generated fresh for one update, and it deletes
itself afterwards. Everything it does is a standard command someone can read.

THE FOUR RULES IT IMPLEMENTS, each a failure it has to survive:

  * IT WAITS for the app's PID to be gone, and gives up with a message rather
    than racing it. Moving files under a live process is how you get a
    half-updated install and a crash on the next launch.
  * THE EXE IS SWAPPED BY RENAME, never copied over the top. Staging sits
    beside the install on the same volume for exactly this reason, so each
    move is a directory operation: it either happened or it did not, and there
    is never a moment where a half-written file sits under the real name.
  * THE PREVIOUS EXE IS KEPT as rapid-pdf.exe.bak. If anything after that
    point fails, including the relaunch, the .bak goes back and the old
    version starts. A failed update must never leave a dead install.
  * IT WRITES A LOG beside the exe, every run, success or failure. When an
    update goes wrong the app is not running, so there is nowhere else for it
    to say anything.

EVERY EXTERNAL COMMAND IS FULLY QUALIFIED to %SystemRoot%\\System32, and that
is not tidiness. On a machine with Git for Windows on PATH, a bare `find`
resolves to GNU find, which reads its argument as a directory, fails, and
reports "not running" for a process that is very much running. The cost of
that would be the exe being replaced underneath a live app. Same risk for
`sort`, `robocopy` and `ping`.

WHAT IT HONESTLY CANNOT DO. It can tell that the new exe started; it cannot
tell that it stayed up. A build that launches and dies on its own second line
is beyond a batch file, and pretending otherwise would mean a rollback on a
timer, which would fight a slow first launch. The .bak is the recovery that
does work: rename it back by hand and the previous version runs again.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from core.update.client import EXE_NAME, StagedUpdate

#: Written beside the install, not inside it, so the tree being updated does
#: not contain the thing doing the updating.
SCRIPT_NAME = "apply-update.cmd"

#: The swap's own log. Beside the exe, because that is where somebody looks
#: when the app did not come back, and the app is not running to show it.
LOG_NAME = "update.log"

#: Roughly a second per turn, so about two minutes for the app to shut down.
#: Long enough for a large PDF to finish saving, short enough that a wedged
#: process does not leave a batch file spinning on a machine forever.
WAIT_TURNS = 120


class SwapNotStarted(Exception):
    """The helper could not be written or launched. Nothing was changed."""


def script_path(target: Path) -> Path:
    return Path(target).parent / SCRIPT_NAME


def log_path(target: Path) -> Path:
    return Path(target) / LOG_NAME


def _safe(text: str) -> str:
    """A string that cannot break out of an `echo` in a batch file.

    Everything meaningful to cmd.exe is dropped rather than escaped. These are
    log lines and a version number, so losing a character is cosmetic and
    getting the quoting wrong is a broken update.
    """
    return "".join(c for c in str(text)
                   if c.isalnum() or c in " ._-:/\\+()[]").strip()


def build_script(staged: StagedUpdate, *, exe_name: str = EXE_NAME,
                 pid: int, wait_turns: int = WAIT_TURNS) -> str:
    """The batch file, as text. Pure, so a test can read what will run.

    Kept as one function rather than assembled from fragments: the order of
    these steps IS the safety argument, and it should read top to bottom for
    somebody working out what a failed update did.
    """
    install = str(Path(staged.install_dir))
    payload = str(Path(staged.payload_dir))
    staging = str(Path(staged.staging_dir))
    # These go into `set "VAR=..."` lines. A quote would close the assignment
    # and a percent sign would be read as a variable, and either produces a
    # script that does something other than what it says, while the app is
    # closed and nobody is watching. Refusing is the honest answer.
    for label, value in (("install", install), ("staging", staging)):
        if '"' in value or "%" in value:
            raise SwapNotStarted(
                f"The update cannot be applied: the {label} path contains a "
                f"quote or a percent sign, which a batch file cannot carry "
                f"safely.\n  {value}\n"
                "Nothing has been changed. Move the app to a path without "
                "those characters."
            )

    lines = [
        "@echo off",
        "setlocal EnableExtensions",
        "rem Generated by core/update/swap.py for one update, then it deletes",
        "rem itself. Safe to delete by hand if it is ever found lying about.",
        f'set "INSTALL={install}"',
        f'set "PAYLOAD={payload}"',
        f'set "STAGING={staging}"',
        f'set "EXE={exe_name}"',
        f'set "LOG=%INSTALL%\\{LOG_NAME}"',
        f'set "PID={int(pid)}"',
        f'set "LIMIT={int(wait_turns)}"',
        'set "WAITED=0"',
        "rem Fully qualified, every one of them: a bare `find` on a machine",
        "rem with Git for Windows on PATH is GNU find, which fails and reports",
        "rem a running app as closed. That would swap the exe under a live",
        "rem process. Same risk for robocopy and ping.",
        'set "SYS=%SystemRoot%\\System32"',
        "",
        f'call :log "---- updating to {_safe(staged.info.version)} ----"',
        'call :log "waiting for Rapid PDF (pid %PID%) to close"',
        "",
        ":wait",
        '"%SYS%\\tasklist.exe" /FI "PID eq %PID%" /NH 2>nul | '
        '"%SYS%\\find.exe" "%PID%" >nul',
        "if errorlevel 1 goto closed",
        "set /a WAITED+=1",
        "if %WAITED% GEQ %LIMIT% goto stuck",
        '"%SYS%\\ping.exe" -n 2 127.0.0.1 >nul',
        "goto wait",
        "",
        ":closed",
        'call :log "Rapid PDF has closed, applying the update"',
        "",
        "rem Everything except the exe first. /MOVE empties the payload as it",
        "rem goes; the exe is excluded because it is swapped by rename below.",
        '"%SYS%\\robocopy.exe" "%PAYLOAD%" "%INSTALL%" /E /MOVE '
        '/XF "%PAYLOAD%\\%EXE%" /NFL /NDL /NJH /NJS /NP /R:2 /W:2 '
        '>>"%LOG%" 2>&1',
        "rem robocopy exit codes below 8 are success, 8 and up are failures.",
        "if errorlevel 8 goto copyfailed",
        "",
        "rem The exe, by rename, both ways. The old one becomes the .bak that",
        "rem every failure below falls back on.",
        'if exist "%INSTALL%\\%EXE%.bak" del /q "%INSTALL%\\%EXE%.bak" '
        ">nul 2>&1",
        'if not exist "%INSTALL%\\%EXE%" goto putnew',
        'move /Y "%INSTALL%\\%EXE%" "%INSTALL%\\%EXE%.bak" >nul 2>&1 '
        "|| goto locked",
        ":putnew",
        'move /Y "%PAYLOAD%\\%EXE%" "%INSTALL%\\%EXE%" >nul 2>&1 '
        "|| goto rollback",
        'if not exist "%INSTALL%\\%EXE%" goto rollback',
        "",
        'call :log "files are in place, starting Rapid PDF"',
        'start "" /D "%INSTALL%" "%INSTALL%\\%EXE%"',
        "if errorlevel 1 goto rollback",
        'rd /s /q "%STAGING%" >nul 2>&1',
        'call :log "update finished. The previous exe is beside the new one '
        'as %EXE%.bak"',
        "goto done",
        "",
        ":stuck",
        'call :log "GAVE UP: Rapid PDF (pid %PID%) was still running after '
        '%LIMIT% checks. NOTHING has been changed and the update is still '
        'staged in %STAGING%. Close Rapid PDF and run this file again."',
        "goto done",
        "",
        ":locked",
        'call :log "FAILED: %EXE% could not be renamed, so something still '
        'has it open. NOTHING has been changed."',
        "goto restart",
        "",
        ":copyfailed",
        'call :log "FAILED: robocopy could not put the supporting files in '
        'place. The install may now be PART updated: check the robocopy '
        'output above. %EXE% was NOT touched."',
        "goto restart",
        "",
        ":rollback",
        'call :log "FAILED partway through the swap. Putting the previous '
        '%EXE% back."',
        'if exist "%INSTALL%\\%EXE%.bak" move /Y "%INSTALL%\\%EXE%.bak" '
        '"%INSTALL%\\%EXE%" >nul 2>&1',
        'call :log "the previous %EXE% is back and has been started. The new '
        'version was NOT applied. If the robocopy line above moved any '
        'supporting files in before this, they are still there, so run the '
        'update again to finish the job properly."',
        "goto restart",
        "",
        ":restart",
        'if exist "%INSTALL%\\%EXE%" start "" /D "%INSTALL%" '
        '"%INSTALL%\\%EXE%"',
        "goto done",
        "",
        ":log",
        'echo [%DATE% %TIME%] %~1>>"%LOG%"',
        "exit /b 0",
        "",
        ":done",
        "rem Delete this file on the way out. The (goto) trick closes the",
        "rem batch context first, so cmd.exe is not still reading the file it",
        "rem is being asked to remove.",
        '(goto) 2>nul & del "%~f0"',
        "",
    ]
    return "\r\n".join(lines)


def write_script(staged: StagedUpdate, *, exe_name: str = EXE_NAME,
                 pid: int | None = None,
                 wait_turns: int = WAIT_TURNS) -> Path:
    """Write the helper beside the install and return its path."""
    pid = os.getpid() if pid is None else pid
    target = script_path(staged.install_dir)
    body = build_script(staged, exe_name=exe_name, pid=pid,
                        wait_turns=wait_turns)
    try:
        # ASCII, deliberately. cmd.exe reads a batch file in the console
        # codepage, and a UTF-8 BOM on line one becomes three characters in
        # front of "@echo off", which prints an error before anything runs.
        target.write_text(body, encoding="ascii", errors="replace", newline="")
    except OSError as exc:
        raise SwapNotStarted(
            f"The update could not be applied: {target} could not be written "
            f"({exc.strerror or exc}). Nothing has been changed."
        ) from exc
    return target


def _comspec() -> str:
    """The real cmd.exe, by full path where Windows will name one.

    Same reasoning as the qualified commands inside the script: PATH on these
    machines carries Git for Windows and whatever else got installed, and the
    one process that replaces the app's exe is not the place to find out what
    a bare name resolves to.
    """
    return (os.environ.get("COMSPEC")
            or str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                   / "System32" / "cmd.exe"))


def launch(script: Path, target: Path) -> "subprocess.Popen":
    """Start the helper so it outlives the app, with no window.

    Returns the Popen. The app ignores it and exits; a test uses it to wait on
    the helper and to make sure nothing is left running.

    CREATE_NO_WINDOW, AND NOT DETACHED_PROCESS. DETACHED_PROCESS gives the
    helper no console at all, and tasklist.exe writes NOTHING when it has no
    console: the wait loop then reads an empty result as "still running" and
    spins until it gives up, every time, on every machine. CREATE_NO_WINDOW
    gives the process its own console that is never shown, and the process
    still outlives its parent because the console belongs to it.

    CREATE_NEW_PROCESS_GROUP so a Ctrl-C aimed at the app cannot reach it.

    The working directory is the install's PARENT, because a process holding
    the install folder as its cwd is a process holding a handle on it.
    """
    script, target = Path(script), Path(target)
    flags = 0
    if sys.platform == "win32":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        return subprocess.Popen(
            [_comspec(), "/c", str(script)],
            cwd=str(target.parent),
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        raise SwapNotStarted(
            f"The update could not be started: {exc.strerror or exc}. "
            "Nothing has been changed."
        ) from exc


def apply(staged: StagedUpdate, *, exe_name: str = EXE_NAME,
          pid: int | None = None, wait_turns: int = WAIT_TURNS) -> Path:
    """Write the helper and start it. The caller must then exit the app.

    Returns the script's path so the caller can name it if the app never comes
    back. Nothing on disk has changed when this returns: the helper is sitting
    in its wait loop, watching for this process to go.
    """
    script = write_script(staged, exe_name=exe_name, pid=pid,
                          wait_turns=wait_turns)
    launch(script, staged.install_dir)
    return script

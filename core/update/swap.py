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

THE FIVE RULES IT IMPLEMENTS, each a failure it has to survive:

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
  * IT COUNTS WHAT IT WROTE before it calls the update done, and rolls back
    when the count does not add up. This rule was bought expensively. The
    version before it decided success on two things: robocopy exiting under 8,
    which it does when it copied NOTHING, and the new exe existing as a NAME,
    which says nothing about its size. An install ended up with 34 files, no
    exe, and an _internal holding four entries where hundreds belong, and the
    log said "update finished" underneath it. A check that cannot fail is not
    a check, it is a sentence about how things usually go.
  * IT WRITES A LOG beside the exe, every run, success or failure, and the log
    carries the numbers it checked, not just the verdict. When an update goes
    wrong the app is not running, so there is nowhere else for it to say
    anything, and "it failed" without the counts sends the next person
    guessing. Robocopy's own job summary is left in there for the same reason.

EVERY EXTERNAL COMMAND IS FULLY QUALIFIED to %SystemRoot%\\System32, and that
is not tidiness. On a machine with Git for Windows on PATH, a bare `find`
resolves to GNU find, which reads its argument as a directory, fails, and
reports "not running" for a process that is very much running. The cost of
that would be the exe being replaced underneath a live app. Same risk for
`sort`, `robocopy` and `ping`.

WHAT IT HONESTLY CANNOT DO. It cannot tell that the new exe stayed up, and it
cannot even tell that it started: `start` returns 0 the moment it hands the exe
to Windows, so there is no errorlevel worth reading after it and the script no
longer pretends there is. A build that launches and dies on its own second line
is beyond a batch file, and covering it would mean a rollback on a timer, which
would fight a slow first launch. What it CAN check is what is on disk before
the launch, and it does, to the file. The .bak is the recovery for the rest:
rename it back by hand and the previous version runs again.
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

    THE SUCCESS CRITERIA, in full, because they used to be weaker than they
    looked. "update finished" is written when, and only when, all of these
    held:

      1. the app's process was gone before anything moved,
      2. robocopy exited under 8,
      3. the exe was renamed into place and exists,
      4. the install then held at least `staged.file_count` files, counted off
         the disk after the swap,
      5. and that exe was exactly `staged.exe_bytes` bytes.

    4 and 5 are the ones that carry the weight. 2 passes when robocopy copied
    nothing at all, and 3 passes on a zero byte file with the right name.
    """
    install = str(Path(staged.install_dir))
    payload = str(Path(staged.payload_dir))
    staging = str(Path(staged.staging_dir))
    # The two numbers the script verifies against. Refused rather than
    # defaulted: a zero here would make the count check pass on any install and
    # the size check fail on every one, and both of those are worse than being
    # told the update cannot be applied while nothing has been touched.
    expect_files = int(staged.file_count)
    expect_exe = int(staged.exe_bytes)
    if expect_files < 1 or expect_exe < 1:
        raise SwapNotStarted(
            f"The update cannot be applied: the staged copy reports "
            f"{expect_files} files and a {expect_exe} byte {exe_name}, so "
            "there is nothing to check the result against.\n"
            "Nothing has been changed. Download the update again."
        )
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
        "rem What the finished install has to look like. EXPECT is a floor and",
        "rem not an equality: an install legitimately carries files this",
        "rem payload did not bring (update.log, the .bak, anything an older",
        "rem version left), but after a copy that worked, every one of the",
        "rem payload's files is in there, so the total cannot be lower.",
        f'set "EXPECT={expect_files}"',
        f'set "EXESIZE={expect_exe}"',
        'set "COUNT=0"',
        'set "NEWSIZE=0"',
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
        'call :log "this update is %EXPECT% files with a %EXESIZE% byte '
        '%EXE%, and the install will be counted afterwards to prove it"',
        "",
        "rem Everything except the exe first. /MOVE empties the payload as it",
        "rem goes; the exe is excluded because it is swapped by rename below.",
        "rem /NJH and /NJS are deliberately NOT here. They suppress robocopy's",
        "rem job header and summary, and the summary is the only record of how",
        "rem many files actually moved: without it a failed update cannot be",
        "rem read afterwards, which is how one shipped 34 files and said it had",
        "rem finished. /NFL /NDL stay, so the log gets the totals and not 252",
        "rem lines of filenames.",
        '"%SYS%\\robocopy.exe" "%PAYLOAD%" "%INSTALL%" /E /MOVE '
        '/XF "%PAYLOAD%\\%EXE%" /NFL /NDL /NP /R:2 /W:2 '
        '>>"%LOG%" 2>&1',
        "rem robocopy exit codes below 8 are success, 8 and up are failures.",
        "rem This is the WEAKEST of the checks and nothing is trusted to it: 0",
        "rem means it copied nothing, and 1, 2 and 3 mean it copied some of it.",
        "rem All of those pass here and are caught by the count below.",
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
        "rem THE CHECK THAT DECIDES. Everything above reports on what it tried",
        "rem to do; these two lines read what is actually on the disk. The",
        "rem count is of files only (/a-d), the whole tree (/s), bare (/b),",
        "rem and find.exe counts the lines. find is fully qualified for the",
        "rem same reason as every other command here: GNU find would read the",
        "rem argument as a directory and answer something useless.",
        "for /f %%N in ('dir /a-d /s /b \"%INSTALL%\" ^| "
        "\"%SYS%\\find.exe\" /c /v \"\"') do set \"COUNT=%%N\"",
        "rem %%~zA is the size of the exe now sitting in the install. An exe",
        "rem that is missing leaves this empty, which `if not defined` turns",
        "rem back into 0 rather than into a syntax error two lines down.",
        'for %%A in ("%INSTALL%\\%EXE%") do set "NEWSIZE=%%~zA"',
        'if not defined COUNT set "COUNT=0"',
        'if not defined NEWSIZE set "NEWSIZE=0"',
        'call :log "checked: %COUNT% files in the install, expected at least '
        '%EXPECT%. %EXE% is %NEWSIZE% bytes, expected %EXESIZE%."',
        "if %COUNT% LSS %EXPECT% goto shortcount",
        "if not %NEWSIZE% EQU %EXESIZE% goto badexe",
        "",
        'call :log "verified, starting Rapid PDF"',
        'start "" /D "%INSTALL%" "%INSTALL%\\%EXE%"',
        "rem No errorlevel check after start, on purpose. It returns 0 as soon",
        "rem as it hands the exe to Windows, so a check there passes whatever",
        "rem happens next, and a check that cannot fire reads like a promise",
        "rem this file is not in a position to make.",
        "rem The staging folder goes only now, after the checks passed. While",
        "rem anything above can still send this to :rollback, the payload is",
        "rem the only copy of the new build on the machine.",
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
        'has it open. %EXE% itself was NOT changed, but the robocopy above '
        'had already run, so the supporting files may have moved in. Run the '
        'update again from %STAGING% once nothing is holding the exe."',
        "goto restart",
        "",
        ":shortcount",
        'call :log "FAILED: the install holds %COUNT% files and this update '
        'needed at least %EXPECT%, so the copy did not finish. See the '
        'robocopy summary above for what it managed. Rolling back."',
        "goto rollback",
        "",
        ":badexe",
        'call :log "FAILED: %EXE% in the install is %NEWSIZE% bytes and the '
        'downloaded one is %EXESIZE%, so what is under that name is not the '
        'new build. Rolling back."',
        "goto rollback",
        "",
        ":copyfailed",
        'call :log "FAILED: robocopy gave up putting the supporting files in '
        'place. The install may now be PART updated: check the robocopy '
        'summary above. Rolling back."',
        "goto rollback",
        "",
        ":rollback",
        'call :log "FAILED partway through the swap. Putting the previous '
        '%EXE% back."',
        "rem The new exe goes back to the payload FIRST, while it still",
        "rem exists. Overwriting it with the .bak would destroy the only copy",
        "rem of the new build on the machine and leave nothing to retry from.",
        'if not exist "%PAYLOAD%" md "%PAYLOAD%" >nul 2>&1',
        'if exist "%INSTALL%\\%EXE%.bak" if exist "%INSTALL%\\%EXE%" '
        'move /Y "%INSTALL%\\%EXE%" "%PAYLOAD%\\%EXE%" >nul 2>&1',
        'if exist "%INSTALL%\\%EXE%.bak" move /Y "%INSTALL%\\%EXE%.bak" '
        '"%INSTALL%\\%EXE%" >nul 2>&1',
        'if not exist "%INSTALL%\\%EXE%" call :log "SERIOUS: there is no '
        '%EXE% in the install and no %EXE%.bak to put back. Reinstall Rapid '
        'PDF from the release page."',
        'call :log "the previous %EXE% is back and has been started. The new '
        'version was NOT applied. The staged copy has been LEFT in %STAGING% '
        'on purpose, so run this update again rather than downloading it '
        'twice. If the robocopy line above moved any supporting files in '
        'before this, they are still there, and running the update again '
        'finishes the job properly."',
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

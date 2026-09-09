"""Hand the update to the installer that built the install.

WHAT THIS REPLACED, AND WHY. Up to 1.9.0 an update was done by the app to
itself, in a batch file this repo generated at run time. That file waited for
the app's own PID to disappear by polling `tasklist.exe`, slept by running
`ping.exe -n 2 127.0.0.1`, moved the new build in with `robocopy /E /MOVE`, and
renamed the new exe over the running one while keeping the old as a `.bak`. It
was careful, it was well tested, and it worked.

It also could not ship. On 9 September 2026 Sophos Endpoint Agent fired
`Evade_13a (T1036.003)` on this repo's own test suite, and it was not wrong
about what it saw. Every one of those steps is on a behavioural detection list
on its own:

  * a script written to disk at run time and executed,
  * a hidden window (`CREATE_NO_WINDOW`) on the process that runs it,
  * polling a process list for a PID,
  * `ping` used as a sleep timer, which is a signature so old it has a name,
  * a bulk file move into a program directory,
  * an executable renamed over another executable.

Chained in that order, by an unsigned binary, that is not "a few heuristics",
it is the textbook shape of a dropper. No code-signing certificate fixes it
either: a signature answers "who wrote this", and a behavioural engine is
asking "what is it doing". The only fix is to stop doing it.

WHAT AN UPDATE IS NOW. For an INSTALLED copy: download the same
`rapid-pdf-setup-X.Y.Z.exe` that a person would download by hand, verify it
against the sha256 GitHub publishes, and run it with Inno Setup's own silent
switches. Inno closes the app through the Windows Restart Manager, replaces the
files, rewrites the registry, and our own `[Run]` entry starts the new build.
One process, made by a tool used by a very large number of Windows programs,
doing the thing it exists to do. That is a shape endpoint protection is built
to allow.

For a PORTABLE copy: nothing is applied at all. See "PORTABLE" below.

THE SWITCHES, AND WHY EACH ONE IS THERE:

  /SILENT                 no wizard, but the installation progress window IS
                          shown. /VERYSILENT would hide that too, and a
                          hidden install is both worse for the user (the app
                          vanishes and nothing says why) and the exact
                          property that makes a thing look like a dropper.
                          Visible is the point.
  /SP-                    turns off the "This will install Rapid PDF X.Y.Z. Do
                          you wish to continue?" message box, which /SILENT
                          does NOT suppress on its own. Without it an update
                          can stop dead on a modal box, behind whatever the
                          user went off to do after the app closed. Only this
                          prompt is suppressed: /SUPPRESSMSGBOXES is
                          deliberately NOT passed, because an install that
                          fails has nowhere else to say so once the app has
                          gone.
  /CLOSEAPPLICATIONS      Inno's CloseApplications directive already defaults
                          to yes, so this is stated rather than relied on:
                          it is what closes the app if it has not finished
                          exiting by the time Setup reaches the file phase.
  /NORESTARTAPPLICATIONS  Inno can only restart an app that called
                          RegisterApplicationRestart, and RapidPDF does not,
                          so leaving this on would be a promise nothing keeps.
                          The relaunch is done by the [Run] entry below
                          instead, which is deterministic.
  /RAPIDPDFRELAUNCH=1     read by `RelaunchAfterUpdate` in rapid-pdf.iss. It
                          gates a plain [Run] entry (no checkbox, no
                          skipifsilent) that starts the app once the files are
                          in. Absent, as it is for a person double-clicking
                          setup, that entry does not run and the ordinary
                          "Launch RapidPDF" checkbox is what they get.
  /DIR=<install>          belt and braces. A fixed AppId plus
                          UsePreviousAppDir would find the same folder on its
                          own, but this code only runs when it has ALREADY
                          confirmed that this folder is the one Inno's own
                          uninstall key points at, so it may as well say so.
                          It is what stops an update ever creating a second
                          install beside the first.
  /LOG=<install>\\update.log
                          the same file the batch file used to write, in the
                          same place, for the same reason: when an update goes
                          wrong the app is not running, so there is nowhere
                          else for anything to be said. Inno's own log is far
                          more detailed than the one this repo used to write.

THIS COMMAND LINE WAS RUN AGAINST A REAL INSTALLER before it shipped, on
9 Sept 2026 with Inno Setup 6, using a throwaway script that did nothing but
record what happened. Two things about it are not stated outright anywhere in
Inno's documentation and both are load bearing: that a plain [Run] entry fires
under /SILENT, and that Setup accepts a command line switch of its own that it
knows nothing about. Both held. Setup exited 0, the relaunch entry ran, the
"Launch RapidPDF" checkbox entry did not (so nothing can start the app twice),
and the log landed at the path it was given.

PORTABLE INSTALLS GET NO SELF-UPDATE, ON PURPOSE. A portable copy is a folder
somebody unzipped, and the installer cannot update it: Inno installs to
%LocalAppData%\\Programs\\RapidPDF whatever folder the running copy is in, so
running it against a portable copy makes a SECOND install and leaves the
portable one stale, which is worse than doing nothing. The alternative would
be keeping the whole file-swap machinery alive for that one case, which is the
entire thing this change exists to delete. So a portable copy is told, in the
notice, to download the new zip and unpack it over its folder. That is what a
portable build is: a folder you manage yourself.

WHAT IS NOT DONE, STATED PLAINLY, because the code that came before did it:

  * There is no `.bak` of the previous exe any more. Rolling back means
    installing the previous release from the Releases page. Inno undoes its
    own work when an install FAILS partway ("Setup was not completed"); what
    is gone is the ability to undo an install that SUCCEEDED and then turned
    out to be a bad build.
  * Nothing counts the files in the install afterwards, and nothing measures
    the new exe. That check existed because robocopy could exit 0 having
    copied nothing. Inno does not have that failure mode: it verifies its own
    compressed data, and it reports what it did through an exit code and a
    log. The check moved to the only place this code still controls, which is
    before the installer is run at all (see client._check_installer).
  * Nothing refuses a path containing a quote or a percent sign. That rule
    existed because a path had to survive being pasted into a batch file. The
    command is now an argv LIST handed to CreateProcess, so there is no shell
    to survive and no quoting to get wrong. That one is a strengthening, not
    a loss.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core.update.client import StagedInstaller, log_path

#: Inno's own switches. Named rather than inlined so the test that pins them
#: reads as a list of decisions and not as a string comparison.
SILENT = "/SILENT"
NO_STARTUP_PROMPT = "/SP-"
CLOSE_APPS = "/CLOSEAPPLICATIONS"
NO_RESTART_APPS = "/NORESTARTAPPLICATIONS"

#: Ours. rapid-pdf.iss reads it with {param:RAPIDPDFRELAUNCH|0} and uses it to
#: gate the [Run] entry that starts the app again. Inno ignores command line
#: switches it does not know, which is the whole basis of {param:}.
RELAUNCH_SWITCH = "/RAPIDPDFRELAUNCH=1"


class InstallerNotStarted(Exception):
    """The installer could not be started. Nothing was changed."""


def build_command(staged: StagedInstaller, *,
                  log: Path | None = None) -> list[str]:
    """The argv the installer is started with. Pure, so a test can read it.

    A LIST, NEVER A STRING. Every element goes to CreateProcess through
    subprocess's own quoting, so an install path with a space in it (which is
    every install made before the 1.8.0 rename:
    %LocalAppData%\\Programs\\Rapid PDF) needs no escaping here and cannot be
    split in half by one.
    """
    target = Path(staged.install_dir)
    setup = Path(staged.installer_path)
    if not setup.is_file():
        raise InstallerNotStarted(
            f"The update cannot be applied: {setup.name} is not where it was "
            "downloaded to.\n"
            "Nothing has been changed. Download the update again."
        )
    command = [
        str(setup),
        SILENT,
        NO_STARTUP_PROMPT,
        CLOSE_APPS,
        NO_RESTART_APPS,
        RELAUNCH_SWITCH,
        f"/DIR={target}",
    ]
    if log is not None:
        command.append(f"/LOG={Path(log)}")
    return command


def prepare_log(target: Path) -> Path | None:
    """Make sure the log path can be written, or give it up rather than fail.

    /LOG="filename" ABORTS THE WHOLE INSTALL if Setup cannot create the file,
    which would turn a logging convenience into a failed update. So the file
    is created here first, by the app, while the app is still running and can
    say something about it. If that does not work the switch is left off and
    Inno logs into TEMP under a name of its own, which is worse to find and
    better than not updating.
    """
    path = log_path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError:
        return None
    return path


def launch(staged: StagedInstaller) -> "subprocess.Popen":
    """Start the installer and return without waiting for it.

    NO CREATE_NO_WINDOW, AND THAT IS THE POINT. The old helper hid its window
    because a console flashing up mid-update looked broken. Setup is a GUI
    program with a progress window of its own, so there is nothing to hide,
    and hiding a process that is about to rewrite a program directory is one
    of the behaviours that got the old design convicted.

    CREATE_NEW_PROCESS_GROUP stays: a Ctrl-C aimed at the app has no business
    reaching the installer. It is not a concealment flag.

    The working directory is the install's PARENT. A process holding the
    install folder as its cwd is a process holding a handle on it, and Setup
    is about to replace everything in there.

    The caller must exit the app straight after this returns. Setup will close
    it through the Restart Manager if it does not, but exiting cleanly is how
    unsaved work gets its prompt, and the window has already handled that.
    """
    target = Path(staged.install_dir)
    command = build_command(staged, log=prepare_log(target))
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        return subprocess.Popen(
            command,
            cwd=str(target.parent),
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        raise InstallerNotStarted(
            f"The update could not be started: {exc.strerror or exc}. "
            "Nothing has been changed."
        ) from exc


def apply(staged: StagedInstaller) -> "subprocess.Popen":
    """Run the installer. The caller must then exit the app.

    Returns the Popen so a test can wait on it. The app ignores it: by the
    time this matters the app is on its way out, and a process that outlives
    its parent is exactly what is wanted here.
    """
    return launch(staged)

"""Which build this is, as a string the running app can read.

WHY THIS FILE EXISTS AT ALL. Until now the version lived only in two build
inputs: `rapid-pdf.iss` (what the installer stamps) and
`packaging/version_info.txt` (what Explorer shows in Properties). Neither is
readable from inside the running app, so the app genuinely did not know which
build it was. A self-updater whose whole job is "is the release newer than me"
cannot be written on top of that, so the number moves here first and the two
build inputs follow it.

ONE NUMBER, THREE PLACES, AND A TEST THAT SAYS SO. Inno Setup cannot import
Python and PyInstaller's version resource is a literal, so the value still has
to be typed into all three files on a release. `tests/test_version.py` reads
all three and fails when they drift, which turns a silent mismatch into a red
test at the moment it is introduced. That is the cheapest correct answer here;
generating the other two would mean a build step to maintain for one string.

SEMVER, AND ONLY SEMVER. The tags on GitHub are `vX.Y.Z` and the release
titles are "Rapid PDF X.Y.Z". core/update/release.py compares the three
integers, never the string, so `1.10.0` is correctly newer than `1.9.0`.

AN UNREADABLE VERSION IS NOT AN OLD VERSION. If this ever fails to produce a
sane string, running_version() returns "" rather than guessing, and the
updater reads that as "cannot be compared" and offers nothing. The opposite
mistake, treating an unknown as 0.0.0, would offer an update to every install
forever; treating a release's unknown as 0.0.0 would tell an install it is
already ahead and switch updates off for good. Neither is worth risking to
save a version check.
"""

from __future__ import annotations

#: The one place the version is decided. Bump it, then bump `rapid-pdf.iss`
#: and `packaging/version_info.txt` to match (tests/test_version.py checks).
APP_VERSION = "1.5.0"


def running_version() -> str:
    """The version of the build that is running, or "" when it cannot be read.

    Empty is a real answer and callers must treat it as "no comparison is
    possible", never as a very old build. See the module docstring.
    """
    try:
        text = str(APP_VERSION).strip()
    except Exception:  # noqa: BLE001 - a version lookup must never be fatal
        return ""
    return text

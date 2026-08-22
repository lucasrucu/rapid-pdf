"""The version has to say the same thing in all three places it is written.

`core/version.APP_VERSION` is the one the app reads at runtime and the one the
updater compares against GitHub. `rapid-pdf.iss` is what the installer stamps
into Add/Remove Programs, and `packaging/version_info.txt` is what Explorer
shows under Properties. Inno cannot import Python and PyInstaller's version
resource is a literal, so all three are typed by hand on a release.

Drift between them is silent and it matters: a build that thinks it is 1.3.0
while the release is tagged v1.4.0 would offer itself an update it already
has, on every launch. This turns that into a failing test at the moment the
mistake is made.
"""

import re
from pathlib import Path

from core.update.release import parse_version
from core.version import APP_VERSION

ROOT = Path(__file__).resolve().parent.parent


def test_app_version_is_semver():
    assert parse_version(APP_VERSION) is not None, (
        f"APP_VERSION {APP_VERSION!r} is not three integers, so the updater "
        "cannot compare it against a release and would never offer anything"
    )


def test_inno_setup_agrees():
    text = (ROOT / "rapid-pdf.iss").read_text(encoding="utf-8", errors="replace")
    match = re.search(r'#define\s+AppVersion\s+"([^"]+)"', text)
    assert match, "rapid-pdf.iss no longer defines AppVersion"
    assert match.group(1) == APP_VERSION


def test_pyinstaller_version_resource_agrees():
    text = (ROOT / "packaging" / "version_info.txt").read_text(
        encoding="utf-8", errors="replace")
    major, minor, patch = parse_version(APP_VERSION)
    expected_tuple = f"({major}, {minor}, {patch}, 0)"
    expected_string = f"{major}.{minor}.{patch}.0"

    assert f"filevers={expected_tuple}" in text
    assert f"prodvers={expected_tuple}" in text
    assert f"StringStruct('FileVersion', '{expected_string}')" in text
    assert f"StringStruct('ProductVersion', '{expected_string}')" in text

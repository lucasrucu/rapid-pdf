"""The app is called "Rapid PDF", and nothing shipped says more than that.

WHAT WENT WRONG. The Windows version resource carried
"Rapid PDF <dash> fast PDF annotation and page organization" in FileDescription.
FileDescription is not a description as far as the shell is concerned: it is
the NAME Explorer prints in the "Open with" list, in Task Manager's process
row, and in the taskbar tooltip. A whole tagline in that field made the
"Open with" entry run the width of the dialog. ProductName was already right;
only FileDescription was long, and nothing was checking the two agreed.

THE DASH, SEPARATELY, AND WHY GREP MISSED IT TWICE. version_info.txt held a
real em dash behind a UTF-8 BOM, and rapid-pdf.iss held "â€”", the
mojibake you get when UTF-8 bytes are decoded as cp1252 and written back. A
plain repo grep for U+2014 skips the first (the BOM throws the encoding guess)
and does not match the second at all, which is how one string survived several
releases in two files. So the check below reads each file explicitly and looks
for the character AND its double-encoded form.

WHY THE STRINGS ARE READ OUT OF THE BUILD INPUTS. version_info.txt is a literal
consumed by PyInstaller and rapid-pdf.iss is a literal consumed by Inno Setup.
Neither is importable, and neither exists at runtime, so there is nothing to
assert against except the file text. Same reasoning as tests/test_version.py,
which reads all three version literals for the same reason.

A LONGER DESCRIPTION IS STILL ALLOWED, IN THE RIGHT PLACES. The installer's
ApplicationDescription, the About box and the landing site all get a real
sentence, because those have room for one. They are held to the no-dash rule,
not to the name rule.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_INFO = ROOT / "packaging" / "version_info.txt"
ISS = ROOT / "rapid-pdf.iss"
SPEC = ROOT / "rapid-pdf.spec"
MAIN = ROOT / "main.py"
MAIN_WINDOW = ROOT / "ui" / "main_window.py"

#: The whole name, and the only name. Anything that identifies the app to the
#: user is this string exactly, with nothing appended.
APP_NAME = "Rapid PDF"

#: A real em dash, an en dash, and the cp1252 double-encoding of an em dash
#: that two of these files were actually carrying.
BANNED_DASHES = ("—", "–", "â€”")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _version_info_strings() -> dict:
    """Every StringStruct in the Windows version resource, as a dict."""
    text = _read(VERSION_INFO)
    pairs = re.findall(r"StringStruct\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)", text)
    assert pairs, "no StringStruct entries found in packaging/version_info.txt"
    return dict(pairs)


def test_file_description_is_just_the_app_name():
    """The one that shows in "Open with"."""
    value = _version_info_strings()["FileDescription"]
    assert value == APP_NAME, (
        f"FileDescription is {value!r}. Explorer prints this verbatim in the "
        f'"Open with" list and Task Manager, so it has to be {APP_NAME!r} and '
        "nothing else. Put the sentence in the About box instead"
    )


def test_product_name_is_just_the_app_name():
    value = _version_info_strings()["ProductName"]
    assert value == APP_NAME, f"ProductName is {value!r}, expected {APP_NAME!r}"


def test_the_two_name_fields_agree():
    """They drifted once, which is how the long one went unnoticed."""
    strings = _version_info_strings()
    assert strings["FileDescription"] == strings["ProductName"]


def test_installer_app_name_is_just_the_app_name():
    match = re.search(r'#define\s+AppName\s+"([^"]*)"', _read(ISS))
    assert match, "rapid-pdf.iss no longer defines AppName"
    assert match.group(1) == APP_NAME, (
        f"AppName is {match.group(1)!r}. It names the Start-menu shortcut, the "
        "desktop shortcut, the install folder and the Default Apps entry, so a "
        "long one lands in four places at once"
    )


def test_the_app_registers_itself_under_the_app_name():
    assert f'setApplicationName("{APP_NAME}")' in _read(MAIN), (
        f"main.py must call setApplicationName({APP_NAME!r}); QSettings and "
        "the %LOCALAPPDATA% settings folder are both named off it"
    )


def test_the_window_title_is_the_name_then_the_file():
    """No dash character in the separator, and the name still leads."""
    text = _read(MAIN_WINDOW)
    assert f'setWindowTitle(f"{APP_NAME} - {{name}}[*]")' in text, (
        "the titled-window format must be 'Rapid PDF - <file>[*]' with a plain "
        "hyphen; an em dash here reads badly at title-bar size"
    )
    assert f'setWindowTitle("{APP_NAME}")' in text, (
        "with no document open the title is the bare app name"
    )


def test_no_shipped_product_string_carries_a_dash():
    """Every literal these three build inputs put in front of a user.

    Scoped to the build inputs on purpose. The docs under docs/ are prose for
    whoever is reading the repo, not strings Windows or the app ever shows, so
    holding them to this would be noise with no user-visible payoff.
    """
    for path in (VERSION_INFO, ISS, SPEC):
        text = _read(path)
        for dash in BANNED_DASHES:
            assert dash not in text, (
                f"{path.name} contains {dash!r}. Product strings and the "
                "comments around them stay plain ASCII: this file is read by "
                "PyInstaller and Inno Setup, and one of them has already been "
                "saved back as cp1252 mojibake once"
            )


def test_the_about_box_describes_without_naming_a_second_time():
    """A sentence is fine here. The old tagline and any dash are not."""
    text = _read(MAIN_WINDOW)
    assert '"About Rapid PDF"' in text
    assert "fast PDF annotation and page organization" not in text.lower(), (
        "the old FileDescription tagline is gone from the version resource; "
        "it should not reappear anywhere else either"
    )

"""The shell registration has to survive an update, and it has to stay in sync.

TWO DEFECTS, ONE CAUSE, AND THIS IS THE FILE THAT PINS THE FIX.

Up to 1.8.0 the Windows association keys were written by the installer and by
nothing else. That single assumption produced both of the bugs Lucas reported
on 4 Sept 2026:

  The Open With entry vanished. On 2 Sept an Inno uninstall ran by accident
  against a stale uninstall log. Its own uninsdeletekey and uninsdeletevalue
  flags removed Software\\Classes\\RapidPDF.Document and the
  .pdf\\OpenWithProgids\\RapidPDF.Document value, which between them are the
  entire reason RapidPDF appears in the right-click menu. The install folder
  was rebuilt, the app still ran, and the menu entry stayed gone, because
  nothing except setup has ever written those keys.

  The gold icon was never actually fixed for him. The document-icon fix
  shipped in 1.6.0, but he reached 1.6.0 and 1.7.0 through the IN-APP UPDATER,
  which replaces files and writes no registry. His DefaultIcon went on pointing
  at rapid-pdf.exe across two releases that both contained the fix.

So from 1.8.1 the app asserts its own registration on every launch. These tests
pin the list, pin the parts of it that are load-bearing, and pin the agreement
between the Python list and the installer script, because two copies of the
same registry layout is exactly the arrangement that drifts.

WHY THE .iss IS READ AS TEXT. It is a literal consumed by Inno Setup, not
importable and not present at runtime. Same reasoning as tests/test_version.py
and tests/test_product_name.py.
"""

import re
from pathlib import Path

import pytest

from core.shell_registration import (
    DOCUMENT_ICON,
    EXE_NAME,
    FRIENDLY_NAME,
    PROGID,
    desired_entries,
)

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "rapid-pdf.iss"
MAIN = ROOT / "main.py"

INSTALL_DIR = r"C:\Users\example\AppData\Local\Programs\RapidPDF"


def _iss() -> str:
    return ISS.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def entries():
    return desired_entries(INSTALL_DIR)


@pytest.fixture(scope="module")
def by_key(entries):
    out = {}
    for subkey, name, data in entries:
        out[(subkey.lower(), name.lower())] = data
    return out


# ---------------------------------------------------------------------------
# The two values that actually decide whether the entry appears
# ---------------------------------------------------------------------------


def test_progid_is_offered_as_a_pdf_handler(by_key):
    """THIS is what puts RapidPDF in Open With, not the ProgID existing.

    A ProgID can be perfectly formed and completely invisible. The uninstall
    on 2 Sept removed exactly this value, and that alone was enough.
    """
    key = (r"software\classes\.pdf\openwithprogids", PROGID.lower())
    assert key in by_key, (
        "nothing registers RapidPDF.Document under .pdf\\OpenWithProgids. "
        "Without that value the app does not appear in the Open With list, "
        "however complete the rest of the registration is"
    )


def test_the_progid_has_an_open_command_pointing_at_the_exe(by_key):
    command = by_key[(rf"software\classes\{PROGID.lower()}\shell\open\command", "")]
    assert EXE_NAME in command
    assert '"%1"' in command, (
        "the open verb must pass the file path as a quoted %1, or any PDF "
        "with a space in its name opens as several broken arguments"
    )


def test_the_applications_key_is_the_second_route(by_key):
    """So the entry no longer hangs on one value in a shared key."""
    apps = rf"software\classes\applications\{EXE_NAME}"
    assert by_key[(apps, "friendlyappname")] == FRIENDLY_NAME
    assert (f"{apps}\\supportedtypes", ".pdf") in by_key, (
        "SupportedTypes is what lets the shell offer this exe for a .pdf, and "
        "it is also how Explorer resolves the bare exe name it stores in "
        "FileExts\\.pdf\\OpenWithList. Without it that MRU entry is dead text"
    )
    assert EXE_NAME in by_key[(f"{apps}\\shell\\open\\command", "")]


# ---------------------------------------------------------------------------
# The icon, which is the half he raised twice
# ---------------------------------------------------------------------------


def test_default_icon_is_the_document_icon_not_the_app_tile(by_key):
    """Measured, not reasoned about.

    On 4 Sept 2026, SHGetFileInfo against a throwaway extension gave average
    RGB 234,184,67 with the key absent and 234,185,67 pointing at the exe:
    the same gold tile both times. Pointing at pdf-document.ico gave
    223,180,177, a white page with a red band. "Leave it alone" is not a state
    Windows can express, and absent is not neutral.
    """
    value = by_key[(rf"software\classes\{PROGID.lower()}\defaulticon", "")]
    assert DOCUMENT_ICON in value, (
        f"DefaultIcon is {value!r}; it has to be the document icon. This is "
        "the image Explorer stamps on every PDF on the machine once RapidPDF "
        "is the default handler"
    )
    assert EXE_NAME not in value, (
        f"DefaultIcon is {value!r}, which is the app icon again. That is the "
        "1.5.0 bug: choosing RapidPDF turned every PDF gold"
    )


def test_the_combine_verb_keeps_the_app_icon(by_key):
    """The other direction. A menu verb icon names the program, not the file."""
    combine = r"software\classes\systemfileassociations\.pdf\shell\rapidpdf.combine"
    assert by_key[(combine, "icon")].endswith(f"{EXE_NAME},0")
    assert by_key[(combine, "multiselectmodel")] == "Player", (
        "a command-line verb defaults to Document, which the shell hides "
        "entirely past 15 selected files. Combine takes any number"
    )


# ---------------------------------------------------------------------------
# The name in the list
# ---------------------------------------------------------------------------


def test_friendly_app_name_is_set_on_both_registration_routes(by_key):
    """Because the shell would otherwise read a stale per-user cache.

    MuiCache is keyed by exe PATH and holds whatever FileDescription that path
    had when the shell first looked. The 1.8.0 rename shortened the exe
    resource but did not move the exe, so the cache went on serving the old
    tagline. FriendlyAppName outranks it.
    """
    assert by_key[(rf"software\classes\{PROGID.lower()}", "friendlyappname")] == (
        FRIENDLY_NAME
    )
    assert by_key[
        (rf"software\classes\applications\{EXE_NAME}", "friendlyappname")
    ] == FRIENDLY_NAME


def test_nothing_registered_carries_the_old_spaced_name(entries):
    for subkey, name, data in entries:
        for field in (subkey, name, data):
            assert "Rapid PDF" not in field.replace(INSTALL_DIR, ""), (
                f"{field!r} still carries the pre-1.8.0 spelling"
            )


# ---------------------------------------------------------------------------
# The boundary that must not be crossed
# ---------------------------------------------------------------------------


def test_nothing_here_touches_userchoice(entries):
    """Which app opens a PDF is Lucas's decision, not this program's.

    Windows hash protects UserChoice precisely so installers cannot seize a
    file type. This module makes RapidPDF available; choosing it stays a human
    act.
    """
    for subkey, name, data in entries:
        blob = f"{subkey} {name} {data}".lower()
        assert "userchoice" not in blob
        assert "fileexts" not in blob


def test_everything_is_per_user(entries):
    """No key here needs admin, and a per-user install must not write HKLM."""
    for subkey, _name, _data in entries:
        assert not subkey.lower().startswith("hklm")
        assert subkey.lower().startswith(("software\\classes", "software\\"))


# ---------------------------------------------------------------------------
# The installer and the module are two copies of one layout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subkey",
    [
        r"Software\Classes\RapidPDF.Document",
        r"Software\Classes\RapidPDF.Document\DefaultIcon",
        r"Software\Classes\RapidPDF.Document\shell\open\command",
        r"Software\Classes\.pdf\OpenWithProgids",
        r"Software\Classes\Applications\{#AppExeName}",
        r"Software\Classes\Applications\{#AppExeName}\SupportedTypes",
        r"Software\Classes\Applications\{#AppExeName}\shell\open\command",
        # Written through the AppName macro in the .iss, so the literal
        # spelling never appears there. Checked as the macro on purpose: a
        # regression that hard coded the name would still pass a literal
        # check and would then survive the next rename unnoticed.
        r"Software\{#AppName}\Capabilities",
        r"Software\{#AppName}\Capabilities\FileAssociations",
        r"Software\RegisteredApplications",
    ],
)
def test_installer_writes_every_key_the_app_asserts(subkey):
    """A key the app repairs but setup never writes would be invisible on a
    fresh install until the first launch, and vice versa is worse: a key setup
    writes and the app does not know about survives an uninstall it should not.
    """
    assert f'Subkey: "{subkey}"' in _iss(), (
        f"rapid-pdf.iss does not write {subkey}, but "
        "core/shell_registration.py asserts it. The two lists have drifted"
    )


def test_installer_still_carries_the_friendly_app_name():
    assert re.search(
        r'Subkey:\s*"Software\\Classes\\RapidPDF\.Document";[^\n]*'
        r'ValueName:\s*"FriendlyAppName"',
        _iss(),
    ), "the installer must set FriendlyAppName on the ProgID"


def test_installer_never_writes_userchoice():
    text = _iss().lower()
    assert "userchoice" not in text, (
        "an installer that writes UserChoice is hijacking a file type. "
        "Windows hash protects that value for exactly this reason"
    )


# ---------------------------------------------------------------------------
# It has to actually run
# ---------------------------------------------------------------------------


def test_the_app_asserts_registration_on_launch():
    text = MAIN.read_text(encoding="utf-8", errors="replace")
    assert "ensure_registered" in text, (
        "main.py must call ensure_registered(). The whole point is that the "
        "in-app updater never runs setup, so the app is the only thing that "
        "can repair its own registration"
    )
    # After the single-instance election: a multi-select Combine fires one
    # process per file, and they must not race on the same keys.
    assert text.index("claim_primary(") < text.index("ensure_registered()")


def test_registration_is_a_no_op_off_windows_and_unfrozen():
    """Running from source there is no exe for the shell to point at."""
    from core.shell_registration import _install_dir

    assert _install_dir() is None, (
        "_install_dir must return None when not frozen; registering a path "
        "inside a developer checkout would break the moment it moved"
    )

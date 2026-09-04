"""Keep RapidPDF's Windows shell registration true, every time the app starts.

WHY THIS FILE EXISTS. Until 1.8.1 the association keys were written in exactly
one place, the [Registry] section of `rapid-pdf.iss`, which means they were
written by the INSTALLER and by nothing else. Two shipping mechanisms then
walked straight through that assumption:

  The in-app updater. `core/update/swap.py` replaces the files in the install
  folder and restarts the app. It never runs setup, so it never writes a
  registry value. Lucas updated to 1.6.0 on 31 Aug 2026 and to 1.7.0 on 2 Sept
  through the updater, and the document-icon fix that shipped in 1.6.0 sat in
  his install folder without ever reaching his registry. He reported the same
  bug twice against two builds that both contained the fix.

  The uninstaller. Inno's `uninsdeletekey` and `uninsdeletevalue` flags remove
  the ProgID and the `.pdf\\OpenWithProgids` value, which is correct on a real
  uninstall and catastrophic when an uninstall runs by accident. One did, on
  2 Sept 2026, against a stale uninstall log. It took the whole shell
  registration with it, and because the updater does not write registry, no
  amount of updating brought it back. The Open With entry simply stayed gone.

So the registration cannot live only in the installer. It lives here, the app
asserts it on every launch, and the installer's [Registry] section is now a
mirror of this list for the benefit of the very first run.

WHAT THIS DELIBERATELY DOES NOT DO. It never writes
`Explorer\\FileExts\\.pdf\\UserChoice`. That value is hash protected precisely
so installers cannot seize a file type, the hash is undocumented and per user,
and which app opens a PDF is Lucas's decision and not this program's. This
module makes RapidPDF AVAILABLE. Choosing it stays a human act.

EVERYTHING IS HKCU. No key here needs admin rights, and a per-user install has
no business writing to HKLM.

WRITE ONLY WHAT DIFFERS. `ensure_registered` reads before it writes and touches
nothing that is already correct, so the common case is a dozen registry reads
and no writes. That matters because `SHChangeNotify` is only fired when
something actually changed; firing it on every launch would make Explorer
rebuild icons for no reason.
"""

from __future__ import annotations

import os
import sys

from core.version import APP_VERSION  # noqa: F401  (kept for future gating)

#: The ProgID. This string is frozen and must never be renamed again. The app
#: was renamed on the shell surface from "Rapid PDF" to "RapidPDF" in 1.8.0,
#: and the one thing that survived unharmed was this identifier, because it was
#: hard coded rather than derived from the display name. Deriving a ProgID from
#: a display name orphans every association the moment marketing changes its
#: mind.
PROGID = "RapidPDF.Document"

#: What Explorer shows in the Open With list. Without this value the shell
#: falls back to the exe's FileDescription, and, worse, to a per-user cache of
#: it in `Local Settings\\...\\Shell\\MuiCache` that is keyed by exe PATH. The
#: rename did not change the path, so that cache still held the old tagline
#: "Rapid PDF - fast PDF annotation and page organization" long after the exe
#: resource had been shortened to "RapidPDF". Setting FriendlyAppName outranks
#: the cache and makes the displayed name ours.
FRIENDLY_NAME = "RapidPDF"

EXE_NAME = "rapid-pdf.exe"

#: The .pdf FILE icon. See the long argument in `rapid-pdf.iss`; the short
#: version is that "leave the icon alone" is not a state Windows can express.
#: The icon of a file type IS the icon of the ProgID that owns it, and with
#: DefaultIcon absent the shell paints the first icon of the exe, which is the
#: gold app tile Lucas complained about. Measured on 4 Sept 2026 with
#: SHGetFileInfo against a throwaway extension: key absent gives average RGB
#: 234,184,67, pointing at the exe gives 234,185,67, the same tile. Pointing at
#: pdf-document.ico gives 223,180,177, a white page with a red PDF band.
DOCUMENT_ICON = "pdf-document.ico"


def _install_dir() -> str | None:
    """The folder holding the running exe, or None when not frozen.

    Registration is only meaningful for an installed build. Running from source
    there is no exe for the shell to launch, and writing these keys would point
    Explorer at a path that stops existing the moment the checkout moves.
    """
    if not getattr(sys, "frozen", False):
        return None
    return os.path.dirname(os.path.abspath(sys.executable))


def desired_entries(install_dir: str) -> list[tuple[str, str, str]]:
    """Every (subkey, value name, data) this app wants under HKCU.

    A value name of "" means the key's default value. This list is the single
    source of truth; `rapid-pdf.iss` mirrors it, and
    `tests/test_shell_registration.py` asserts the two agree.
    """
    exe = os.path.join(install_dir, EXE_NAME)
    icon = os.path.join(install_dir, DOCUMENT_ICON)
    classes = "Software\\Classes"
    progid = f"{classes}\\{PROGID}"
    apps = f"{classes}\\Applications\\{EXE_NAME}"
    combine = f"{classes}\\SystemFileAssociations\\.pdf\\shell\\RapidPDF.Combine"

    return [
        # The ProgID itself: type name, display name, file icon.
        (progid, "", "PDF Document"),
        (progid, "FriendlyAppName", FRIENDLY_NAME),
        (f"{progid}\\DefaultIcon", "", f"{icon},0"),
        # The open verb. MultiSelectModel=Document is explicit on purpose: an
        # open verb makes one window per file, so the shell's 15 item cap is
        # the right cap.
        (f"{progid}\\shell\\open", "", f"Open with {FRIENDLY_NAME}"),
        (f"{progid}\\shell\\open", "MultiSelectModel", "Document"),
        (f"{progid}\\shell\\open\\command", "", f'"{exe}" "%1"'),
        # Offer the ProgID as a handler for .pdf. THIS value, not the
        # existence of the ProgID, is what puts RapidPDF in the Open With
        # list. A ProgID can be perfectly formed and completely invisible.
        (f"{classes}\\.pdf\\OpenWithProgids", PROGID, ""),
        # The Applications key. A second, independent route into the Open With
        # list and into the "Choose another app" dialog, so the entry no
        # longer hangs on the single OpenWithProgids value above being intact.
        # SupportedTypes is what makes the app offer itself for .pdf here.
        (apps, "FriendlyAppName", FRIENDLY_NAME),
        (f"{apps}\\SupportedTypes", ".pdf", ""),
        (f"{apps}\\shell\\open\\command", "", f'"{exe}" "%1"'),
        # The Combine verb, registered under SystemFileAssociations so it
        # shows for every PDF whoever owns the extension. Its icon stays the
        # APP tile, and should: a context menu icon says which program is
        # about to run, not what the file is.
        (combine, "MUIVerb", f"Combine with {FRIENDLY_NAME}"),
        (combine, "Icon", f"{exe},0"),
        (combine, "MultiSelectModel", "Player"),
        (f"{combine}\\command", "", f'"{exe}" --combine "%1"'),
        # Default Programs registration, so RapidPDF is listed in
        # Settings > Default apps and Lucas CAN pick it. Listed, never forced.
        (
            "Software\\RapidPDF\\Capabilities",
            "ApplicationName",
            FRIENDLY_NAME,
        ),
        (
            "Software\\RapidPDF\\Capabilities",
            "ApplicationDescription",
            "Fast PDF page management and markup",
        ),
        (
            "Software\\RapidPDF\\Capabilities\\FileAssociations",
            ".pdf",
            PROGID,
        ),
        (
            "Software\\RegisteredApplications",
            FRIENDLY_NAME,
            "Software\\RapidPDF\\Capabilities",
        ),
    ]


def _clear_stale_mui_cache(winreg, exe: str) -> int:
    """Drop a cached display name for our exe that is not our name any more.

    MuiCache is keyed by exe path and holds whatever FileDescription that path
    had the first time the shell looked. The 1.8.0 rename shortened the
    resource but left the path alone, so the cache kept serving the old
    tagline. Deleting the value is safe: the shell rebuilds it from the exe on
    next use. This is a display cache, nothing depends on it.
    """
    path = (
        "Software\\Classes\\Local Settings\\Software\\Microsoft"
        "\\Windows\\Shell\\MuiCache"
    )
    removed = 0
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS
        ) as key:
            name = f"{exe}.FriendlyAppName"
            try:
                current, _ = winreg.QueryValueEx(key, name)
            except OSError:
                return 0
            if str(current) != FRIENDLY_NAME:
                winreg.DeleteValue(key, name)
                removed = 1
    except OSError:
        return 0
    return removed


def ensure_registered() -> int:
    """Assert the shell registration. Returns how many values were written.

    Never raises. A PDF viewer that refuses to start because a registry write
    failed would be trading a cosmetic problem for a fatal one.
    """
    if os.name != "nt":
        return 0
    install_dir = _install_dir()
    if install_dir is None:
        return 0

    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows only
        return 0

    # A build that is missing its document icon must not register a
    # DefaultIcon pointing at nothing: an icon path that does not resolve is
    # how a file type ends up blank. Better to leave whatever is already
    # there, which at worst is the previous install's working value.
    if not os.path.isfile(os.path.join(install_dir, DOCUMENT_ICON)):
        return 0

    written = 0
    for subkey, name, data in desired_entries(install_dir):
        try:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ
                ) as key:
                    current, kind = winreg.QueryValueEx(key, name)
                if kind == winreg.REG_SZ and str(current) == data:
                    continue
            except OSError:
                pass  # missing key or value; fall through and write it
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_WRITE
            ) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)
            written += 1
        except OSError:
            continue  # one bad value must not abort the rest

    written += _clear_stale_mui_cache(
        winreg, os.path.join(install_dir, EXE_NAME)
    )

    if written:
        _notify_shell()
    return written


def _notify_shell() -> None:
    """Tell Explorer the associations moved.

    Explorer caches a file type's icon for the life of the session and past a
    reboot out of iconcache_*.db. Without this the registry is right and the
    screen is wrong, which reads to a user as "still broken".
    """
    try:
        import ctypes

        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None
        )
    except Exception:  # noqa: BLE001 - never fatal
        pass

"""The app's half: is there a newer release, and can it be put on disk safely.

TWO STEPS, WITH OPPOSITE ERROR RULES. That difference is the design of this
file.

  check()  NEVER RAISES. It runs unasked, in the background, on every launch.
           No network, a captive portal at a hotel, a rate limit, GitHub down,
           a release published without its payload: every one of those means
           there is no update to offer, so they all return None and nothing
           appears on screen.

  stage()  RAISES, loudly. It runs because a person pressed Update and is
           watching a progress bar. A digest that does not match means the
           bytes that arrived are not the bytes GitHub published, and the only
           safe thing to do with those is stop, delete them, and say so.

WHAT STAGE DOES NOT DO: touch the install. Not one byte. It downloads beside
the install and verifies every byte against the release's own sha256, and only
then hands the result to installer.py, which is the part that needs the app to
have exited. Up to the moment the installer runs, giving up costs nothing but a
folder.

WHY THE SETUP EXE AND NOT THE PORTABLE ZIP. This is the reverse of what this
file said up to 1.9.0, and the reason it changed is worth reading before it is
changed back.

The zip was chosen because it IS the install folder, so applying it was a file
swap this code could verify itself, and because an unsigned setup.exe fetched
by a browser carries a mark-of-the-web that puts SmartScreen in front of it.
Both of those were true. What they missed is that the file swap had to be
carried out by something that outlives the app, and everything capable of that
on Windows, chained the way a swap needs, reads as a dropper to a behavioural
engine. Sophos convicted this repo's own test suite for it on 9 September 2026.
See the docstring at the top of core/update/installer.py.

Point by point, on the two reasons that were right at the time:

  * MARK-OF-THE-WEB IS A BROWSER'S DOING, not the network's. The Zone.Identifier
    stream is written by the program that fetches the file, and urllib does not
    write one, so a setup.exe downloaded HERE has no mark and SmartScreen has
    nothing to gate on. Note what is NOT being done about this: nothing strips
    a mark, ever. Deleting a Zone.Identifier is itself a catalogued evasion
    (T1553.005) and would trade one conviction for a worse one. This code just
    never creates one.
  * THE INSTALLER REALLY WOULD NOT UPDATE A PORTABLE COPY. That objection
    stands, completely, and it is why a portable copy is no longer offered a
    self-update at all. See install_kind() below and installer.py's docstring.

What the installer does that a swap never did: it maintains the Start-menu
entry, the uninstall registration and the whole [Registry] section, so an
updated install stops drifting away from a freshly installed one. Two bugs in
the 1.8.x series were exactly that drift.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from core.update import release as release_mod
from core.update.feed import FeedUnavailable, GitHubReleases
from core.update.release import Asset, Release, human_size
from core.version import running_version as _running_version

#: The staging folder is named beside the install, never inside it: a folder
#: inside would be sitting in the tree Setup is about to replace.
STAGING_SUFFIX = ".update"

#: The exe an install is built around. Only a frozen build has one.
EXE_NAME = "rapid-pdf.exe"

#: The update's log, beside the exe. Inno writes it (see installer.py), and it
#: is named here because both halves need to agree on where it goes.
LOG_NAME = "update.log"

#: Read size for the download and for hashing. The asset is tens of MB, and the
#: default 64 KB turns that into a syscall benchmark rather than a disk one.
CHUNK = 1 << 20

#: The floor on how big the downloaded installer has to be before it is
#: believed to be an installer at all.
#:
#: A real rapid-pdf setup exe is around 50 MB: the whole PyInstaller onedir
#: tree, lzma2 compressed. The floor is set far below that on purpose, for the
#: same reason the old payload file-count floor was. This is a sanity check,
#: not a manifest. GitHub's digest proves the bytes are the ones that were
#: uploaded; it says nothing about whether what was uploaded is a build. A
#: placeholder file, a wrong upload or a stub named rapid-pdf-setup-X.Y.Z.exe
#: would hash perfectly and still not be an installer. A megabyte is
#: comfortably under anything that could carry PySide6 and comfortably over
#: anything that could not.
MIN_INSTALLER_BYTES = 1 << 20

#: A PE starts with these two bytes and everything Windows will execute as a
#: program is a PE. Checked because what happens next is running the file.
PE_MAGIC = b"MZ"

#: What an install of this app is, as far as updating goes.
INSTALLED = "installed"     # made by setup, and updatable by setup
PORTABLE = "portable"       # a folder somebody unzipped, updated by hand
SOURCE = "source"           # not frozen at all, so there is nothing to update

#: The AppId from rapid-pdf.iss, and the key Inno names after it. Typed out
#: rather than read: the .iss is a build input, it is not shipped, and there is
#: nothing to parse at run time. It must stay in step with rapid-pdf.iss, and
#: tests/test_update.py reads both files to say so.
APP_ID = "{A7E3C9F1-4B2D-4E6A-9C8F-1D5B7A0E3F42}"
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 rf"\{APP_ID}_is1")


class UpdateError(Exception):
    """The update stopped, and the install was not touched."""


@dataclass(frozen=True)
class UpdateInfo:
    """A release that is newer than the build doing the asking."""

    release: Release
    running: str

    @property
    def version(self) -> str:
        return self.release.version

    @property
    def asset(self) -> Asset:
        return self.release.asset

    def headline(self) -> str:
        """One line for the notice."""
        return (f"Rapid PDF {self.version} is available "
                f"({human_size(self.asset.size)}). "
                f"You are on {self.running or 'an unknown build'}.")


@dataclass(frozen=True)
class StagedInstaller:
    """A verified copy of the release's installer, ready to be run.

    `installer_bytes` is measured off the file on disk after the download, not
    taken from the release JSON, for the same reason the old payload numbers
    were measured rather than trusted: what matters is what is actually there.
    """

    info: UpdateInfo
    install_dir: Path
    staging_dir: Path
    installer_path: Path
    installer_bytes: int

    def discard(self) -> None:
        """Throw the staging folder away. Safe to call twice."""
        shutil.rmtree(self.staging_dir, ignore_errors=True)


def install_dir() -> Path | None:
    """The folder holding the running exe, or None when running from source.

    None is not a failure, it is the everyday development case: there is no
    install to update. The UI turns that into "open the release page" instead
    of "update now".
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def installed_location() -> Path | None:
    """Where Inno Setup says RapidPDF is installed, or None if it says nothing.

    Read out of Inno's own uninstall registration, which is written by setup
    and by nothing else. That is what makes it a trustworthy answer to "was
    this copy installed": a portable copy cannot have one, because nothing
    ever ran setup for it.

    THE ALTERNATIVE, AND WHY IT DOES NOT WORK. The obvious approach is a marker
    file in the portable zip. It cannot be used here, because 1.9.0 and earlier
    update by laying the zip's CONTENTS over the install folder, so an INSTALLED
    copy that reaches 2.0.0 through the old updater would have the marker in it
    and would look portable for the rest of its life. Detecting installed
    POSITIVELY, from a key the old updater never wrote, has no such hole.

    None on anything unexpected: not Windows, no winreg, key absent, value
    absent, value empty. Every one of those means "cannot show this was
    installed", which install_kind() reads as portable, and portable is the
    answer that never runs an installer.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:      # pragma: no cover - Windows always has it
        return None
    # HKCU first: PrivilegesRequired=lowest means that is where a normal
    # install registers. HKLM is read after it for a per-machine install, in
    # both registry views, because which one Inno wrote depends on how it was
    # compiled and reading the wrong one would report "portable" for a machine
    # that is anything but.
    places = [
        (winreg.HKEY_CURRENT_USER, 0),
        (winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_64KEY", 0)),
        (winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_32KEY", 0)),
    ]
    for root, view in places:
        try:
            with winreg.OpenKey(root, UNINSTALL_KEY, 0,
                                winreg.KEY_READ | view) as key:
                value = winreg.QueryValueEx(key, "InstallLocation")[0]
        except OSError:
            continue
        # Inno writes this with a trailing backslash, and quotes turn up in
        # hand-edited registries. Path() copes with the first, not the second.
        text = str(value or "").strip().strip('"')
        if text:
            return Path(text)
    return None


def same_folder(left: Path | None, right: Path | None) -> bool:
    """Do these two paths name the same folder, as Windows sees it.

    Case-insensitive and trailing-separator-insensitive, because the registry
    value and sys.executable are written by different programs and agree on
    neither.
    """
    if left is None or right is None:
        return False
    try:
        a = os.path.normcase(os.path.normpath(str(Path(left).resolve())))
        b = os.path.normcase(os.path.normpath(str(Path(right).resolve())))
    except OSError:
        return False
    return a == b


def install_kind(target: Path | None = None) -> str:
    """INSTALLED, PORTABLE or SOURCE. What kind of copy is running.

    Only INSTALLED gets a self-update, and it gets it by running the same
    installer a person would run by hand. PORTABLE is sent to the release page
    to fetch the zip, because running the installer against a portable folder
    would build a second install somewhere else and leave this one stale.
    SOURCE has no exe at all.

    PORTABLE IS THE FALLBACK FOR EVERY DOUBT, deliberately. Being wrong towards
    portable costs a manual download. Being wrong towards installed runs an
    installer against a folder it does not own.
    """
    target = install_dir() if target is None else Path(target)
    if target is None:
        return SOURCE
    return INSTALLED if same_folder(installed_location(), target) else PORTABLE


def staging_dir_for(target: Path) -> Path:
    """Where a staged download goes for a given install.

    BESIDE THE INSTALL, still, though for a smaller reason than it used to be.
    The old swap needed staging on the same volume so every move was a rename;
    nothing renames anything now. What is left is that the installer is a large
    file which should land on the same disk the install is on rather than
    filling C: for a copy running off a stick, and that a folder next to the
    install is somewhere a person can find and delete.
    """
    target = Path(target)
    return target.parent / f"{target.name}{STAGING_SUFFIX}"


def log_path(target: Path) -> Path:
    """The update log, beside the exe.

    Beside the exe because that is where somebody looks when the app did not
    come back, and the app is not running to show it anywhere else.
    """
    return Path(target) / LOG_NAME


def check(current_version: str | None = None, feed=None) -> UpdateInfo | None:
    """Is GitHub offering a build newer than this one. None if not, ever.

    `current_version` of None means "you did not tell me", which falls back to
    core.version.APP_VERSION. That is different from "" or "unknown", which
    mean the version could not be read at all and offer nothing.

    RETURNS NONE, NEVER RAISES, and the list of reasons is deliberately long
    because every one of them is a state a laptop is actually in:

      * no network, DNS not resolving, a proxy or captive portal in the way,
      * GitHub rate limiting this address, or having an outage,
      * the repo has no releases, or the newest one is a draft,
      * the newest release was published without its setup exe,
      * the asset publishes no sha256, so nothing could verify a download,
      * either side's version cannot be read, so there is nothing to compare,
      * the release is the same as this build, or older.

    The last two are the ones worth being careful about. An unreadable version
    is not an old one: see release.parse_version. Equal is not newer either,
    so re-publishing a release never nags anybody.
    """
    try:
        source = feed if feed is not None else GitHubReleases()
        current = current_version if current_version is not None else _running_version()
        latest = release_mod.parse_latest(source.latest_release())
        if not release_mod.is_newer(latest.version, current):
            return None
        return UpdateInfo(release=latest, running=str(current or ""))
    except Exception:  # noqa: BLE001 - see the docstring, this is the contract
        return None


def stage(info: UpdateInfo, target: Path, feed=None,
          progress=None) -> StagedInstaller:
    """Download and verify the release's installer beside the install.

    `progress(done_bytes, total_bytes, phase)` is called as the download runs
    and once when the checking starts. Tens of MB over a site link is why there
    is a bar at all.

    THE DOWNLOAD IS HASHED AS IT LANDS, against the digest GitHub publishes for
    the asset, and a mismatch deletes everything staged and raises. What is
    downloaded here is a program that is about to be RUN, which makes this the
    single most important check in the whole updater. Half an update is not a
    smaller update, it is a broken app, and staging exists so that there is a
    moment where stopping is free. This is it.

    Raises UpdateError for anything that stops it. Nothing it raises can leave
    the install different from how it found it, because it never writes there.
    """
    target = Path(target)
    source = feed if feed is not None else GitHubReleases()
    staging = staging_dir_for(target)
    asset = info.asset

    # A staging folder left by an abandoned update is stale by definition: it
    # was built against a different release. Cleared rather than reused,
    # because reusing it would mean trusting a file nothing has checked since.
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateError(
            f"The update could not be prepared: {staging} cannot be created "
            f"({exc.strerror or exc}). Nothing has been changed."
        ) from exc

    setup = staging / asset.name
    try:
        digest = _download(source, asset, setup, progress)
        _verify(asset, setup, digest)
        if progress is not None:
            progress(asset.size, asset.size, "checking")
        size = _check_installer(setup)
    except UpdateError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except FeedUnavailable as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(
            f"The download stopped: {exc}. Nothing has been changed."
        ) from exc
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(
            f"The update stopped on a file error: {exc.strerror or exc}. "
            "Nothing has been changed."
        ) from exc

    return StagedInstaller(
        info=info, install_dir=target, staging_dir=staging,
        installer_path=setup, installer_bytes=size,
    )


def _download(source, asset: Asset, target: Path, progress) -> str:
    """Stream the asset to disk and return its sha256, hashed as it lands.

    Written through a `.part` and renamed, so a transfer that dies mid-file
    leaves a stray part-file rather than a short file under the real name.
    Hashed on the way past rather than in a second pass: it is tens of MB, and
    reading it twice to learn something the first read already knew is a
    minute of somebody's day.
    """
    tmp = target.with_name(target.name + ".part")
    sha = hashlib.sha256()
    done = 0
    try:
        with source.open_asset(asset.url) as stream, \
                open(tmp, "wb", buffering=0) as out:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                sha.update(chunk)
                out.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, asset.size, "downloading")
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return sha.hexdigest()


def _verify(asset: Asset, archive: Path, digest: str) -> None:
    """Check the download against the release, or stop the whole update."""
    size = archive.stat().st_size
    if size == asset.size and digest == asset.sha256:
        return
    raise UpdateError(
        f"The update stopped: {asset.name} is not the file GitHub published.\n"
        f"  expected {asset.size} bytes, sha256 {asset.sha256[:16]}...\n"
        f"  got      {size} bytes, sha256 {digest[:16]}...\n"
        "\n"
        "That usually means the download was cut short or something on the "
        "network altered it. Nothing has been changed and this install is "
        "exactly as it was. Try again in a few minutes."
    )


def _check_installer(setup: Path) -> int:
    """Is the verified download actually a program. Returns its size.

    A THIRD CLAIM, AND IT IS NOT THE SAME AS THE OTHER TWO. The digest says the
    bytes are the ones GitHub stored, and the size check says the transfer was
    complete. Neither says the file is an installer. The zip path had three
    shape checks for exactly this reason, and dropping them because the file
    is now an exe rather than an archive would be dropping the lesson: a
    release published with a placeholder, or with the wrong file attached under
    the right name, hashes perfectly and is not a build.

    This is the LAST place an update can be stopped for free. After it, the
    file gets run.
    """
    size = setup.stat().st_size
    if size < MIN_INSTALLER_BYTES:
        raise UpdateError(
            f"The update stopped: {setup.name} is {human_size(size)}, and a "
            f"Rapid PDF installer is around 50 MB. Something that small "
            "cannot be a whole build, and it is not going to be run. "
            "Nothing has been changed."
        )
    with open(setup, "rb") as handle:
        magic = handle.read(len(PE_MAGIC))
    if magic != PE_MAGIC:
        raise UpdateError(
            f"The update stopped: {setup.name} does not start like a Windows "
            "program, so whatever was published under that name is not an "
            "installer. Nothing has been changed."
        )
    return size

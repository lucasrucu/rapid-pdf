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
the install, verifies the whole archive against the release's own sha256,
unpacks it, and only then hands the result to swap.py, which is the part that
needs the app to have exited. Up to the moment the swap runs, giving up costs
nothing but a folder.

WHY THE PORTABLE ZIP AND NOT THE SETUP EXE. Both are published on every
release, and the zip is the one a self-update can take:

  * The exe is not code-signed. A setup.exe downloaded over HTTPS gets a
    mark-of-the-web, and SmartScreen puts "Windows protected your PC" in front
    of an unsigned one. That is an unskippable two-click detour ("More info",
    "Run anyway") in the middle of an update the user has already agreed to,
    and it is exactly the kind of prompt that teaches people to click through
    warnings. The zip is data: nothing executes it, so nothing screens it, and
    the files this code writes itself carry no mark.
  * The zip IS the install. It holds `rapid-pdf/` with the exe and _internal/
    exactly as they sit in an install folder, so applying it is a file swap
    this code can verify first. Running an installer means handing the whole
    outcome to a wizard and finding out afterwards.
  * Running the installer would not update a portable install, it would create
    a SECOND one. Inno puts it in %LocalAppData%\\Programs\\Rapid PDF whatever
    folder the running copy is in, and the user would be left with a stale copy
    where their shortcut points. A file swap updates whichever install is
    actually running, which is the only one that matters.
  * The zip needs no elevation either way. Inno runs PrivilegesRequired=lowest
    so an installed copy is already under %LocalAppData% and writable, and a
    portable copy is wherever the user unzipped it. A swap in place is
    therefore no more privileged than the app already is.

The one thing the installer does that this does not is maintain the Start-menu
entry and the uninstall registration, and a swap leaves both pointing at the
same folder, so both keep working.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from core.update import release as release_mod
from core.update.feed import FeedUnavailable, GitHubReleases
from core.update.release import Asset, Release, human_size
from core.version import running_version as _running_version

#: The staging folder is named beside the install, never inside it: a folder
#: inside would end up in the tree the swap moves files into.
STAGING_SUFFIX = ".update"

#: The exe an install is built around. Only a frozen build has one to swap.
EXE_NAME = "rapid-pdf.exe"

#: The folder PyInstaller puts a onedir build's runtime in, beside the exe.
#: An install without it is an exe with nothing to run against: it starts, it
#: fails to find python3xx.dll, and it dies before it can say so.
INTERNAL_DIR = "_internal"

#: The floor on how many files an archive has to hold before it is believed to
#: be a build at all.
#:
#: A real portable zip unpacks to about 252 files: the exe, _internal with the
#: Python runtime and the PySide6 and Qt DLLs, the Qt plugin folders, and the
#: bundled tessdata. The floor is set far below that, on purpose. This is a
#: sanity check, not a manifest. It exists to reject an archive that unpacked
#: to a handful of files, which is what a truncated build, a zip of the wrong
#: thing, or a release published half-uploaded looks like, WITHOUT rejecting a
#: legitimately slimmer future build that drops a language pack or stops
#: bundling tessdata. Fifty is comfortably under any build that could start
#: PySide6 at all and comfortably over anything that could not.
MIN_PAYLOAD_FILES = 50

#: Read size for the download and for hashing. The asset is 67 MB, and the
#: default 64 KB turns that into a syscall benchmark rather than a disk one.
CHUNK = 1 << 20

#: A ceiling on what the archive is allowed to expand to, as a multiple of its
#: own size. The real ratio is about 3. A zip bomb is not the threat model
#: here (the digest is published by GitHub for a file GitHub stores), but an
#: unpack with no ceiling at all is a disk nobody meant to fill.
MAX_EXPANSION = 20


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
class StagedUpdate:
    """A verified copy of the new build, unpacked and ready to be swapped in.

    THE THREE NUMBERS ARE NOT STATISTICS. swap.build_script writes file_count
    and exe_bytes into the batch file, and that file refuses to call an update
    finished unless the install ends up holding at least file_count files and
    an exe of exactly exe_bytes bytes. They are the only thing standing between
    a robocopy that moved almost nothing and a log line claiming it worked:
    that is not hypothetical, it is the 34 file install this check was written
    for. Do not let them go stale, and do not let them go unread.
    """

    info: UpdateInfo
    install_dir: Path
    staging_dir: Path
    payload_dir: Path
    file_count: int
    payload_bytes: int
    exe_bytes: int

    def discard(self) -> None:
        """Throw the staging folder away. Safe to call twice."""
        shutil.rmtree(self.staging_dir, ignore_errors=True)


def install_dir() -> Path | None:
    """The folder holding the running exe, or None when running from source.

    None is not a failure, it is the everyday development case: there is no
    exe to swap, so there is nothing for swap.py to do. The UI turns that into
    "open the release page" instead of "update now".
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def staging_dir_for(target: Path) -> Path:
    """Where a staged update goes for a given install.

    BESIDE THE INSTALL, AND THAT IS LOAD BEARING. swap.py moves the staged
    files into place, and a move is only a rename (instant, and unable to
    leave a half-written file under a real name) when both ends are on the
    same volume. Staging in %TEMP% would put the payload on C: while the
    install can be on a USB stick, and every move would silently become a copy.
    """
    target = Path(target)
    return target.parent / f"{target.name}{STAGING_SUFFIX}"


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
      * the newest release was published without its portable zip,
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


def stage(info: UpdateInfo, target: Path, feed=None, progress=None) -> StagedUpdate:
    """Download, verify and unpack the release beside the install.

    `progress(done_bytes, total_bytes, phase)` is called as the download runs
    and once when the unpack starts. 67 MB over a site link is why there is a
    bar at all.

    THE ARCHIVE IS HASHED BEFORE IT IS OPENED, against the digest GitHub
    publishes for the asset. A mismatch deletes everything staged and raises.
    Half an update is not a smaller update, it is a broken app, and staging
    exists so that there is a moment where stopping is free. This is it.

    Raises UpdateError for anything that stops it. Nothing it raises can leave
    the install different from how it found it, because it never writes there.
    """
    target = Path(target)
    source = feed if feed is not None else GitHubReleases()
    staging = staging_dir_for(target)
    asset = info.asset

    # A staging folder left by an abandoned update is stale by definition: it
    # was built against a different release. Cleared rather than reused,
    # because reusing it would mean trusting files nothing has checked since.
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateError(
            f"The update could not be prepared: {staging} cannot be created "
            f"({exc.strerror or exc}). Nothing has been changed."
        ) from exc

    archive = staging / asset.name
    payload = staging / "payload"
    try:
        digest = _download(source, asset, archive, progress)
        _verify(asset, archive, digest)
        if progress is not None:
            progress(asset.size, asset.size, "unpacking")
        count, size = _unpack(archive, payload, asset.size)
        # Measured here, from the file on disk, and not taken from the zip's
        # own header: what the swap has to match is what was actually written.
        exe_bytes = (payload / EXE_NAME).stat().st_size
        archive.unlink(missing_ok=True)
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

    return StagedUpdate(
        info=info, install_dir=target, staging_dir=staging,
        payload_dir=payload, file_count=count, payload_bytes=size,
        exe_bytes=exe_bytes,
    )


def _download(source, asset: Asset, target: Path, progress) -> str:
    """Stream the asset to disk and return its sha256, hashed as it lands.

    Written through a `.part` and renamed, so a transfer that dies mid-file
    leaves a stray part-file rather than a short file under the real name.
    Hashed on the way past rather than in a second pass: it is 67 MB, and
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


def _unpack(archive: Path, payload: Path, archive_size: int) -> tuple[int, int]:
    """Unpack the verified archive into `payload`, returning (files, bytes).

    THE TOP-LEVEL FOLDER IS STRIPPED. The published zip holds everything under
    `rapid-pdf/`, because it is the PyInstaller onedir folder zipped whole, and
    what the swap needs is the CONTENTS of that folder laid over an install.
    Stripping is conditional: if a future zip is ever published flat, the
    entries are taken as they are rather than losing their first path segment.

    EVERY NAME IS CHECKED before anything is written, even though the archive
    has already been verified against GitHub's digest. The digest proves the
    bytes are the published ones; it says nothing about whether the published
    ones contain `..\\..\\Windows\\System32\\something`. Those are different
    claims and only one of them is about trust.

    AND WHAT CAME OUT IS CHECKED FOR SHAPE, which is a third claim again. The
    digest says the bytes are the published ones and the name check says they
    landed where they should; neither says the archive is a Rapid PDF build.
    For a long time the only shape check here was that a file called
    rapid-pdf.exe existed, so a zip holding exactly that one file staged
    happily and the swap then laid it over a working install. The three checks
    at the bottom are the cheapest place in the whole update to stop: nothing
    outside the staging folder has been touched yet, so giving up costs a
    folder.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            entries = [item for item in zf.infolist() if not item.is_dir()]
            if not entries:
                raise UpdateError(
                    "The update stopped: the downloaded archive is empty. "
                    "Nothing has been changed."
                )

            total = sum(item.file_size for item in entries)
            if total > archive_size * MAX_EXPANSION:
                raise UpdateError(
                    f"The update stopped: the archive claims to unpack to "
                    f"{human_size(total)} from {human_size(archive_size)}, "
                    "which is not what a Rapid PDF release looks like. "
                    "Nothing has been changed."
                )

            prefix = _common_prefix([item.filename for item in entries])
            payload.mkdir(parents=True, exist_ok=True)
            root = payload.resolve()

            for item in entries:
                rel = _safe_relative(item.filename, prefix)
                target = (payload / rel)
                # The belt to the braces of _safe_relative: whatever the name
                # was, the resolved path has to land inside the payload.
                if not str(target.resolve()).startswith(str(root)):
                    raise UpdateError(
                        f"The update stopped: the archive contains an entry "
                        f"that would be written outside the update folder "
                        f"({item.filename!r}). Nothing has been changed."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(item) as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out, CHUNK)

            if not (payload / EXE_NAME).is_file():
                raise UpdateError(
                    f"The update stopped: the downloaded archive has no "
                    f"{EXE_NAME} in it, so it is not a Rapid PDF build. "
                    "Nothing has been changed."
                )
            if not (payload / INTERNAL_DIR).is_dir():
                raise UpdateError(
                    f"The update stopped: the downloaded archive has "
                    f"{EXE_NAME} but no {INTERNAL_DIR} folder beside it, and "
                    f"an exe with no {INTERNAL_DIR} cannot start. It is not a "
                    "complete Rapid PDF build. Nothing has been changed."
                )
            if len(entries) < MIN_PAYLOAD_FILES:
                raise UpdateError(
                    f"The update stopped: the downloaded archive holds "
                    f"{len(entries)} files, and a Rapid PDF build is around "
                    f"250. Something that small cannot be a whole build, and "
                    f"laying it over this install would leave an app that "
                    "does not start. Nothing has been changed."
                )
            return len(entries), total
    except zipfile.BadZipFile as exc:
        raise UpdateError(
            f"The update stopped: the downloaded archive could not be opened "
            f"({exc}). Nothing has been changed."
        ) from exc


def _common_prefix(names: list[str]) -> str:
    """The single top-level folder every entry sits under, or "" if there isn't one."""
    tops = {name.replace("\\", "/").split("/", 1)[0] for name in names}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    if not top or top in (".", ".."):
        return ""
    # Only a prefix if every entry actually has something after it, otherwise
    # a flat archive of one file would lose its only path segment.
    if all("/" in name.replace("\\", "/") for name in names):
        return top
    return ""


def _safe_relative(name: str, prefix: str) -> Path:
    """The archive entry's path inside the payload, or raise.

    Absolute paths, drive letters and any `..` segment are refused rather than
    sanitised: a name that needs rewriting to be safe is a name this code does
    not understand, and quietly repairing it would hide that.
    """
    rel = name.replace("\\", "/").lstrip("/")
    if prefix and rel.startswith(prefix + "/"):
        rel = rel[len(prefix) + 1:]
    if not rel:
        raise UpdateError(
            f"The update stopped: the archive contains an unusable entry "
            f"({name!r}). Nothing has been changed."
        )
    if len(rel) >= 2 and rel[1] == ":":
        raise UpdateError(
            f"The update stopped: the archive contains an absolute path "
            f"({name!r}). Nothing has been changed."
        )
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UpdateError(
            f"The update stopped: the archive contains an entry that points "
            f"outside itself ({name!r}). Nothing has been changed."
        )
    return Path(*parts)

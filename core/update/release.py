"""What a GitHub release is to this app, and which of two versions is newer.

THE COMPARISON IS THE POINT OF THIS MODULE and it must not be clever. The app
versions itself with semver (`core/version.APP_VERSION`), the tags are
`vX.Y.Z`, and the release titles read "Rapid PDF X.Y.Z". So the comparison is
on THREE INTEGERS, as a tuple, never on the string:

    "1.10.0" < "1.9.0"      as strings, and that is wrong
    (1, 10, 0) > (1, 9, 0)  as tuples, and that is right

That gap is not hypothetical for a project already at 1.3.0. The first time a
minor number reaches double digits, a string comparison silently stops
offering updates and nobody finds out.

AN UNPARSEABLE VERSION IS NOT AN OLD VERSION. parse_version() returns None for
an empty string, for "unknown", for "1.3", for "1.3.0.1", for a prerelease
suffix, and for anything else that is not exactly three integers. None means
"this side cannot be compared", and is_newer() answers False whenever either
side is None. The temptation is to read an unknown as 0.0.0 so that "at least
something happens", and it is the single worst thing this code could do:

  * an unknown RUNNING version read as 0.0.0 offers an update to every
    install, on every launch, forever,
  * an unknown RELEASE version read as 0.0.0 tells every install it is already
    ahead, and self-update quietly stops working for good, with no error
    anywhere.

Refusing to guess costs one skipped update and nothing else.

WHY THE RELEASE PARSE IS STRICT. Everything checked here is trusted later:
client.stage() writes the asset to disk and swap.py moves it into a working
install. A release that is half readable is the worst outcome, because it
would download a payload and verify it against nothing. So a release with no
usable digest is not a release this app will install, and it is refused here
rather than being downloaded and hoped about. client.check() turns every raise
into "no update", so strictness costs nothing at run time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: Exactly three integers, with an optional `v`. Nothing else, and in
#: particular no fourth component and no `-rc1` tail: a version this cannot
#: read is a version it declines to compare rather than one it guesses at.
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

#: The asset a self-update takes. See client.py for why it is the portable zip
#: and not the setup exe.
PORTABLE_SUFFIX = "-portable.zip"

#: A digest as the Releases API publishes it: "sha256:" and 64 hex characters.
_DIGEST = re.compile(r"^sha256:(?P<hex>[0-9a-f]{64})$", re.IGNORECASE)

#: A release body is shown in a banner, not rendered. Anything past this is a
#: changelog nobody reads off a one-line notice.
NOTES_LIMIT = 2000


class ReleaseError(Exception):
    """GitHub answered, but what it said is not a release this can install."""


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """(major, minor, patch), or None when there is no version to compare.

    None for None, for "", for "unknown", for "1.3", for "1.3.0.1", for
    "1.3.0-rc1" and for anything else off the shape. None is a real answer and
    callers must treat it as "cannot compare", never as zero. See the module
    docstring for what treating it as zero would cost.
    """
    if text is None:
        return None
    match = _SEMVER.match(str(text).strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer(candidate: str | None, running: str | None) -> bool:
    """True only when both versions parse AND the candidate is higher.

    Equal is not newer, so republishing a release never nags anybody. Lower is
    not newer either, so an accidental re-tag of an old build cannot walk an
    install backwards. Unknown on either side is not newer, because it is not
    anything.
    """
    left, right = parse_version(candidate), parse_version(running)
    if left is None or right is None:
        return False
    return left > right


@dataclass(frozen=True)
class Asset:
    """One downloadable file on a release, with the hash it must match."""

    name: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class Release:
    """A published release, reduced to the handful of fields this app uses."""

    version: str          # "1.3.0", normalised off the tag
    tag: str              # "v1.3.0"
    title: str            # "Rapid PDF 1.3.0"
    notes: str
    published_at: str
    html_url: str
    asset: Asset


def parse_latest(payload: bytes | str,
                 asset_suffix: str = PORTABLE_SUFFIX) -> Release:
    """Turn the /releases/latest body into a Release, or raise ReleaseError.

    `asset_suffix` names the file a self-update installs. Exactly one uploaded
    asset must match it: zero means the release was published without the
    payload (which has happened, see docs/build.md), and two would mean
    guessing which one to install.
    """
    data = _as_object(payload)

    if data.get("draft"):
        raise ReleaseError("the latest release is a draft")
    if data.get("prerelease"):
        # /releases/latest already excludes these, so this only fires when a
        # caller hands over a specific release. Still refused: a prerelease is
        # not something to push at an install unasked.
        raise ReleaseError("the latest release is a prerelease")

    tag = str(data.get("tag_name") or "").strip()
    title = str(data.get("name") or "").strip()
    version = _version_from(tag, title)
    if version is None:
        raise ReleaseError(
            f"the release names no version this app can read "
            f"(tag {tag!r}, title {title!r})"
        )

    asset = _asset_from(data.get("assets"), asset_suffix)

    notes = str(data.get("body") or "").strip()
    if len(notes) > NOTES_LIMIT:
        notes = notes[:NOTES_LIMIT].rstrip() + "..."

    return Release(
        version=version,
        tag=tag or f"v{version}",
        title=title or f"Rapid PDF {version}",
        notes=notes,
        published_at=str(data.get("published_at") or ""),
        html_url=str(data.get("html_url") or ""),
        asset=asset,
    )


def _as_object(payload: bytes | str) -> dict:
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseError(f"the reply is not UTF-8 text: {exc}") from exc
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ReleaseError(f"the reply is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReleaseError("the reply is not a JSON object")
    return data


def _version_from(tag: str, title: str) -> str | None:
    """The version, off the tag if it can be, off the title if it must be.

    The tag is the contract (`v1.3.0`); the title ("Rapid PDF 1.3.0") is the
    fallback for a release tagged some other way. Both are checked with the
    same regex, so a title that does not end in a plain semver is no version
    at all rather than a partial match.
    """
    parts = parse_version(tag)
    if parts is None and title:
        parts = parse_version(title.rsplit(" ", 1)[-1])
    if parts is None:
        return None
    return "{}.{}.{}".format(*parts)


def _asset_from(assets, suffix: str) -> Asset:
    if not isinstance(assets, list):
        raise ReleaseError("the release lists no assets")

    matches = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name.endswith(suffix):
            continue
        if str(item.get("state") or "") != "uploaded":
            # An asset still uploading has a size and a URL and is not there.
            continue
        matches.append(item)

    if not matches:
        raise ReleaseError(f"the release carries no {suffix} asset")
    if len(matches) > 1:
        names = ", ".join(sorted(str(m.get("name")) for m in matches))
        raise ReleaseError(f"the release carries more than one {suffix} "
                           f"asset: {names}")

    item = matches[0]
    name = str(item.get("name"))

    digest = _DIGEST.match(str(item.get("digest") or "").strip())
    if digest is None:
        # NOT a warning to carry on past. The digest is the only thing that
        # says the bytes that arrived are the bytes that were published, and
        # what gets done with those bytes is overwriting a working install.
        raise ReleaseError(
            f"{name} publishes no sha256 digest, so nothing could verify the "
            "download before it replaced the app"
        )

    url = str(item.get("browser_download_url") or "").strip()
    if not url.startswith("https://"):
        raise ReleaseError(f"{name} has no https download URL")

    try:
        size = int(item.get("size"))
    except (TypeError, ValueError) as exc:
        raise ReleaseError(f"{name} has no usable size") from exc
    if size <= 0:
        raise ReleaseError(f"{name} has a size of {size}")

    return Asset(name=name, size=size,
                 sha256=digest.group("hex").lower(), url=url)


def human_size(byte_count: int) -> str:
    """"67.3 MB". For a banner, so one decimal and no pedantry about MiB."""
    value = float(byte_count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"

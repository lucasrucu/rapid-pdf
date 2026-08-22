"""The network half: the GitHub Releases API, anonymously.

NO TOKEN, AND THAT IS A DECISION, not an omission. `lucasrucu/rapid-pdf` is a
public repo, so both things this needs are anonymous: the Releases API answers
unauthenticated requests, and `browser_download_url` serves the asset to
anybody. Shipping a credential inside an app that anybody can download would
mean shipping the credential to anybody who downloads the app, which is not a
thing to do in exchange for a rate limit nobody is close to. One check per
launch against an anonymous limit of 60 an hour per address leaves room for
fifty-nine launches to spare.

WHY urllib AND NOT requests. rapid-pdf's requirements are pymupdf, PySide6 and
qtawesome. Adding `requests` for two GETs would put it (and urllib3, certifi,
charset-normalizer, idna) into a PyInstaller bundle that is already 67 MB, for
an API that `urllib.request` reaches in fifteen lines. Certificate validation
is on by default and comes from the system store on Windows, which is what
should be trusted here anyway.

EVERY FAILURE IS THE SAME FAILURE. Offline, DNS not resolving, a captive
portal, a proxy, a rate limit, GitHub having an outage, a firewall on a mine
site: all of them mean "no update today", none of them is worth a dialog, and
they all arrive here as FeedUnavailable. client.check() swallows it. The one
caller that does show a message is the download, because a person pressed a
button and is watching a progress bar.

A FEED IS UNTRUSTED INPUT. Nothing here validates content. release.py checks
the JSON and client.stage() checks every byte against the published digest
before anything is moved into an install.
"""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from typing import BinaryIO

from core.version import APP_VERSION

OWNER = "lucasrucu"
REPO = "rapid-pdf"

API_ROOT = "https://api.github.com"

#: GitHub refuses requests with no User-Agent, and asks that it name the
#: client. It also makes this app's traffic legible in any log it shows up in.
USER_AGENT = f"rapid-pdf/{APP_VERSION} (+https://github.com/{OWNER}/{REPO})"

#: Short on purpose. This runs on startup in the background, and the only
#: thing a longer wait buys on a dead link is a thread sitting there.
TIMEOUT = 10.0

#: The download gets a longer one: it is 67 MB, and a slow link is not a dead
#: one. This is per read, not for the whole transfer.
DOWNLOAD_TIMEOUT = 60.0

#: A ceiling on the API reply so a wrong or hostile answer cannot be read into
#: memory forever. The real one is about 6 KB.
MAX_JSON = 1 << 20


class FeedUnavailable(Exception):
    """GitHub could not be reached, or did not answer with a release.

    A NORMAL CONDITION, not a fault. See the module docstring.
    """


class GitHubReleases:
    """The two things the updater asks of GitHub."""

    def __init__(self, owner: str = OWNER, repo: str = REPO,
                 timeout: float = TIMEOUT) -> None:
        self.owner = owner
        self.repo = repo
        self.timeout = timeout

    def describe(self) -> str:
        return f"github.com/{self.owner}/{self.repo} releases"

    @property
    def latest_url(self) -> str:
        return f"{API_ROOT}/repos/{self.owner}/{self.repo}/releases/latest"

    @property
    def releases_page(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/releases"

    def latest_release(self) -> bytes:
        """The raw JSON for the newest published, non-prerelease release."""
        request = urllib.request.Request(
            self.latest_url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                return reply.read(MAX_JSON)
        except urllib.error.HTTPError as exc:
            # 404 is a repo with no releases yet, 403/429 is the rate limit,
            # 5xx is GitHub. One answer for all of them.
            raise FeedUnavailable(
                f"{self.describe()} answered {exc.code}"
            ) from exc
        except (urllib.error.URLError, ssl.SSLError, socket.timeout,
                TimeoutError, OSError, ValueError) as exc:
            raise FeedUnavailable(
                f"{self.describe()} could not be reached "
                f"({exc.__class__.__name__})"
            ) from exc

    def open_asset(self, url: str) -> BinaryIO:
        """A binary stream for one release asset. The caller closes it.

        The URL comes from the release JSON and is checked for an https scheme
        by release.parse_latest before it gets here, which is what stops a
        rewritten reply from pointing this at a file:// path.
        """
        if not str(url).startswith("https://"):
            raise FeedUnavailable(f"refusing a non-https asset URL: {url!r}")
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/octet-stream",
                     "User-Agent": USER_AGENT},
        )
        try:
            return urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise FeedUnavailable(
                f"the download answered {exc.code}"
            ) from exc
        except (urllib.error.URLError, ssl.SSLError, socket.timeout,
                TimeoutError, OSError, ValueError) as exc:
            raise FeedUnavailable(
                f"the download could not be started "
                f"({exc.__class__.__name__})"
            ) from exc

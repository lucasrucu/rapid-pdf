"""The self-updater, everything except the widgets.

NOTHING HERE TOUCHES THE NETWORK. Every test that needs GitHub gets a fake
feed built from a real captured reply, because a test suite that depends on
api.github.com fails on a plane, fails behind a proxy, and burns an anonymous
rate limit that the app itself needs.

WHAT IS ACTUALLY BEING PINNED, in order of how expensive getting it wrong is:

  1. The version comparison, including every shape that must NOT compare. An
     unreadable version read as 0.0.0 either nags every install forever or
     switches updates off forever, depending on which side it lands on.
  2. check()'s "never raises" contract, against a feed that fails in every way
     a feed can fail.
  3. That a download whose hash does not match is never run, and that a failed
     update leaves nothing behind.
  4. install_kind(), which decides whether an update is applied at all. Getting
     it wrong towards "installed" runs an installer against a folder Inno does
     not own.
  5. The command line handed to the installer, which is the one thing that
     runs while the app is not there to report anything.

WHY THERE IS NO LONGER A TEST THAT COPIES rundll32.exe. Up to 1.9.0 the swap
helper was a batch file, so testing it meant running it, which meant a fake
install with a real exe in it, and the exe used was a copy of System32's
rundll32.exe under another name. That is MITRE ATT&CK T1036.003, "Masquerading:
Rename System Utilities", and on 9 September 2026 Sophos Endpoint Agent
detected it (Evade_13a) on this repo's own suite and deleted the files
mid-test. It was right about what it saw. The batch file is gone (see
core/update/installer.py), and where a real process is still needed the exe is
a five line stub compiled here with MSVC, which is a program written for this
test and not a Windows component wearing a different name.

THE STUB IS STILL HANDLED CAREFULLY, for the reason the old fixture was: it is
GUI subsystem so nothing ever creates a console window, it returns immediately,
and every process and temp folder is registered with addCleanup before it is
created. A test that spawns real processes and does not clean up after itself
leaves consoles all over somebody's desktop overnight; that has happened on
this machine, on the sibling project this design came from, and it is not
happening here.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from core.update import client, installer
from core.update.feed import FeedUnavailable
from core.update.release import (
    ReleaseError, human_size, is_newer, parse_latest, parse_version,
)

WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures: a release payload shaped like the real one, and a feed to serve it
# ---------------------------------------------------------------------------

def setup_asset(version: str, *, sha: str, size: int) -> dict:
    return {
        "name": f"rapid-pdf-setup-{version}.exe",
        "state": "uploaded",
        "size": size,
        "digest": f"sha256:{sha}",
        "content_type": "application/octet-stream",
        "browser_download_url": (
            "https://github.com/lucasrucu/rapid-pdf/releases/download/"
            f"v{version}/rapid-pdf-setup-{version}.exe"),
    }


def portable_asset(version: str) -> dict:
    return {
        "name": f"rapid-pdf-{version}-portable.zip",
        "state": "uploaded",
        "size": 67262398,
        "digest": "sha256:" + "b" * 64,
        "content_type": "application/zip",
        "browser_download_url": (
            "https://github.com/lucasrucu/rapid-pdf/releases/download/"
            f"v{version}/rapid-pdf-{version}-portable.zip"),
    }


def release_payload(version: str = "1.4.0", *, sha: str | None = None,
                    size: int = 48293712, assets: list | None = None,
                    **overrides) -> dict:
    """A /releases/latest reply, trimmed to the fields this app reads.

    The field names and the digest format are copied from the real reply for
    lucasrucu/rapid-pdf v1.3.0, so a change in the API shape shows up here.
    Both assets are attached by default because every real release carries
    both: the setup exe an update runs, and the zip a portable copy is sent to
    fetch by hand.
    """
    sha = sha or ("b6219c1b5bca85be60106d5595f9d713"
                  "c3df3f2d25f09e97bbe3d6757db86f5e")
    if assets is None:
        assets = [setup_asset(version, sha=sha, size=size),
                  portable_asset(version)]
    payload = {
        "tag_name": f"v{version}",
        "name": f"Rapid PDF {version}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-08-21T03:41:43Z",
        "html_url": f"https://github.com/lucasrucu/rapid-pdf/releases/tag/v{version}",
        "body": "## What's new\n\nThings.",
        "assets": assets,
    }
    payload.update(overrides)
    return payload


class FakeFeed:
    """Serves a canned JSON reply and a canned asset, or raises on demand."""

    def __init__(self, payload=None, asset_bytes: bytes = b"",
                 json_error: Exception | None = None,
                 asset_error: Exception | None = None) -> None:
        self.payload = payload
        self.asset_bytes = asset_bytes
        self.json_error = json_error
        self.asset_error = asset_error
        self.asset_calls = 0

    def describe(self) -> str:
        return "a fake feed"

    def latest_release(self) -> bytes:
        if self.json_error is not None:
            raise self.json_error
        if isinstance(self.payload, (bytes, str)):
            return (self.payload.encode() if isinstance(self.payload, str)
                    else self.payload)
        return json.dumps(self.payload).encode()

    def open_asset(self, url: str):
        self.asset_calls += 1
        if self.asset_error is not None:
            raise self.asset_error
        return io.BytesIO(self.asset_bytes)


def fake_installer(size: int | None = None) -> bytes:
    """Something shaped enough like a setup exe to get past the shape check.

    "MZ" and a plausible length, and nothing else. The staging code checks
    those two things and no more, on purpose: it is the last cheap place to
    stop, not a PE parser, and a real installer's internals are Inno's problem.
    """
    size = client.MIN_INSTALLER_BYTES if size is None else size
    body = client.PE_MAGIC + b"\x90" * max(0, size - len(client.PE_MAGIC))
    return body[:size]


#: Built once. It is a megabyte, and rebuilding it per test would be a
#: megabyte of memset for every assertion in this file.
GOOD_INSTALLER = fake_installer()


def remove_tree(path: Path, tries: int = 20) -> None:
    """rmtree, but it keeps trying for a second before it gives up.

    For the tests that start a real process. Windows does not release a
    directory handle the instant a process exits, so a single
    rmtree(ignore_errors=True) straight after one silently leaves the folder
    behind, and one leaked folder per run is how somebody's TEMP fills up.
    """
    for _ in range(tries):
        try:
            shutil.rmtree(path)
        except OSError:
            pass
        if not Path(path).exists():
            return
        time.sleep(0.05)
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

class VersionComparison(unittest.TestCase):

    def test_plain_and_prefixed_versions_parse(self):
        self.assertEqual(parse_version("1.3.0"), (1, 3, 0))
        self.assertEqual(parse_version("v1.3.0"), (1, 3, 0))
        self.assertEqual(parse_version("  v1.3.0  "), (1, 3, 0))
        self.assertEqual(parse_version("v10.20.30"), (10, 20, 30))

    def test_anything_off_the_shape_is_no_version_at_all(self):
        # Every one of these must be None, not a guess. A guess here is the
        # difference between "no update this launch" and "no updates ever".
        for text in (None, "", "   ", "unknown", "1.3", "1", "1.3.0.1",
                     "1.3.0-rc1", "v1.3.0+build", "one.two.three", "v", "1.x.0",
                     "Rapid PDF", "latest"):
            with self.subTest(text=text):
                self.assertIsNone(parse_version(text))

    def test_the_comparison_is_numeric_not_alphabetical(self):
        # The case a string comparison gets wrong, and the reason this module
        # exists: as text, "1.10.0" sorts BEFORE "1.9.0". No longer
        # hypothetical, this is the release that crossed it.
        self.assertTrue(is_newer("1.10.0", "1.9.0"))
        self.assertFalse(is_newer("1.9.0", "1.10.0"))
        self.assertTrue(is_newer("2.0.0", "1.99.99"))
        self.assertTrue(is_newer("1.3.1", "1.3.0"))

    def test_equal_is_not_newer(self):
        self.assertFalse(is_newer("1.3.0", "1.3.0"))
        self.assertFalse(is_newer("v1.3.0", "1.3.0"))

    def test_older_is_not_newer(self):
        self.assertFalse(is_newer("1.2.1", "1.3.0"))

    def test_an_unreadable_version_on_either_side_is_not_newer(self):
        self.assertFalse(is_newer("1.4.0", "unknown"))
        self.assertFalse(is_newer("1.4.0", ""))
        self.assertFalse(is_newer("1.4.0", None))
        self.assertFalse(is_newer("unknown", "1.3.0"))
        self.assertFalse(is_newer(None, "1.3.0"))
        self.assertFalse(is_newer(None, None))

    def test_human_size_reads_like_a_banner(self):
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(67262398), "64.1 MB")


# ---------------------------------------------------------------------------
# Reading a release
# ---------------------------------------------------------------------------

class ReleaseParsing(unittest.TestCase):

    def test_a_real_shaped_reply_parses(self):
        rel = parse_latest(json.dumps(release_payload("1.4.0")))
        self.assertEqual(rel.version, "1.4.0")
        self.assertEqual(rel.tag, "v1.4.0")
        self.assertEqual(rel.asset.name, "rapid-pdf-setup-1.4.0.exe")
        self.assertEqual(len(rel.asset.sha256), 64)
        self.assertTrue(rel.asset.url.startswith("https://"))

    def test_the_zip_is_read_too_but_is_not_the_one_that_gets_run(self):
        # Both are on every release. Only the setup exe is ever executed; the
        # zip is carried so a portable copy can be told what to go and fetch.
        rel = parse_latest(json.dumps(release_payload("1.4.0")))
        self.assertEqual(rel.asset.name, "rapid-pdf-setup-1.4.0.exe")
        self.assertIsNotNone(rel.portable)
        self.assertEqual(rel.portable.name, "rapid-pdf-1.4.0-portable.zip")

    def test_the_version_falls_back_to_the_title(self):
        payload = release_payload("1.4.0", tag_name="release-2026-08")
        self.assertEqual(parse_latest(json.dumps(payload)).version, "1.4.0")

    def test_a_release_naming_no_readable_version_is_refused(self):
        payload = release_payload("1.4.0", tag_name="nightly", name="Nightly")
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_drafts_and_prereleases_are_refused(self):
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(release_payload(draft=True)))
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(release_payload(prerelease=True)))

    def test_a_release_without_the_setup_exe_is_refused(self):
        # v1.3.0's actual situation, and it is the strict half of the rule: the
        # setup exe is the thing an update RUNS, so a release published without
        # it offers no update at all, to anybody. Never "install whatever else
        # is there".
        payload = release_payload("1.4.0", assets=[portable_asset("1.4.0")])
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_a_release_without_the_portable_zip_is_still_installable(self):
        # The loose half of the same rule, and it changed at 1.10.0. Nothing
        # here installs the zip any more, so a release missing it is perfectly
        # installable and all that is lost is being able to name the manual
        # download. Refusing it would switch updates off over a file nothing
        # reads.
        sha = hashlib.sha256(GOOD_INSTALLER).hexdigest()
        payload = release_payload("1.4.0", assets=[
            setup_asset("1.4.0", sha=sha, size=len(GOOD_INSTALLER))])
        rel = parse_latest(json.dumps(payload))
        self.assertEqual(rel.asset.name, "rapid-pdf-setup-1.4.0.exe")
        self.assertIsNone(rel.portable)

    def test_an_asset_still_uploading_does_not_count(self):
        payload = release_payload("1.4.0")
        payload["assets"][0]["state"] = "starter"
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_an_asset_with_no_digest_is_refused(self):
        # Nothing could verify the download, and what would be done with it is
        # RUNNING it. Refused rather than trusted.
        payload = release_payload("1.4.0")
        payload["assets"][0]["digest"] = None
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_a_digest_that_is_not_sha256_is_refused(self):
        payload = release_payload("1.4.0")
        payload["assets"][0]["digest"] = "md5:" + "a" * 32
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_a_non_https_download_url_is_refused(self):
        payload = release_payload("1.4.0")
        payload["assets"][0]["browser_download_url"] = "http://example.invalid/x.exe"
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_two_matching_assets_are_refused_rather_than_guessed_between(self):
        payload = release_payload("1.4.0")
        second = dict(payload["assets"][0])
        second["name"] = "rapid-pdf-setup-1.4.0-x64.exe"
        payload["assets"].append(second)
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_some_other_exe_on_the_release_is_not_taken_for_the_installer(self):
        # The match is on BOTH ends of the name. A bare ".exe" match would pick
        # up anything somebody attached afterwards, and what happens to the
        # match is that it gets run.
        payload = release_payload("1.4.0")
        payload["assets"].append({
            "name": "debug-symbols.exe", "state": "uploaded", "size": 100,
            "digest": "sha256:" + "c" * 64,
            "browser_download_url": "https://example.invalid/debug.exe",
        })
        rel = parse_latest(json.dumps(payload))
        self.assertEqual(rel.asset.name, "rapid-pdf-setup-1.4.0.exe")

    def test_rubbish_is_refused(self):
        for text in ("", "not json", "[]", "null", '"a string"', b"\xff\xfe\x00"):
            with self.subTest(text=text):
                with self.assertRaises(ReleaseError):
                    parse_latest(text)

    def test_long_release_notes_are_trimmed(self):
        payload = release_payload("1.4.0", body="x" * 9000)
        self.assertLessEqual(len(parse_latest(json.dumps(payload)).notes), 2100)


# ---------------------------------------------------------------------------
# check(): the contract is that it cannot raise, and cannot say yes wrongly
# ---------------------------------------------------------------------------

class Check(unittest.TestCase):

    def test_a_newer_release_is_offered(self):
        feed = FakeFeed(release_payload("1.4.0"))
        info = client.check("1.3.0", feed=feed)
        self.assertIsNotNone(info)
        self.assertEqual(info.version, "1.4.0")
        self.assertEqual(info.running, "1.3.0")
        self.assertIn("1.4.0", info.headline())

    def test_the_same_release_is_not_offered(self):
        feed = FakeFeed(release_payload("1.3.0"))
        self.assertIsNone(client.check("1.3.0", feed=feed))

    def test_an_older_release_is_not_offered(self):
        feed = FakeFeed(release_payload("1.2.1"))
        self.assertIsNone(client.check("1.3.0", feed=feed))

    def test_an_unreadable_running_version_offers_nothing(self):
        feed = FakeFeed(release_payload("1.4.0"))
        for current in ("", "unknown", "1.3", "1.4", "nightly"):
            with self.subTest(current=current):
                self.assertIsNone(client.check(current, feed=feed))

    def test_no_version_given_means_the_build_asks_about_itself(self):
        # None is "you did not tell me", not "unknown": it falls back to
        # core.version.APP_VERSION, which is what the app itself passes.
        from core.version import APP_VERSION
        major, minor, patch = parse_version(APP_VERSION)
        newer = f"{major}.{minor + 1}.0"
        feed = FakeFeed(release_payload(newer))
        info = client.check(None, feed=feed)
        self.assertIsNotNone(info)
        self.assertEqual(info.running, APP_VERSION)
        self.assertIsNone(client.check(None, feed=FakeFeed(
            release_payload(APP_VERSION))))

    def test_every_way_a_feed_can_fail_is_just_no_update(self):
        # Offline, DNS, a proxy, a rate limit, GitHub down, a truncated reply,
        # a reply from something that is not GitHub, and a bug in the feed
        # itself. None of them may reach the caller as an exception.
        feeds = [
            FakeFeed(json_error=FeedUnavailable("offline")),
            FakeFeed(json_error=TimeoutError("timed out")),
            FakeFeed(json_error=OSError("no route to host")),
            FakeFeed(json_error=RuntimeError("a bug in the feed")),
            FakeFeed(json_error=KeyError("something unexpected")),
            FakeFeed(payload=b"<html>a captive portal</html>"),
            FakeFeed(payload=b'{"tag_name": "v1.4.0"'),
            FakeFeed(payload={"tag_name": "v1.4.0", "assets": []}),
            FakeFeed(payload={}),
            FakeFeed(payload=release_payload("1.4.0", draft=True)),
        ]
        for feed in feeds:
            with self.subTest(feed=feed.json_error or feed.payload):
                self.assertIsNone(client.check("1.3.0", feed=feed))

    def test_check_never_downloads_anything(self):
        feed = FakeFeed(release_payload("1.4.0"), asset_bytes=GOOD_INSTALLER)
        client.check("1.3.0", feed=feed)
        self.assertEqual(feed.asset_calls, 0)


# ---------------------------------------------------------------------------
# Which kind of install is this. Nothing is applied without this answer.
# ---------------------------------------------------------------------------

class InstallKind(unittest.TestCase):
    """install_kind() decides whether an update happens at all.

    The dangerous direction is one-way: reporting PORTABLE for an installed
    copy costs a manual download, and reporting INSTALLED for a portable copy
    runs Inno against a folder it does not own, which does not update it and
    quietly creates a second install somewhere else.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rapidpdf-kind-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = self.tmp / "RapidPDF"
        self.install.mkdir()

    def _registered(self, value):
        """Pretend Inno's uninstall key says this."""
        original = client.installed_location
        client.installed_location = lambda: value
        self.addCleanup(setattr, client, "installed_location", original)

    def test_no_exe_at_all_is_source(self):
        self.assertEqual(client.install_kind(None), client.SOURCE)

    def test_a_folder_inno_registered_is_installed(self):
        self._registered(self.install)
        self.assertEqual(client.install_kind(self.install), client.INSTALLED)

    def test_a_folder_inno_never_heard_of_is_portable(self):
        self._registered(self.tmp / "somewhere else")
        self.assertEqual(client.install_kind(self.install), client.PORTABLE)

    def test_no_registration_at_all_is_portable(self):
        # The everyday portable case: nothing ever ran setup, so there is no
        # key to read.
        self._registered(None)
        self.assertEqual(client.install_kind(self.install), client.PORTABLE)

    def test_the_trailing_backslash_inno_writes_does_not_break_the_match(self):
        # Inno stores InstallLocation with a trailing separator and
        # sys.executable never has one. Comparing the strings raw would report
        # every installed copy as portable.
        self._registered(Path(str(self.install) + os.sep))
        self.assertEqual(client.install_kind(self.install), client.INSTALLED)

    @unittest.skipUnless(WINDOWS, "case-insensitive paths are a Windows thing")
    def test_the_case_inno_wrote_does_not_break_the_match(self):
        self._registered(Path(str(self.install).upper()))
        self.assertEqual(client.install_kind(self.install), client.INSTALLED)

    def test_the_app_id_matches_the_one_in_the_installer_script(self):
        # client.APP_ID names the registry key Inno writes. If the .iss ever
        # changes its AppId, every installed copy starts reporting portable and
        # self-update silently stops working, with no error anywhere.
        text = (ROOT / "rapid-pdf.iss").read_text(encoding="utf-8")
        # Inno escapes a leading brace by doubling it: {{GUID} in the file is
        # the AppId {GUID}.
        self.assertIn(f'#define AppId "{{{client.APP_ID}"', text)
        self.assertIn(f"{client.APP_ID}_is1", client.UNINSTALL_KEY)

    @unittest.skipUnless(WINDOWS, "the registry read is Windows only")
    def test_reading_the_real_registry_answers_without_raising(self):
        # Whatever this machine has, the read must produce a Path or None and
        # must not raise: it runs on every launch that offers an update.
        answer = client.installed_location()
        self.assertTrue(answer is None or isinstance(answer, Path))


# ---------------------------------------------------------------------------
# stage(): download, verify, and never touch the install
# ---------------------------------------------------------------------------

class Staging(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rapidpdf-update-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = self.tmp / "Rapid PDF"
        self.install.mkdir()
        (self.install / client.EXE_NAME).write_bytes(b"the old exe")

    def _feed_for(self, body: bytes, version: str = "1.4.0",
                  sha: str | None = None):
        digest = sha or hashlib.sha256(body).hexdigest()
        payload = release_payload(version, sha=digest, size=len(body))
        return FakeFeed(payload, asset_bytes=body)

    def test_a_good_release_stages_and_the_install_is_untouched(self):
        feed = self._feed_for(GOOD_INSTALLER)
        info = client.check("1.3.0", feed=feed)

        seen = []
        staged = client.stage(info, self.install, feed=feed,
                              progress=lambda d, t, p: seen.append(p))

        self.assertTrue(staged.installer_path.is_file())
        self.assertEqual(staged.installer_path.name,
                         "rapid-pdf-setup-1.4.0.exe")
        self.assertEqual(staged.installer_path.read_bytes(), GOOD_INSTALLER)
        # Measured off the file on disk, not read out of the release JSON.
        self.assertEqual(staged.installer_bytes, len(GOOD_INSTALLER))
        self.assertEqual(staged.staging_dir, self.tmp / "Rapid PDF.update")
        self.assertEqual(staged.installer_path.parent, staged.staging_dir)
        # Not one byte of the install has moved.
        self.assertEqual((self.install / client.EXE_NAME).read_bytes(),
                         b"the old exe")
        self.assertIn("downloading", seen)
        self.assertIn("checking", seen)

        staged.discard()
        self.assertFalse(staged.staging_dir.exists())

    def test_a_hash_mismatch_stops_before_anything_is_run(self):
        feed = self._feed_for(GOOD_INSTALLER, sha="0" * 64)
        info = client.check("1.3.0", feed=feed)
        with self.assertRaises(client.UpdateError) as caught:
            client.stage(info, self.install, feed=feed)
        self.assertIn("not the file GitHub published", str(caught.exception))
        # Nothing left behind, and the install is exactly as it was.
        self.assertFalse(client.staging_dir_for(self.install).exists())
        self.assertEqual((self.install / client.EXE_NAME).read_bytes(),
                         b"the old exe")

    def test_a_truncated_download_stops(self):
        feed = self._feed_for(GOOD_INSTALLER)
        info = client.check("1.3.0", feed=feed)
        feed.asset_bytes = GOOD_INSTALLER[:-500]
        with self.assertRaises(client.UpdateError):
            client.stage(info, self.install, feed=feed)
        self.assertFalse(client.staging_dir_for(self.install).exists())

    def test_a_download_that_dies_mid_transfer_leaves_nothing(self):
        feed = self._feed_for(GOOD_INSTALLER)
        info = client.check("1.3.0", feed=feed)
        feed.asset_error = FeedUnavailable("the link dropped")
        with self.assertRaises(client.UpdateError):
            client.stage(info, self.install, feed=feed)
        self.assertFalse(client.staging_dir_for(self.install).exists())

    def test_something_too_small_to_be_an_installer_is_refused(self):
        # THE BUG THE OLD FILE-COUNT FLOOR CAUGHT, in its new shape. GitHub's
        # digest matches whatever was uploaded, so a placeholder attached under
        # the right name verifies perfectly. The floor is the only thing
        # between that and running it.
        thin = fake_installer(client.MIN_INSTALLER_BYTES - 1)
        feed = self._feed_for(thin)
        info = client.check("1.3.0", feed=feed)
        with self.assertRaises(client.UpdateError) as caught:
            client.stage(info, self.install, feed=feed)
        self.assertIn("cannot be a whole build", str(caught.exception))
        self.assertIn("Nothing has been changed", str(caught.exception))
        self.assertFalse(client.staging_dir_for(self.install).exists())
        self.assertEqual((self.install / client.EXE_NAME).read_bytes(),
                         b"the old exe")

    def test_the_floor_is_a_floor_and_not_a_manifest(self):
        # Set far under a real installer's ~50 MB on purpose, so a slimmer
        # future build is not refused. Exactly the floor stages; one under it
        # does not, which is the test above.
        feed = self._feed_for(fake_installer(client.MIN_INSTALLER_BYTES))
        info = client.check("1.3.0", feed=feed)
        staged = client.stage(info, self.install, feed=feed)
        self.assertEqual(staged.installer_bytes, client.MIN_INSTALLER_BYTES)
        staged.discard()

    def test_something_that_is_not_a_program_is_refused(self):
        # Right name, right size, right hash, and not an executable. The digest
        # says the bytes are the published ones; it does not say they are a
        # build, and this is the last place that can be asked for free.
        not_a_program = b"PK\x03\x04" + b"z" * client.MIN_INSTALLER_BYTES
        feed = self._feed_for(not_a_program)
        info = client.check("1.3.0", feed=feed)
        with self.assertRaises(client.UpdateError) as caught:
            client.stage(info, self.install, feed=feed)
        self.assertIn("does not start like a Windows program",
                      str(caught.exception))
        self.assertFalse(client.staging_dir_for(self.install).exists())

    def test_a_download_aborted_from_the_progress_callback_leaves_nothing(self):
        # How closing the app mid-download stops it: the worker's progress
        # callback raises, and stage()'s own cleanup takes the half-written
        # folder with it. See ui/update_notice._StageWorker.cancel.
        feed = self._feed_for(GOOD_INSTALLER)
        info = client.check("1.3.0", feed=feed)

        def abort(done, total, phase):
            raise client.UpdateError("Rapid PDF is closing.")

        with self.assertRaises(client.UpdateError):
            client.stage(info, self.install, feed=feed, progress=abort)
        self.assertFalse(client.staging_dir_for(self.install).exists())
        self.assertEqual((self.install / client.EXE_NAME).read_bytes(),
                         b"the old exe")

    def test_a_stale_staging_folder_is_cleared_not_reused(self):
        stale = client.staging_dir_for(self.install)
        stale.mkdir(parents=True)
        (stale / "leftover.exe").write_bytes(b"from a dead update")
        feed = self._feed_for(GOOD_INSTALLER)
        info = client.check("1.3.0", feed=feed)
        staged = client.stage(info, self.install, feed=feed)
        self.assertFalse((staged.staging_dir / "leftover.exe").exists())

    def test_the_part_file_does_not_survive_a_dead_transfer(self):
        # The download is written through a .part and renamed, so a transfer
        # that dies leaves no short file under the real name. Nothing at all
        # should be left here, because the whole folder goes.
        feed = self._feed_for(GOOD_INSTALLER)
        info = client.check("1.3.0", feed=feed)
        feed.asset_error = FeedUnavailable("the link dropped")
        with self.assertRaises(client.UpdateError):
            client.stage(info, self.install, feed=feed)
        self.assertFalse(client.staging_dir_for(self.install).exists())


# ---------------------------------------------------------------------------
# The command the installer is run with
# ---------------------------------------------------------------------------

def fake_staged(install: Path, version: str = "1.4.0", *,
                installer_path: Path | None = None,
                installer_bytes_: int = 48_293_712) -> client.StagedInstaller:
    """A staged installer pointing at a file that really is on disk.

    build_command refuses a path that is not there, so the file is created:
    "the download is gone" is a real failure with a test of its own and must
    not be the accidental state of every other one.
    """
    staging = client.staging_dir_for(install)
    if installer_path is None:
        installer_path = staging / f"rapid-pdf-setup-{version}.exe"
        installer_path.parent.mkdir(parents=True, exist_ok=True)
        if not installer_path.exists():
            installer_path.write_bytes(client.PE_MAGIC)
    info = client.UpdateInfo(
        release=parse_latest(json.dumps(release_payload(version))),
        running="1.3.0")
    return client.StagedInstaller(
        info=info, install_dir=install, staging_dir=staging,
        installer_path=installer_path, installer_bytes=installer_bytes_)


class InstallerCommand(unittest.TestCase):
    """What gets handed to CreateProcess, read as text.

    This replaced a class that read a generated batch file the same way. The
    batch file is gone because every step in it was on a behavioural detection
    list; what is left is one process, started with documented switches, and
    the switches are what these tests pin.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rapidpdf-cmd-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A space in the path on purpose: that is every install made before the
        # 1.8.0 rename, and it is what a batch file could not carry safely.
        self.install = self.tmp / "Rapid PDF"
        self.install.mkdir()
        self.staged = fake_staged(self.install)
        self.command = installer.build_command(self.staged)

    def test_the_installer_itself_is_what_runs(self):
        self.assertEqual(self.command[0], str(self.staged.installer_path))

    def test_it_is_silent_but_not_invisible(self):
        # /SILENT hides the wizard and SHOWS the progress window.
        # /VERYSILENT would hide that too, which is both worse for the user
        # and the property that makes a thing look like a dropper.
        self.assertIn("/SILENT", self.command)
        self.assertNotIn("/VERYSILENT", self.command)

    def test_the_one_prompt_silent_does_not_suppress_is_suppressed(self):
        # /SILENT does not stop the "This will install... continue?" box, and
        # an update that stops on a modal box after the app has closed is an
        # app that never comes back.
        self.assertIn("/SP-", self.command)

    def test_error_boxes_are_left_switched_on(self):
        # The opposite call, on purpose. Once the app has closed, a message
        # box is the only way an install that went wrong can say so.
        self.assertNotIn("/SUPPRESSMSGBOXES", self.command)

    def test_it_asks_setup_to_close_the_app_rather_than_racing_it(self):
        # This is what replaced polling tasklist.exe for the app's own PID.
        self.assertIn("/CLOSEAPPLICATIONS", self.command)

    def test_the_relaunch_is_ours_and_not_the_restart_manager(self):
        # Inno can only restart an app that called RegisterApplicationRestart,
        # and this one does not, so /RESTARTAPPLICATIONS would be a promise
        # nothing keeps. The [Run] entry gated on the switch below is what
        # actually starts the app again.
        self.assertIn("/NORESTARTAPPLICATIONS", self.command)
        self.assertNotIn("/RESTARTAPPLICATIONS", self.command)
        self.assertIn("/RAPIDPDFRELAUNCH=1", self.command)

    def test_the_relaunch_switch_is_spelled_the_same_in_the_installer_script(self):
        # A typo here is silent: the update works and the app never comes back.
        text = (ROOT / "rapid-pdf.iss").read_text(encoding="utf-8")
        name = installer.RELAUNCH_SWITCH.lstrip("/").split("=")[0]
        self.assertIn(f"{{param:{name}|0}}", text)
        self.assertIn("Check: RelaunchAfterUpdate", text)
        self.assertIn("function RelaunchAfterUpdate", text)

    def test_it_names_the_folder_it_is_updating(self):
        # Belt and braces against Inno's own UsePreviousAppDir, and the thing
        # that stops an update ever making a SECOND install beside the first.
        self.assertIn(f"/DIR={self.install}", self.command)

    def test_a_path_with_a_space_needs_no_escaping_and_gets_none(self):
        # The old helper refused paths carrying a quote or a percent sign,
        # because they had to survive being pasted into a batch file. An argv
        # list has no shell to survive, so the path goes through whole.
        [dir_arg] = [a for a in self.command if a.startswith("/DIR=")]
        self.assertEqual(dir_arg[len("/DIR="):], str(self.install))
        self.assertIn(" ", dir_arg)

    def test_a_path_a_batch_file_could_not_carry_is_now_fine(self):
        odd = self.tmp / "odd%path"
        odd.mkdir()
        staged = fake_staged(odd)
        command = installer.build_command(staged)
        self.assertIn(f"/DIR={odd}", command)

    def test_the_log_goes_beside_the_exe_when_it_is_asked_for(self):
        log = client.log_path(self.install)
        command = installer.build_command(self.staged, log=log)
        self.assertIn(f"/LOG={log}", command)
        self.assertEqual(log.name, "update.log")

    def test_the_log_switch_is_left_off_when_there_is_no_log_to_write_to(self):
        # /LOG="filename" ABORTS the install when Setup cannot create the file,
        # so a logging convenience must never be able to fail an update. No
        # log means no switch, and Inno writes into TEMP under a name of its
        # own, which is worse to find and much better than not updating.
        self.assertFalse([a for a in self.command if a.startswith("/LOG=")])

    def test_a_download_that_is_no_longer_there_is_refused(self):
        staged = fake_staged(self.install)
        staged.installer_path.unlink()
        with self.assertRaises(installer.InstallerNotStarted) as caught:
            installer.build_command(staged)
        self.assertIn("Nothing has been changed", str(caught.exception))

    def test_prepare_log_creates_the_file_and_names_it(self):
        path = installer.prepare_log(self.install)
        self.assertEqual(path, client.log_path(self.install))
        self.assertTrue(path.is_file())

    def test_prepare_log_gives_up_rather_than_raising(self):
        # A folder where the file should be: the open() cannot work, and the
        # answer is None (log to TEMP) rather than an exception that would
        # stop an update that is otherwise fine.
        blocked = self.tmp / "blocked"
        blocked.mkdir()
        client.log_path(blocked).mkdir()
        self.assertIsNone(installer.prepare_log(blocked))

    def test_nothing_in_the_command_is_a_shell(self):
        # The whole point. No cmd.exe, no powershell, no script written to
        # disk, no ping used as a timer, no robocopy, no tasklist. If any of
        # these comes back, so does the detection that caused this rewrite.
        joined = " ".join(self.command).lower()
        for banned in ("cmd.exe", "powershell", "ping.exe", "robocopy",
                       "tasklist", ".cmd", ".bat", ".ps1"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, joined)


class NoSwapMachineryLeftAnywhere(unittest.TestCase):
    """The behaviours that got convicted, checked for across the whole app.

    Not a style rule. Sophos fired Evade_13a on this repo on 9 September 2026
    for a chain of exactly these calls, and each one on its own is enough to
    put an unsigned binary back on a behavioural engine's list. A test is the
    only thing that stops one drifting back in, because every one of them
    looks reasonable in isolation at the moment somebody types it.
    """

    #: Where the updater lives, plus the UI that drives it. The sweep is
    #: deliberately narrow: `subprocess` is used elsewhere in this app for
    #: things that have nothing to do with an update.
    FILES = ("core/update/client.py", "core/update/installer.py",
             "core/update/release.py", "core/update/feed.py",
             "core/update/__init__.py", "ui/update_notice.py")

    def _code(self, name: str) -> str:
        """The file with its comments and its prose taken out.

        Read off the source rather than grepped raw, because these modules
        spend most of their length EXPLAINING why they no longer do any of
        this, and a raw grep would match the explanation. Comments go, and so
        does every triple-quoted string, which in this package is always a
        docstring and never a value.
        """
        import tokenize
        text = (ROOT / name).read_text(encoding="utf-8")
        kept = []
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                continue
            if token.type == tokenize.STRING:
                body = token.string.lstrip("rbfuRBFU")
                if body.startswith('"""') or body.startswith("'''"):
                    continue
            kept.append(token.string)
        return " ".join(kept).lower()

    def test_no_updater_code_writes_or_runs_a_script(self):
        for name in self.FILES:
            code = self._code(name)
            for banned in ("cmd.exe", "comspec", "powershell", "robocopy",
                           "tasklist", "ping.exe", ".cmd", ".bat", ".ps1"):
                with self.subTest(file=name, banned=banned):
                    self.assertNotIn(banned, code)

    def test_no_updater_code_hides_a_window(self):
        # CREATE_NO_WINDOW on the process that rewrites a program directory is
        # concealment, and it is what the old helper used. Setup shows its own
        # progress window and there is nothing to hide.
        for name in self.FILES:
            with self.subTest(file=name):
                self.assertNotIn("create_no_window", self._code(name))

    def test_the_swap_module_is_gone_and_stays_gone(self):
        self.assertFalse((ROOT / "core" / "update" / "swap.py").exists())


# ---------------------------------------------------------------------------
# Actually starting a process
# ---------------------------------------------------------------------------

STUB_SOURCE = r"""
/* The stand-in installer for tests/test_update.py.
 *
 * GUI subsystem, so CreateProcess never makes a console window for it. It
 * writes the command line it was given to the file named by
 * RAPIDPDF_STUB_LOG, which is how the test reads back what launch() actually
 * passed rather than only what build_command() said it would, then waits a
 * third of a second and returns. The wait is there so a test can see that
 * launch() came back while the process was still alive, which is the whole
 * contract: the app has to be free to close.
 *
 * IT IS COMPILED, NOT COPIED. The fixture this replaced copied
 * System32\rundll32.exe under another name, which is MITRE ATT&CK T1036.003
 * and which Sophos detected on this machine.
 */
#include <windows.h>

int WINAPI wWinMain(HINSTANCE self, HINSTANCE prev, PWSTR args, int show)
{
    wchar_t path[1024];
    DWORD n = GetEnvironmentVariableW(L"RAPIDPDF_STUB_LOG", path, 1024);
    if (n > 0 && n < 1024) {
        HANDLE out = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                                 FILE_ATTRIBUTE_NORMAL, NULL);
        if (out != INVALID_HANDLE_VALUE) {
            LPWSTR whole = GetCommandLineW();
            DWORD wrote = 0;
            WriteFile(out, whole,
                      (DWORD)(lstrlenW(whole) * sizeof(wchar_t)), &wrote, NULL);
            CloseHandle(out);
        }
    }
    Sleep(300);
    return 0;
}
"""

VCVARS = (r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
          r"\VC\Auxiliary\Build\vcvars64.bat")


def find_vcvars() -> Path | None:
    """The MSVC environment script, or None when there is no compiler here.

    Tried in the order they are likely to exist: the Build Tools install this
    was written against, then whatever vswhere reports, which covers a full
    Visual Studio. None is not a failure, it is a machine with no compiler,
    and the one test that needs one skips.
    """
    if not WINDOWS:
        return None
    direct = Path(VCVARS)
    if direct.is_file():
        return direct
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
                   ) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return None
    try:
        found = subprocess.run(
            [str(vswhere), "-latest", "-products", "*", "-requires",
             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            capture_output=True, text=True, timeout=30,
            creationflags=NO_WINDOW, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not found:
        return None
    script = Path(found) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    return script if script.is_file() else None


def build_stub(where: Path) -> Path | None:
    """Compile the stand-in installer, or None when there is no compiler.

    The vcvars call is a normal MSVC bootstrap and the only reason cmd.exe
    appears anywhere in this file: it is how the toolchain is entered on
    Windows, it is running a compiler, and it is nothing to do with what the
    app does at run time.
    """
    vcvars = find_vcvars()
    if vcvars is None:
        return None
    source = where / "stub.c"
    source.write_text(STUB_SOURCE, encoding="ascii")
    # Not "rapid-pdf.exe" and not the real setup name either: nothing in this
    # file should be able to name a process somebody actually has running.
    target = where / "rapid-pdf-setup-stub.exe"
    line = (f'call "{vcvars}" >nul && cl /nologo /O1 /MT '
            f'/Fo"{where}\\stub.obj" /Fe"{target}" "{source}" '
            f'/link /SUBSYSTEM:WINDOWS /ENTRY:wWinMainCRTStartup '
            f'kernel32.lib >nul')
    try:
        # shell=True on purpose: cmd.exe's own /c quoting rules and
        # subprocess's argv quoting disagree about a command holding both
        # quoted paths and &&, and this is the form that survives both.
        done = subprocess.run(line, shell=True, cwd=str(where),
                              capture_output=True, text=True, timeout=300,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return target if target.is_file() and done.returncode == 0 else None


@unittest.skipUnless(WINDOWS, "starting the installer is Windows only")
class LaunchingTheInstaller(unittest.TestCase):
    """launch() really starts a process, against a stub compiled here.

    SKIPS CLEANLY WITH NO COMPILER. Everything else in this file is pure and
    runs everywhere; this one class needs a real exe to start, and a machine
    with no MSVC gets a skip with the reason on it rather than a failure.
    """

    stub: Path | None = None
    stub_home: Path | None = None

    @classmethod
    def setUpClass(cls):
        cls.stub_home = Path(tempfile.mkdtemp(prefix="rapidpdf-stub-"))
        cls.stub = build_stub(cls.stub_home)

    @classmethod
    def tearDownClass(cls):
        if cls.stub_home is not None:
            shutil.rmtree(cls.stub_home, ignore_errors=True)

    def setUp(self):
        if self.stub is None:
            self.skipTest(
                "no MSVC toolchain on this machine, so there is no stand-in "
                "installer to start. Install VS Build Tools with the C++ "
                "workload, or read the command line tests above, which cover "
                "everything except the Popen itself.")
        self.tmp = Path(tempfile.mkdtemp(prefix="rapidpdf-launch-test-"))
        # Registered FIRST so it runs LAST: addCleanup is last in, first out,
        # and nothing may still be holding this folder when it goes.
        self.addCleanup(remove_tree, self.tmp)
        self.started = []
        self.addCleanup(self._stop_everything)
        self.install = self.tmp / "Rapid PDF"
        self.install.mkdir()
        staging = client.staging_dir_for(self.install)
        staging.mkdir(parents=True)
        stand_in = staging / self.stub.name
        shutil.copy2(self.stub, stand_in)
        self.staged = fake_staged(self.install, installer_path=stand_in)
        self.record = self.tmp / "command-line.txt"
        os.environ["RAPIDPDF_STUB_LOG"] = str(self.record)
        self.addCleanup(os.environ.pop, "RAPIDPDF_STUB_LOG", None)

    def _stop_everything(self):
        """Nothing the test started may outlive it, or outlive the folder.

        launch() gives the installer the install's PARENT as its working
        directory, which in this test IS the temp folder, so a process still
        winding down keeps a handle on the thing that is about to be deleted.
        Every process is waited for here, before the folder goes.
        """
        for process in self.started:
            try:
                process.kill()
                process.wait(timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass

    def _launch(self, how=None):
        """Start the stand-in installer and remember it, so cleanup can wait."""
        started = (how or installer.launch)(self.staged)
        self.started.append(started)
        return started

    def _command_line(self) -> str:
        return self.record.read_bytes().decode("utf-16-le", errors="replace")

    def test_it_starts_the_installer_with_the_switches_it_said_it_would(self):
        started = self._launch(installer.apply)
        self.assertEqual(started.wait(timeout=60), 0)

        line = self._command_line()
        for switch in ("/SILENT", "/SP-", "/CLOSEAPPLICATIONS",
                       "/NORESTARTAPPLICATIONS", "/RAPIDPDFRELAUNCH=1"):
            with self.subTest(switch=switch):
                self.assertIn(switch, line)
        # The install path has a space in it, and it arrived in one piece.
        self.assertIn(f"/DIR={self.install}", line)
        self.assertIn(f"/LOG={client.log_path(self.install)}", line)

    def test_it_does_not_wait_for_the_installer(self):
        # The app has to be free to close the moment this returns: Setup is
        # waiting for it to go. The stub holds itself open for a third of a
        # second, so a launch() that waited would be caught here.
        started = self._launch()
        self.assertIsNone(started.poll(),
                          "launch() waited for the installer to finish")
        started.wait(timeout=60)

    def test_the_log_is_created_beside_the_exe_before_setup_runs(self):
        # Written by us and not by Inno, because /LOG= aborts the install when
        # Setup cannot create the file.
        self._launch().wait(timeout=60)
        self.assertTrue(client.log_path(self.install).is_file())

    def test_a_missing_installer_is_refused_before_anything_starts(self):
        self.staged.installer_path.unlink()
        with self.assertRaises(installer.InstallerNotStarted):
            installer.launch(self.staged)


if __name__ == "__main__":
    unittest.main()

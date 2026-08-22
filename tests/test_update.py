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
  3. That a download whose hash does not match is never unpacked, and that a
     failed update leaves nothing behind.
  4. The swap helper, which is the one piece that runs while the app is not
     there to report anything.

THE LAST ONE ACTUALLY RUNS THE BATCH FILE, and it is careful about it: the
process it relaunches is a copy of rundll32.exe (a GUI-subsystem exe that
exits immediately, so no console window is ever created), every process is
started with CREATE_NO_WINDOW, and every process and temp folder is registered
with addCleanup before it is created. A test that spawns real processes and
does not clean up after itself leaves consoles all over somebody's desktop
overnight; that has happened on this machine, on the sibling project this
design came from, and it is not happening here.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from core.update import client, swap
from core.update.feed import FeedUnavailable
from core.update.release import (
    ReleaseError, human_size, is_newer, parse_latest, parse_version,
)

WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Fixtures: a release payload shaped like the real one, and a feed to serve it
# ---------------------------------------------------------------------------

def release_payload(version: str = "1.4.0", *, sha: str | None = None,
                    size: int = 67262398, assets: list | None = None,
                    **overrides) -> dict:
    """A /releases/latest reply, trimmed to the fields this app reads.

    The field names and the digest format are copied from the real reply for
    lucasrucu/rapid-pdf v1.3.0, so a change in the API shape shows up here.
    """
    sha = sha or ("b6219c1b5bca85be60106d5595f9d713"
                  "c3df3f2d25f09e97bbe3d6757db86f5e")
    if assets is None:
        assets = [{
            "name": f"rapid-pdf-{version}-portable.zip",
            "state": "uploaded",
            "size": size,
            "digest": f"sha256:{sha}",
            "content_type": "application/zip",
            "browser_download_url": (
                "https://github.com/lucasrucu/rapid-pdf/releases/download/"
                f"v{version}/rapid-pdf-{version}-portable.zip"),
        }]
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


def build_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return buf.getvalue()


def release_zip(version: str = "1.4.0") -> bytes:
    """The shape the real portable zip has: one top folder, exe plus _internal."""
    return build_zip({
        f"rapid-pdf/{client.EXE_NAME}": b"the new exe" * 100,
        "rapid-pdf/_internal/python312.dll": b"a dll" * 100,
        "rapid-pdf/_internal/base_library.zip": b"stdlib" * 100,
        "rapid-pdf/assets/tessdata/eng.traineddata": b"ocr data" * 100,
    })


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
        # exists: as text, "1.10.0" sorts BEFORE "1.9.0".
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
        self.assertEqual(rel.asset.name, "rapid-pdf-1.4.0-portable.zip")
        self.assertEqual(len(rel.asset.sha256), 64)
        self.assertTrue(rel.asset.url.startswith("https://"))

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

    def test_a_release_without_the_portable_zip_is_refused(self):
        # This is v1.3.0's actual situation for the setup exe, in reverse: a
        # release can be published missing an asset, and the answer is "no
        # update", never "install whatever else is there".
        payload = release_payload("1.4.0", assets=[{
            "name": "rapid-pdf-setup-1.4.0.exe",
            "state": "uploaded", "size": 40_000_000,
            "digest": "sha256:" + "a" * 64,
            "browser_download_url": "https://example.invalid/setup.exe",
        }])
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_an_asset_still_uploading_does_not_count(self):
        payload = release_payload("1.4.0")
        payload["assets"][0]["state"] = "starter"
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_an_asset_with_no_digest_is_refused(self):
        # Nothing could verify the download, and what would be done with it is
        # overwriting a working install. Refused rather than trusted.
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
        payload["assets"][0]["browser_download_url"] = "http://example.invalid/x.zip"
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

    def test_two_matching_assets_are_refused_rather_than_guessed_between(self):
        payload = release_payload("1.4.0")
        second = dict(payload["assets"][0])
        second["name"] = "rapid-pdf-1.4.0-x64-portable.zip"
        payload["assets"].append(second)
        with self.assertRaises(ReleaseError):
            parse_latest(json.dumps(payload))

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
        feed = FakeFeed(release_payload("1.4.0"), asset_bytes=release_zip())
        client.check("1.3.0", feed=feed)
        self.assertEqual(feed.asset_calls, 0)


# ---------------------------------------------------------------------------
# stage(): download, verify, unpack, and never touch the install
# ---------------------------------------------------------------------------

class Staging(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rapidpdf-update-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = self.tmp / "Rapid PDF"
        self.install.mkdir()
        (self.install / client.EXE_NAME).write_bytes(b"the old exe")

    def _feed_for(self, zip_bytes: bytes, version: str = "1.4.0",
                  sha: str | None = None):
        import hashlib
        digest = sha or hashlib.sha256(zip_bytes).hexdigest()
        payload = release_payload(version, sha=digest, size=len(zip_bytes))
        return FakeFeed(payload, asset_bytes=zip_bytes)

    def test_a_good_release_stages_and_the_install_is_untouched(self):
        zip_bytes = release_zip()
        feed = self._feed_for(zip_bytes)
        info = client.check("1.3.0", feed=feed)

        seen = []
        staged = client.stage(info, self.install, feed=feed,
                              progress=lambda d, t, p: seen.append(p))

        payload = staged.payload_dir
        self.assertTrue((payload / client.EXE_NAME).is_file())
        self.assertTrue((payload / "_internal" / "python312.dll").is_file())
        # The zip's `rapid-pdf/` wrapper is stripped: what lands is the
        # CONTENTS of an install, ready to be laid over one.
        self.assertFalse((payload / "rapid-pdf").exists())
        self.assertEqual(staged.file_count, 4)
        self.assertEqual(staged.staging_dir, self.tmp / "Rapid PDF.update")
        # The archive itself is cleaned up once unpacked.
        self.assertFalse((staged.staging_dir / info.asset.name).exists())
        # Not one byte of the install has moved.
        self.assertEqual((self.install / client.EXE_NAME).read_bytes(),
                         b"the old exe")
        self.assertIn("downloading", seen)
        self.assertIn("unpacking", seen)

        staged.discard()
        self.assertFalse(staged.staging_dir.exists())

    def test_a_hash_mismatch_stops_before_anything_is_unpacked(self):
        zip_bytes = release_zip()
        feed = self._feed_for(zip_bytes, sha="0" * 64)
        info = client.check("1.3.0", feed=feed)
        with self.assertRaises(client.UpdateError) as caught:
            client.stage(info, self.install, feed=feed)
        self.assertIn("not the file GitHub published", str(caught.exception))
        # Nothing left behind, and the install is exactly as it was.
        self.assertFalse(client.staging_dir_for(self.install).exists())
        self.assertEqual((self.install / client.EXE_NAME).read_bytes(),
                         b"the old exe")

    def test_a_truncated_download_stops(self):
        zip_bytes = release_zip()
        feed = self._feed_for(zip_bytes)
        info = client.check("1.3.0", feed=feed)
        feed.asset_bytes = zip_bytes[:-500]
        with self.assertRaises(client.UpdateError):
            client.stage(info, self.install, feed=feed)
        self.assertFalse(client.staging_dir_for(self.install).exists())

    def test_a_download_that_dies_mid_transfer_leaves_nothing(self):
        zip_bytes = release_zip()
        feed = self._feed_for(zip_bytes)
        info = client.check("1.3.0", feed=feed)
        feed.asset_error = FeedUnavailable("the link dropped")
        with self.assertRaises(client.UpdateError):
            client.stage(info, self.install, feed=feed)
        self.assertFalse(client.staging_dir_for(self.install).exists())

    def test_an_archive_that_writes_outside_itself_is_refused(self):
        # The digest proves the bytes are the ones GitHub stored. It says
        # nothing about what those bytes contain, which is a different claim.
        evil = build_zip({
            f"rapid-pdf/{client.EXE_NAME}": b"x" * 100,
            "rapid-pdf/../../../../Windows/System32/evil.dll": b"x" * 100,
        })
        feed = self._feed_for(evil)
        info = client.check("1.3.0", feed=feed)
        with self.assertRaises(client.UpdateError) as caught:
            client.stage(info, self.install, feed=feed)
        self.assertIn("outside", str(caught.exception))
        self.assertFalse(client.staging_dir_for(self.install).exists())

    def test_an_archive_with_no_exe_in_it_is_refused(self):
        wrong = build_zip({"rapid-pdf/readme.txt": b"not a build" * 50})
        feed = self._feed_for(wrong)
        info = client.check("1.3.0", feed=feed)
        with self.assertRaises(client.UpdateError) as caught:
            client.stage(info, self.install, feed=feed)
        self.assertIn(client.EXE_NAME, str(caught.exception))

    def test_something_that_is_not_a_zip_is_refused(self):
        feed = self._feed_for(b"this is not a zip file, it is a sentence.")
        info = client.check("1.3.0", feed=feed)
        with self.assertRaises(client.UpdateError):
            client.stage(info, self.install, feed=feed)

    def test_a_download_aborted_from_the_progress_callback_leaves_nothing(self):
        # How closing the app mid-download stops it: the worker's progress
        # callback raises, and stage()'s own cleanup takes the half-written
        # folder with it. See ui/update_notice._StageWorker.cancel.
        zip_bytes = release_zip()
        feed = self._feed_for(zip_bytes)
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
        (stale / "payload").mkdir(parents=True)
        (stale / "payload" / "leftover.txt").write_bytes(b"from a dead update")
        zip_bytes = release_zip()
        feed = self._feed_for(zip_bytes)
        info = client.check("1.3.0", feed=feed)
        staged = client.stage(info, self.install, feed=feed)
        self.assertFalse((staged.payload_dir / "leftover.txt").exists())

    def test_a_flat_archive_keeps_its_paths(self):
        flat = build_zip({
            client.EXE_NAME: b"the new exe" * 50,
            "_internal/python312.dll": b"a dll" * 50,
        })
        feed = self._feed_for(flat)
        info = client.check("1.3.0", feed=feed)
        staged = client.stage(info, self.install, feed=feed)
        self.assertTrue((staged.payload_dir / client.EXE_NAME).is_file())
        self.assertTrue((staged.payload_dir / "_internal"
                         / "python312.dll").is_file())


# ---------------------------------------------------------------------------
# The swap helper, as text
# ---------------------------------------------------------------------------

def fake_staged(install: Path, version: str = "1.4.0") -> client.StagedUpdate:
    staging = client.staging_dir_for(install)
    info = client.UpdateInfo(
        release=parse_latest(json.dumps(release_payload(version))),
        running="1.3.0")
    return client.StagedUpdate(
        info=info, install_dir=install, staging_dir=staging,
        payload_dir=staging / "payload", file_count=4, payload_bytes=1234)


class SwapScript(unittest.TestCase):

    def setUp(self):
        self.install = Path(r"C:\Users\someone\AppData\Local\Programs\Rapid PDF")
        self.script = swap.build_script(fake_staged(self.install), pid=4242)

    def test_every_external_command_is_fully_qualified(self):
        # A bare `find` on a machine with Git for Windows on PATH is GNU find,
        # which fails and reports a running app as closed. The exe would then
        # be swapped under a live process.
        for command in ("tasklist.exe", "find.exe", "ping.exe", "robocopy.exe"):
            with self.subTest(command=command):
                self.assertIn(f'"%SYS%\\{command}"', self.script)
        self.assertIn('set "SYS=%SystemRoot%\\System32"', self.script)

    def test_it_waits_for_the_app_to_exit_and_gives_up_rather_than_racing(self):
        self.assertIn('set "PID=4242"', self.script)
        self.assertIn('/FI "PID eq %PID%"', self.script)
        self.assertIn(":wait", self.script)
        self.assertIn(":stuck", self.script)
        self.assertIn("if %WAITED% GEQ %LIMIT% goto stuck", self.script)

    def test_the_old_exe_is_kept_and_restored_on_failure(self):
        self.assertIn('move /Y "%INSTALL%\\%EXE%" "%INSTALL%\\%EXE%.bak"',
                      self.script)
        self.assertIn(":rollback", self.script)
        self.assertIn('if exist "%INSTALL%\\%EXE%.bak" move /Y '
                      '"%INSTALL%\\%EXE%.bak" "%INSTALL%\\%EXE%"', self.script)
        self.assertIn(":restart", self.script)

    def test_the_exe_is_swapped_by_rename_never_copied_over(self):
        self.assertIn('move /Y "%PAYLOAD%\\%EXE%" "%INSTALL%\\%EXE%"',
                      self.script)
        self.assertIn('/XF "%PAYLOAD%\\%EXE%"', self.script)

    def test_it_logs_beside_the_install_and_deletes_itself(self):
        self.assertIn(f'set "LOG=%INSTALL%\\{swap.LOG_NAME}"', self.script)
        self.assertIn('(goto) 2>nul & del "%~f0"', self.script)

    def test_it_is_ascii_and_crlf_so_cmd_can_read_it(self):
        self.script.encode("ascii")
        self.assertTrue(self.script.startswith("@echo off\r\n"))

    def test_a_path_cmd_cannot_carry_is_refused_rather_than_mangled(self):
        for bad in (r"C:\odd%path\Rapid PDF", 'C:\\a"b\\Rapid PDF'):
            with self.subTest(path=bad):
                with self.assertRaises(swap.SwapNotStarted):
                    swap.build_script(fake_staged(Path(bad)), pid=1)


# ---------------------------------------------------------------------------
# The swap helper, actually running
# ---------------------------------------------------------------------------

@unittest.skipUnless(WINDOWS, "the swap helper is cmd.exe and Windows only")
class SwapRun(unittest.TestCase):
    """Runs the real generated .cmd against a fake install.

    CLEANUP IS PART OF THE TEST, not an afterthought. Every process is started
    with CREATE_NO_WINDOW and registered with addCleanup before it is started,
    and the exe the helper relaunches is a copy of rundll32.exe: GUI subsystem,
    so `start` creates no console for it, and it exits on its own in about a
    tenth of a second. The sibling project's version of this test used
    `cmd /K` and left 45 console windows on the desktop overnight.
    """

    #: Not "rapid-pdf.exe": the cleanup sweep matches on image name, and it
    #: must not be able to reach a real Rapid PDF somebody has open.
    EXE = "rapid-pdf-swaptest.exe"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rapidpdf-swap-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._sweep)

        self.install = self.tmp / "Rapid PDF"
        self.install.mkdir()
        stand_in = Path(os.environ["SystemRoot"]) / "System32" / "rundll32.exe"
        shutil.copy2(stand_in, self.install / self.EXE)
        (self.install / "_internal").mkdir()
        (self.install / "_internal" / "old.txt").write_bytes(b"the old build")

        self.staged = fake_staged(self.install)
        payload = self.staged.payload_dir
        (payload / "_internal").mkdir(parents=True)
        # The same working exe plus a marker byte, so "did the swap happen"
        # is a byte comparison and not a guess.
        new_exe = stand_in.read_bytes() + b"\x00NEW"
        (payload / self.EXE).write_bytes(new_exe)
        (payload / "_internal" / "new.txt").write_bytes(b"the new build")
        self.new_exe = new_exe

    def _sweep(self):
        """Kill anything still carrying the test's image name. Normally a no-op."""
        subprocess.run(["taskkill", "/F", "/IM", self.EXE],
                       creationflags=NO_WINDOW, capture_output=True,
                       check=False)

    def test_the_helper_waits_swaps_and_restarts(self):
        # Stand-in for the running app: a process that exits on its own, so
        # the helper's wait loop has something real to wait for.
        app = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1.5)"],
            creationflags=NO_WINDOW)
        self.addCleanup(app.kill)

        script = swap.write_script(self.staged, exe_name=self.EXE,
                                   pid=app.pid, wait_turns=60)
        helper = swap.launch(script, self.install)
        self.addCleanup(helper.kill)

        app.wait(timeout=30)
        self.assertEqual(helper.wait(timeout=90), 0)

        installed = self.install / self.EXE
        self.assertEqual(installed.read_bytes(), self.new_exe,
                         "the new exe is not the one in the install")
        self.assertTrue((self.install / f"{self.EXE}.bak").is_file(),
                        "the previous exe was not kept as a .bak")
        self.assertTrue((self.install / "_internal" / "new.txt").is_file(),
                        "the supporting files were not moved in")
        self.assertFalse(self.staged.staging_dir.exists(),
                         "the staging folder was not cleaned up")
        self.assertFalse(script.exists(), "the helper did not delete itself")

        log = (self.install / swap.LOG_NAME).read_text(errors="replace")
        self.assertIn("update finished", log)

    def test_it_refuses_to_swap_while_the_app_is_still_running(self):
        # The failure that matters most: a wait loop that reads "no output"
        # as "gone" would replace the exe under a live process. One turn of
        # the loop, so it gives up almost immediately.
        app = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=NO_WINDOW)
        self.addCleanup(app.kill)

        script = swap.write_script(self.staged, exe_name=self.EXE,
                                   pid=app.pid, wait_turns=1)
        helper = swap.launch(script, self.install)
        self.addCleanup(helper.kill)
        self.assertEqual(helper.wait(timeout=60), 0)

        app.kill()
        self.assertNotEqual((self.install / self.EXE).read_bytes(), self.new_exe,
                            "the exe was replaced while the app was running")
        self.assertTrue((self.staged.payload_dir / self.EXE).is_file(),
                        "the staged update should still be there to retry")
        log = (self.install / swap.LOG_NAME).read_text(errors="replace")
        self.assertIn("GAVE UP", log)


if __name__ == "__main__":
    unittest.main()

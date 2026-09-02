"""The installer does not decide what a PDF looks like. These tests pin that.

THE HISTORY, BECAUSE IT WENT WRONG TWICE IN OPPOSITE DIRECTIONS.
Up to 1.5.0 the ProgID's DefaultIcon pointed at rapid-pdf.exe, so the moment a
user picked this app as their default PDF handler, every PDF in Explorer turned
into the gold app tile. 1.6.0 answered that by pointing DefaultIcon at a
document icon of our own drawing, and 1.7.0 went further and shipped that icon
down the in-app update path as well. Better looking, still the wrong call: a
PDF viewer has no business restyling somebody's whole file type on install.

WHAT IS PINNED NOW. The installer registers the ProgID, the open verb and the
combine verb, and claims no DefaultIcon at all; it also DELETES the key, which
is the only thing that reverts an install that already has a value there.

THE LIMIT, SO THE NEXT READER DOES NOT MISREAD THESE TESTS AS "PDFS ARE SAFE".
Windows has no generic PDF icon to fall back on. The icon of a file type is the
icon of the ProgID that owns it, and with DefaultIcon absent the shell uses the
first icon of the exe in shell\\open\\command. So a user who deliberately makes
this app their DEFAULT PDF app still gets the app tile on their PDFs. Deleting
the key is right because almost nobody does that, and because it leaves every
other machine alone; it is not the same as neutrality, and no registry value
means "leave it alone".

THE ARTWORK STAYS IN THE REPO, UNWIRED. assets/pdf-document.ico and its
generator are kept, and the frame tests below still hold them to being a
legible page at 16px, so putting the icon back is one registry line rather than
a redraw. Nothing installed points at it today.
"""

import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "assets" / "pdf-document.ico"
APP_ICON = ROOT / "assets" / "rapid-pdf.ico"
ISS = ROOT / "rapid-pdf.iss"

# What tools/make_document_icon.py is expected to emit. 16 and 32 are the ones
# Explorer actually uses in list and details view; 256 is the extra-large tile.
EXPECTED_SIZES = {16, 24, 32, 48, 64, 128, 256}


def _ico_directory(path: Path):
    """Parse an .ico into [(width, height, bpp, payload)], or fail loudly."""
    data = path.read_bytes()
    assert len(data) > 6, f"{path.name} is too short to be an icon"
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0, f"{path.name} has a non-zero reserved field"
    assert kind == 1, f"{path.name} is type {kind}, not 1 (.ico)"
    assert count > 0, f"{path.name} declares no images"

    frames = []
    for i in range(count):
        entry = data[6 + 16 * i:22 + 16 * i]
        w, h, _colours, _reserved, _planes, bpp, size, offset = struct.unpack(
            "<BBBBHHII", entry)
        # 0 in the directory means 256: the field is a single byte.
        w, h = (w or 256), (h or 256)
        assert offset + size <= len(data), (
            f"{path.name} frame {w}x{h} points past the end of the file")
        frames.append((w, h, bpp, data[offset:offset + size]))
    return frames


def test_document_icon_exists():
    assert ICON.exists(), (
        "assets/pdf-document.ico is missing. Nothing installed points at it "
        "today, but it is the drawn answer to 'what if we have to give PDFs "
        "an icon after all', and deleting it turns that back into an "
        "afternoon of artwork. Regenerate: python tools/make_document_icon.py"
    )


def test_generator_script_is_committed():
    script = ROOT / "tools" / "make_document_icon.py"
    assert script.exists(), (
        "tools/make_document_icon.py is the only way to reproduce the icon; "
        "without it assets/pdf-document.ico is an unmaintainable binary"
    )


def test_is_a_valid_multi_resolution_ico():
    frames = _ico_directory(ICON)
    assert len(frames) >= 2, "a single-size .ico defeats the point"
    for w, h, _bpp, payload in frames:
        assert w == h, f"frame {w}x{h} is not square"
        assert payload, f"frame {w}x{h} has an empty payload"


def test_carries_the_sizes_explorer_asks_for():
    sizes = {w for w, _h, _bpp, _payload in _ico_directory(ICON)}
    assert sizes == EXPECTED_SIZES, (
        f"expected {sorted(EXPECTED_SIZES)}, got {sorted(sizes)}. Explorer "
        "picks the nearest size and scales; a missing 16 is the one that "
        "shows, because details view is where most people meet the icon"
    )


def test_every_frame_is_32_bit():
    for w, _h, bpp, _payload in _ico_directory(ICON):
        assert bpp == 32, (
            f"frame {w}px declares {bpp}bpp; the icon needs a real alpha "
            "channel or it gets a hard edge on dark Explorer backgrounds"
        )


def test_frames_decode_to_their_declared_size():
    from PySide6.QtGui import QImage

    for w, h, _bpp, payload in _ico_directory(ICON):
        img = QImage.fromData(payload)
        assert not img.isNull(), f"frame {w}x{h} does not decode"
        assert (img.width(), img.height()) == (w, h), (
            f"frame declares {w}x{h} but decodes to "
            f"{img.width()}x{img.height()}"
        )


@pytest.mark.parametrize("size", [16, 32])
def test_small_frames_read_as_a_page_with_a_red_label(size):
    """The sizes Explorer really draws must not be a smudge.

    Cheap proxy for legibility: the frame has to be mostly opaque, mostly
    light (the page), and carry a run of clearly red pixels (the label). A
    blank frame, an all-red frame, or the gold app tile all fail this.
    """
    from PySide6.QtGui import QImage

    frames = {w: payload for w, _h, _bpp, payload in _ico_directory(ICON)}
    img = QImage.fromData(frames[size]).convertToFormat(
        QImage.Format.Format_ARGB32)

    opaque = light = red = 0
    for y in range(size):
        for x in range(size):
            c = img.pixelColor(x, y)
            if c.alpha() < 128:
                continue
            opaque += 1
            r, g, b = c.red(), c.green(), c.blue()
            if r > 200 and g > 200 and b > 200:
                light += 1
            elif r > 130 and r - g > 60 and r - b > 60:
                red += 1

    total = size * size
    assert opaque > total * 0.4, (
        f"{size}px frame is mostly transparent ({opaque}/{total} opaque)")
    assert light > opaque * 0.35, (
        f"{size}px frame is not reading as a light page "
        f"({light}/{opaque} light pixels)")
    assert red > total * 0.05, (
        f"{size}px frame has almost no red ({red} pixels); the PDF label is "
        "what makes it a PDF and not just any document")


def _iss_text() -> str:
    return ISS.read_text(encoding="utf-8", errors="replace")


def test_the_installer_does_not_ship_a_document_icon_to_the_app_root():
    """No reader, no file. Nothing points at {app}\\pdf-document.ico now."""
    text = _iss_text()
    assert not re.search(
        r'Source:\s*"assets\\pdf-document\.ico";\s*DestDir:\s*"\{app\}"', text
    ), (
        "rapid-pdf.iss still copies assets\\pdf-document.ico to {app}, but no "
        "registry value points at it. Either the [Files] line is stale or the "
        "DefaultIcon claim has come back without this test being updated"
    )


def test_the_installer_claims_no_default_icon_for_pdfs():
    """The whole point: Explorer keeps drawing PDFs the way it already did."""
    text = _iss_text()
    written = re.search(
        r'Subkey:\s*"Software\\Classes\\RapidPDF\.Document\\DefaultIcon";'
        r'[^\n]*ValueData:\s*"([^"]+)"',
        text,
    )
    assert not written, (
        f"rapid-pdf.iss writes DefaultIcon = {written.group(1)!r} if it "
        "matched. Whatever the picture is, setting this repaints every PDF on "
        "the machine for anyone who makes this app their default handler, and "
        "the decision was that the installer does not do that"
    )


def test_the_installer_clears_a_stale_default_icon():
    """The half that fixes machines that already have a value there.

    1.5.0 through 1.7.0 all wrote DefaultIcon. Simply stopping writing it
    leaves those installs exactly as they are forever, which is the shape of
    defect that produced this whole thread: an in-app update never runs the
    installer, so registry state from an August install is still live.
    """
    text = _iss_text()
    assert re.search(
        r'Subkey:\s*"Software\\Classes\\RapidPDF\.Document\\DefaultIcon";'
        r'[^\n]*Flags:[^\n]*deletekey',
        text,
    ), (
        "rapid-pdf.iss must delete Software\\Classes\\RapidPDF.Document\\"
        "DefaultIcon. Not writing a value is not the same as removing the one "
        "an earlier build wrote"
    )


def test_the_exe_still_wears_the_app_icon():
    """The other half of the split. Fixing the file icon must not swap the app's."""
    spec = (ROOT / "rapid-pdf.spec").read_text(encoding="utf-8", errors="replace")
    assert 'icon="assets/rapid-pdf.ico"' in spec, (
        "the PyInstaller exe icon must stay the app icon; the document icon "
        "belongs on files, not on the program"
    )
    assert APP_ICON.exists()

    text = _iss_text()
    assert re.search(r'SetupIconFile\s*=\s*assets\\rapid-pdf\.ico', text)
    assert "UninstallDisplayIcon={app}\\{#AppExeName}" in text


def test_installer_refreshes_the_shell_icon_cache():
    text = _iss_text()
    assert "SHChangeNotify" in text and "SHCNE_ASSOCCHANGED" in text, (
        "the installer has to fire SHCNE_ASSOCCHANGED. Upgrading users have "
        "an icon cached against the whole .pdf type, and removing the "
        "registry key on its own does not repaint anything"
    )


# ---------------------------------------------------------------------------
# The build side, which 1.7.0 added and this release removes again.
#
# 1.7.0 noticed that the installer's [Files] line was the only thing putting
# the icon at {app}\pdf-document.ico, and that an in-app update never runs the
# installer: it downloads the portable zip, which is the PyInstaller onedir
# folder, and robocopies it over the install (core/update/client.py,
# core/update/swap.py). Its answer was a post-COLLECT copy in the spec, so both
# delivery paths carried the file.
#
# Correct reasoning about the wrong goal. With no DefaultIcon there is nothing
# to deliver, so the copy goes too. The test below is what stops it coming back
# by habit, and the reasoning is preserved in the spec's comment so that a
# future decision to reinstate the icon does not have to rediscover it.
# ---------------------------------------------------------------------------

SPEC = ROOT / "rapid-pdf.spec"


def _spec_text() -> str:
    return SPEC.read_text(encoding="utf-8", errors="replace")


def test_the_spec_does_not_copy_the_document_icon_to_the_onedir_root():
    text = _spec_text()
    assert "shutil" not in text, (
        "rapid-pdf.spec copies a file into the onedir root again. The only "
        "thing that ever needed to be there was pdf-document.ico, for a "
        "DefaultIcon value that no longer exists; if the icon is coming back, "
        "rapid-pdf.iss has to claim it again first and these tests have to say so"
    )


def test_the_document_icon_ships_only_as_ordinary_bundled_data():
    """Kept in assets/, kept out of the install root.

    The datas entry sweeps all of assets/, so the file still lands in
    _internal/assets/ and costs a few KB. That is fine and is not what the
    DefaultIcon path cared about; what mattered was a literal path at the
    install root that a PyInstaller layout change could never move, and
    nothing needs one now.
    """
    text = _spec_text()
    assert '("assets", "assets")' in text
    assert "_onedir_root" not in text, (
        "the onedir-root copy is gone; a leftover reference to it means the "
        "removal was partial"
    )

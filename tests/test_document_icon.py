"""The .pdf FILE icon has to stay a document, and stay wired up.

Up to 1.5.0 the ProgID's DefaultIcon pointed at rapid-pdf.exe, so the moment a
user picked Rapid PDF as their default PDF handler every PDF in Explorer turned
into the gold app tile. Two different jobs got the same picture: the app icon
says which program this is, the document icon says what the file is, and PDFs
have read as a white page with a red label for as long as anyone has used one.
Repainting them all is a confusing thing to do to somebody's machine.

So there are two icons now, and these tests pin both halves of that: the
document icon really is a multi-resolution .ico with the sizes Explorer asks
for (16px above all, which is what the details view draws), and the installer
really points the file association at it while leaving the exe alone.

The 16px frame in particular is checked for content, not just for existing.
A frame that decoded to a blank or single-colour square would still be a valid
.ico and would still install cleanly, and nobody would notice until a PDF went
invisible in a file list.
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
        "assets/pdf-document.ico is missing. Without it the installer copies "
        "nothing to {app} and RapidPDF.Document\\DefaultIcon points at a file "
        "that is not there, which leaves every PDF with a blank icon. "
        "Regenerate: python tools/make_document_icon.py"
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


def test_installer_ships_the_icon_to_the_app_root():
    text = _iss_text()
    assert re.search(
        r'Source:\s*"assets\\pdf-document\.ico";\s*DestDir:\s*"\{app\}"', text
    ), (
        "rapid-pdf.iss must copy assets\\pdf-document.ico to {app}. The "
        "PyInstaller bundle puts assets under {app}\\_internal\\, and "
        "DefaultIcon is stored as a literal path, so the icon needs its own "
        "[Files] line at a path that will not move"
    )


def test_default_icon_points_at_the_document_icon_not_the_exe():
    text = _iss_text()
    match = re.search(
        r'Subkey:\s*"Software\\Classes\\RapidPDF\.Document\\DefaultIcon";'
        r'[^\n]*ValueData:\s*"([^"]+)"',
        text,
    )
    assert match, "rapid-pdf.iss no longer registers RapidPDF.Document\\DefaultIcon"
    value = match.group(1)
    assert "pdf-document.ico" in value, (
        f"DefaultIcon is {value!r}. It has to be the document icon: this is "
        "the image Explorer stamps on every .pdf on the machine once Rapid "
        "PDF is the default handler"
    )
    # Both spellings: the .iss writes the exe through a preprocessor macro, so
    # checking for the literal filename alone would miss a regression.
    for banned in ("rapid-pdf.exe", "{#AppExeName}"):
        assert banned not in value, (
            f"DefaultIcon is {value!r}, which is the app icon again. That is "
            "the 1.5.0 bug: choosing Rapid PDF turned every PDF gold"
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
        "the installer has to fire SHCNE_ASSOCCHANGED. Upgrading users "
        "already have the gold icon cached against every PDF, and changing "
        "the registry value on its own does not repaint anything"
    )

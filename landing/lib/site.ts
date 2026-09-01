// Single source of truth for the landing's external links and copy.
//
// VERSION LIVES IN ONE PLACE AND THE FILENAMES ARE BUILT FROM IT. On
// 2026-08-27 the site still offered 1.2.1 while the newest release was 1.3.0,
// and BOTH download buttons 404'd. GitHub's /releases/latest/download/<asset>
// needs the exact filename, and the filename carries the version, so a
// hardcoded one rots the moment a release ships. Bump VERSION below and both
// links follow.
//
// BEFORE BUMPING, CHECK BOTH ASSETS ARE ON THE RELEASE. 1.3.0 and 1.4.0 each
// shipped the portable zip alone, because the setup .exe needs Inno Setup on
// the build machine and everybody believed it was not installed. It was, at
// %LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe, a per-user path rather than
// the Program Files one docs/build.md names. A button pointing at an asset
// that is not there is worse than no button.

export const REPO = "lucasrucu/rapid-pdf";

/** The released version the site advertises. The one place to change. */
export const VERSION = "1.7.0";

export const LINKS = {
  github: `https://github.com/${REPO}`,
  releasesLatest: `https://github.com/${REPO}/releases/latest`,
  downloadInstaller:
    `https://github.com/${REPO}/releases/latest/download/` +
    `rapid-pdf-setup-${VERSION}.exe`,
  downloadPortable:
    `https://github.com/${REPO}/releases/latest/download/` +
    `rapid-pdf-${VERSION}-portable.zip`,
};

export const SITE = {
  name: "Rapid PDF",
  tagline: "Fast PDF page management and markup. OCR on demand. No wait.",
  description:
    "A focused Windows desktop PDF editor. Open several PDFs as tabs, reorder, combine, and delete pages, then drop highlights, rectangles, and lines, all instantly. Open an A1 engineering drawing and work without the Acrobat lag.",
  version: VERSION,
};

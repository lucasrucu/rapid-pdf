// Single source of truth for the landing's external links and copy.
//
// VERSION LIVES IN ONE PLACE AND THE FILENAMES ARE BUILT FROM IT. On
// 2026-08-27 the site still offered 1.2.1 while the newest release was 1.3.0,
// and BOTH download buttons 404'd. GitHub's /releases/latest/download/<asset>
// needs the exact filename, and the filename carries the version, so a
// hardcoded one rots the moment a release ships. Bump VERSION below and both
// links follow.
//
// The installer is deliberately not offered. 1.3.0 shipped with the portable
// zip only, because building the setup .exe needs Inno Setup on the build
// machine and it is not installed there. Offering a button that 404s is worse
// than not offering one. Put it back when an .exe is actually attached to a
// release, and check the asset is there before you do.

export const REPO = "lucasrucu/rapid-pdf";

/** The released version the site advertises. The one place to change. */
export const VERSION = "1.5.0";

export const LINKS = {
  github: `https://github.com/${REPO}`,
  releasesLatest: `https://github.com/${REPO}/releases/latest`,
  downloadPortable:
    `https://github.com/${REPO}/releases/latest/download/` +
    `rapid-pdf-${VERSION}-portable.zip`,
};

export const SITE = {
  name: "Rapid PDF",
  tagline: "Fast PDF page management and markup. OCR on demand. No wait.",
  description:
    "A focused Windows desktop PDF editor. Reorder, combine, and delete pages, then drop highlights, rectangles, and lines, all instantly. Open an A1 engineering drawing and work without the Acrobat lag.",
  version: VERSION,
};

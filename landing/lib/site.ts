// Single source of truth for the landing's external links and copy.
// Download points at the GitHub Releases "latest" asset so it never goes stale.

export const REPO = "lucasrucu/rapid-pdf";

export const LINKS = {
  github: `https://github.com/${REPO}`,
  releasesLatest: `https://github.com/${REPO}/releases/latest`,
  // The installer filename is stable per the Inno Setup OutputBaseFilename.
  // /releases/latest/download/<asset> always resolves to the newest release's asset.
  downloadInstaller: `https://github.com/${REPO}/releases/latest/download/rapid-pdf-setup-1.2.1.exe`,
  downloadPortable: `https://github.com/${REPO}/releases/latest/download/rapid-pdf-1.2.1-portable.zip`,
};

export const SITE = {
  name: "Rapid PDF",
  tagline: "Fast PDF page management and markup. OCR on demand. No wait.",
  description:
    "A focused Windows desktop PDF editor. Reorder, combine, and delete pages, then drop highlights, rectangles, and lines, all instantly. Open an A1 engineering drawing and work without the Acrobat lag.",
  version: "1.2.1",
};

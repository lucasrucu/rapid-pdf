# Licensing: the decision

**Decided 2026-09-09. RapidPDF is licensed under the GNU AGPL-3.0 or later,
Copyright (C) 2026 Lucas Ruiz.** The full text is in `LICENSE` at the repo root.
This file is the record of why, not a menu of options.

## What forced it

RapidPDF is built on **PyMuPDF**, and the installer ships it. Read from the
installed distribution metadata rather than from memory:

```
pymupdf 1.27.2.3   Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License
PySide6 6.11.1     LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only
qtawesome 1.4.2    MIT
pywinstyles 1.8    Creative Commons Zero v1.0 Universal
```

PyMuPDF is not an incidental dependency. It renders every page and performs
every save, `core/pdf_document.py` is a thin wrapper over it, and its compiled
extensions sit inside the bundle. Under the usual reading of the AGPL, the
distributed binary is a derivative work of it. That leaves two ways to ship
legally: AGPL-3.0 compatible terms, or an Artifex commercial licence, which
costs money. Permissive licences were never actually on the table for free.

PySide6's LGPL-3.0 is satisfiable either way and did not decide anything. It
does carry a relinking obligation that a frozen bundle is exactly where people
overlook, so it is written out in `NOTICE` along with the two build settings in
`rapid-pdf.spec` (onedir, `upx=False`) that keep it satisfied.

## What this costs, accepted knowingly

- No closed commercial edition can be built on this code without also buying a
  commercial PyMuPDF licence. That option was already priced out of reach, so
  nothing was actually given up.
- A fork cannot be closed. That is the point.
- Some employers will not touch AGPL code. Accepted.
- It is one way. Every version already shipped under AGPL stays AGPL for
  whoever holds it.

## What it buys

- The app is legal to distribute, which the eleven releases before this were
  arguably not.
- The landing page can say "open source" again, and does.
- AGPL-3.0 is OSI approved, which keeps **SignPath Foundation** free code
  signing on the table. `docs/build.md` records what being unsigned costs:
  antivirus false positives, SmartScreen warnings, and the 1.8.1 installer
  blocked on download by Sophos. Eligibility still needs confirming against
  their current rules, and a paid OV or EV certificate remains the alternative.

## The remediation part

Eleven GitHub releases went out before this with no licence file and no source
offer. Adding the licence fixes it going forward, and the source offer in the
README and in `NOTICE` covers binaries already out there: the complete
corresponding source for any released version is the git tag of the same
version number in the public repository, free and without a request.

## What was actually done

1. `LICENSE`: the verbatim AGPL-3.0 text, fetched from gnu.org, byte-identical
   to the canonical file.
2. `NOTICE`: third-party attribution, one entry per bundled component, each
   naming the licence that was verified and how it was verified.
3. `README.md`: a Licence section, the standard AGPL notice, and the source
   offer.
4. `landing/components/Hero.tsx`: "open source" restored to the hero strip.
5. `rapid-pdf.iss` (`LicenseFile`) and `packaging/version_info.txt`
   (`LegalCopyright`): the licence declared where the shipped artefacts state
   one, so it shows during install and in the file properties.

Still open, and deliberately not done here: the About box in
`ui/main_window.py` should carry the licence and the source link too. It
currently says only the copyright line.

# Licensing: the open decision

**Status: undecided. Nothing here is a recommendation.**

The landing page used to say "open source" in the hero strip under the download
buttons. It was removed on 2026-09-09 because the repo carries no LICENSE file
and the README makes no licensing statement. With no licence, default copyright
applies and nobody may legally copy, modify or redistribute the code, however
public the repo is. So the claim was not merely unsupported, it told a visitor
they had rights they do not have.

Removing the line is reversible. Adding a licence is much less so: once a
version ships under an open licence, that version stays open for everyone who
already has it, and you cannot take it back. That is why this is written down
rather than decided.

## What claiming "open source" would actually require

1. A LICENSE file at the repo root, committed, naming one licence.
2. The same licence named in the README, so it is visible without digging.
3. A copyright line with the year and the holder.
4. Checking the dependencies allow the licence you pick (see the constraint
   below, which is the part most likely to decide this for you).
5. Optionally a CONTRIBUTING note, once other people can legally send patches.

Until 1 and 2 exist, the honest words for the site are the ones now on it: free,
Windows, files never leave your PC. All true today, none of them a licence.

## The dependency constraint, worth checking before anything else

RapidPDF ships PyMuPDF and PySide6.

- **PyMuPDF** is distributed under AGPL-3.0, with a paid commercial licence as
  the alternative. AGPL is the strongest copyleft in common use.
- **PySide6** is LGPLv3, with a paid commercial licence as the alternative.
  LGPL is satisfiable by a closed app under conditions (dynamic linking and
  relink rights, roughly), which is the ordinary way desktop apps use Qt.

The practical read is that PyMuPDF's AGPL, not your preference, may already set
the floor: an app that bundles an AGPL library and is distributed to others is
generally expected to be offered under AGPL-compatible terms, or covered by the
vendor's commercial licence instead. **This applies to the binary you already
ship, licence file or not.** Verify it properly before choosing anything below,
because if it holds, the permissive options are not actually available for free,
and the real choice is AGPL versus buying a commercial PyMuPDF licence.

## The options, in plain terms

### Permissive (MIT, Apache-2.0)

Anyone may take the code, change it, and ship it in a closed product without
publishing their changes. Shortest path to contributions and to the widest
reuse.

- Upside: no friction for anyone, including you. You keep every commercial
  option open on your own copy, because you own the copyright and can dual
  licence or relicense future versions freely.
- Downside: someone can fork RapidPDF, rebrand it, sell it, and owe you
  nothing but attribution. Apache-2.0 adds an explicit patent grant and a
  notice requirement; MIT is shorter and does neither.

### Weak copyleft (MPL-2.0, LGPL-3.0)

Changes to the project's own files must be published; code that merely uses it
need not be.

- Upside: improvements come back, and the licence does not reach into a larger
  work that includes it. A middle position that still allows commercial use.
- Downside: more to explain to a contributor or an employer than MIT, and less
  reuse than permissive.

### Strong copyleft (GPL-3.0, AGPL-3.0)

Anyone distributing the app or a derived work must offer the full source under
the same licence. AGPL extends that to software offered over a network.

- Upside: a fork cannot be closed. Given the PyMuPDF position above, AGPL may
  also be the least friction, because it is what the dependency already asks
  for.
- Downside: it forecloses shipping a closed commercial edition built on the
  same code, unless you also hold a commercial PyMuPDF licence and own or
  relicense everything else. Some employers will not touch AGPL code at all.

## The SignPath Foundation angle

RapidPDF is an unsigned Windows binary, and docs/build.md already records what
that costs: antivirus false positives, SmartScreen warnings, and at least one
installer blocked on download. A code signing certificate is the fix, and a
real one is an annual cost plus identity validation.

SignPath Foundation gives free code signing certificates to open source
projects. Their eligibility is what it sounds like: an OSI-approved licence,
public source, and a reproducible build from that source. If RapidPDF gets an
OSI licence it plausibly qualifies, and the signing problem goes away without
an annual bill.

That is a concrete, immediate upside on the open side of the ledger. It is not
the only way to sign (a paid OV or EV certificate does the same job with no
licensing consequence at all), and eligibility would need confirming against
their current rules rather than assumed.

## What to do with this

Nothing, until you decide. The site says only true things now, so there is no
clock on it. If the answer turns out to be yes, the work is a LICENSE file, a
README line, and putting the words back on the hero. If it is no, this file is
the record of why, and the hero line stays as it is.

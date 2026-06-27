# Packaging rapid-pdf as a formal Windows application

Research notes and a recommended path for turning rapid-pdf from a `python main.py`
script into a proper installable Windows app: an installer, Start-menu entry,
desktop shortcut, uninstall support, and an app icon.

Status: **research only**. Nothing here is built yet. This document exists so Lucas
can pick an approach. The packaging approach is a decision waiting on him.

---

## TL;DR recommendation

**PyInstaller (onedir) -> Inno Setup installer. Signing optional but recommended
once he showcases it.**

- Freeze with **PyInstaller in onedir mode**. It handles PySide6 + PyMuPDF out of
  the box, is the lowest-effort path, and onedir avoids the worst startup-lag and
  antivirus problems that onefile causes.
- Wrap it with an **Inno Setup** `.iss` script. That gives a real installer wizard,
  Start-menu shortcut, optional desktop shortcut, an uninstaller, and the app icon
  baked into Add/Remove Programs.
- Add an **app icon (`.ico`)** and **version metadata** so it looks like a real app
  in the taskbar and installer. Cheap, do it early.
- **Code signing** is the only thing that removes the SmartScreen "unknown
  publisher" warning. Skip it for personal use. When he showcases the app (qori-hub,
  recruiters), sign it with **Azure Trusted Signing / Artifact Signing** (~$10/mo),
  not a traditional cert.

Rough effort once he decides to do it: **a focused half-day to a day** for a working
signed-or-unsigned installer, most of it spent on the first PyInstaller build and the
icon. See the phased plan at the bottom.

---

## App context that shapes the advice

- Entry point `main.py`, a single-window PySide6 (Qt6) `QApplication`. Run today via
  `.venv\Scripts\python.exe main.py`, `run.bat`, or `pythonw.exe` for no console.
- Dependencies: `pymupdf` and `PySide6`. **Both are large.** PySide6 ships the Qt6
  shared libraries and plugins; PyMuPDF ships the native MuPDF library. A frozen
  build will land around **100-250 MB on disk** depending on tool and mode. This is
  unavoidable and worth stating up front so the output size isn't a surprise.
- Pure desktop, local, no server, Windows-first (Win11). That keeps packaging simple:
  no services, no networking, no cross-platform requirement.
- Personal/professional tool that may later be showcased. That is the only reason
  code signing enters the picture at all.

---

## 1. Freezing the app to an `.exe`

You need a "freezer" to bundle Python + the libraries + your code into something that
runs without a Python install. Three candidates.

### PyInstaller (recommended freezer)

- **PySide6 support:** works out of the box. PyInstaller ships maintained hooks for
  PySide6 that collect the Qt plugins and shared libs automatically. No manual plugin
  copying for a normal app.
- **PyMuPDF support:** modern PyMuPDF (1.24+) bundles its native library in the wheel
  and is picked up automatically. Generally no special hook needed.
- **Known gotchas (flag these, they cost people hours):**
  - *Virtualenv vs system collision.* If PySide6/shiboken6 is also installed
    system-wide, PyInstaller may grab the system copy instead of the venv one without
    warning. Build from a clean venv that has no global PySide6.
  - *Only one Qt binding allowed.* If PyQt5/PyQt6/PySide2 is anywhere in the
    environment, PyInstaller aborts. Keep the build env to PySide6 only.
  - *PyMuPDF + onefile + `console=False`.* Some PyMuPDF versions hit an
    `AssertionError: No output specified` on import when frozen as a windowed onefile
    exe. The clean workaround is **onedir** (and it didn't reproduce when launched via
    `pythonw.exe`). One more reason to prefer onedir here.
- **onefile vs onedir:**
  - *onefile* = one `.exe` that unpacks itself to a temp dir on every launch. Tidy to
    hand someone, but slower cold start (unpacks 100+ MB each time) and a frequent
    antivirus/SmartScreen false-positive trigger because of the self-extraction.
  - *onedir* = a folder with the `.exe` plus its DLLs. Faster start, far fewer AV
    false positives, and it's the natural input to an installer (the installer is what
    hides the folder from the user anyway). **Use onedir.**
- **Output size:** largest of the three, roughly **90-150 MB+** for a PySide6 app
  because it embeds a full CPython plus uncompressed Qt libs.
- **Startup:** onedir starts fast. onefile has a noticeable unpack delay.
- **Build complexity:** lowest. Pure Python, no C toolchain. One command to a working
  build, then a `.spec` file to lock options. (Per the research-only scope, this repo
  has **no** `.spec` file yet.)

### Nuitka (the performance/size alternative)

- Compiles your Python to C and builds a real executable. Has first-class PySide6
  support (`--enable-plugin=pyside6`) and handles PyMuPDF.
- **Upside:** smallest binaries (static linking, dead-code elimination can land ~40%
  smaller than PyInstaller in benchmarks) and the **fastest startup** because there's
  no interpreter bootstrap. Also gives real code obfuscation as a side effect.
- **Downside:** needs a working **C/C++ compiler toolchain** (MSVC or MinGW) set up
  correctly, and **build times are long** (minutes, not seconds, and much longer on a
  cold first compile). More moving parts when a build breaks.
- **Verdict:** worth it only if startup speed or binary size becomes a real complaint,
  or he wants the compiled code harder to inspect for a showcase. For a single-window
  PDF utility, the extra setup and build time don't pay off. Keep it as a "phase 2 if
  needed" option.

### cx_Freeze (briefly)

- Works, pure-Python like PyInstaller, has PySide6 support. In practice its ecosystem
  and hook coverage are thinner than PyInstaller's, and it pairs less commonly with
  Windows installers. No reason to choose it over PyInstaller here.

**Freezer recommendation: PyInstaller, onedir mode.** Lowest effort, well-trodden with
PySide6 + PyMuPDF, and onedir sidesteps the onefile startup and AV pain.

---

## 2. Building a real installer (the part Lucas actually wants)

A frozen `.exe` is not "a formal app." The installer is what gives the Start-menu
entry, the desktop shortcut, the uninstaller, and the Add/Remove Programs presence.

### Inno Setup (recommended installer)

- Free, the de-facto standard for wrapping a frozen Python app. Pascal-scripted `.iss`
  file, plus a GUI script wizard to generate a first draft.
- **What Lucas gets, all from one script:**
  - A proper setup wizard (`rapid-pdf-setup.exe`).
  - **Start-menu shortcut** and an optional **desktop shortcut** (`[Icons]` section,
    desktop shortcut behind a "create a desktop icon" checkbox is the standard
    pattern).
  - A real **uninstaller** registered in Add/Remove Programs, with the app icon and
    version shown.
  - **Per-user** (no admin prompt, installs to `%LocalAppData%`) **or per-machine**
    (admin, installs to `Program Files`) install. Per-user is the smoother showcase
    experience because it avoids the UAC prompt.
  - File association for `.pdf` later if wanted (optional).
- **Effort:** low. Point the script at the PyInstaller onedir output folder, set name
  / version / publisher / icon, define the two shortcuts, compile. A working script is
  an afternoon, much of it first-time learning.
- **Signing:** Inno Setup itself doesn't require signing. You optionally configure it
  to sign both your app exe and the generated setup exe via a sign tool. Independent of
  the installer choice.

### NSIS (alternative)

- Also free and capable, more scriptable and lower-level. Steeper learning curve and
  more boilerplate for the same result. Choose it only if you outgrow Inno Setup's
  model, which is unlikely for this app. **Not recommended over Inno Setup here.**

### MSIX (modern, but heavier)

- Microsoft's modern packaging format: clean install/uninstall, app-store-ready,
  built-in auto-update, sandboxing.
- **Catch:** signing is effectively **mandatory** (Windows won't install an unsigned
  MSIX without dev-mode/sideloading gymnastics), and the whole pipeline is heavier to
  set up. Worth considering only if he later wants Microsoft Store distribution or
  managed auto-updates. Overkill for a personal/showcase desktop tool right now.

### WiX / MSI (enterprise)

- Produces classic `.msi` packages for enterprise deployment (Group Policy, etc.).
  Powerful, XML-heavy, steep. **Overkill here.** (Note: BeeWare Briefcase below uses
  WiX under the hood, so you can get an MSI without writing WiX yourself.)

**Installer recommendation: Inno Setup.** It delivers exactly the "formal installable
app" feel Lucas described, for the least effort, free.

---

## 3. All-in-one alternative: BeeWare Briefcase

Briefcase packages and builds an installer in one tool, cross-platform, producing an
**MSI on Windows** (via WiX, no WiX scripting required).

- **PySide6:** officially supported as a GUI bootstrap (alongside Toga and Pygame), so
  PySide6 + PyMuPDF can be packaged with it.
- **Trade-offs to know:**
  - Briefcase builds Windows apps on the **python.org embeddable** Python, which is
    **missing some stdlib modules** (notably `tkinter`). rapid-pdf doesn't use tkinter,
    so this is probably fine, but it's the kind of thing that surprises you mid-build.
  - It's most natural when you start a project *as* a Briefcase project. Retrofitting
    an existing app means adapting to its config/layout conventions.
  - You get an MSI, not the friendly Inno-style EXE wizard. Fine, just different.
- **Verdict:** Briefcase is a legitimate one-tool option and gives a real MSI with an
  uninstaller. But for a Windows-only app that already exists, **PyInstaller + Inno
  Setup is the more common, more documented, more controllable pairing** and matches
  the "installer wizard" mental image better. Use Briefcase only if you specifically
  want one tool and an MSI and don't mind its conventions.

---

## 4. Code signing (removing the SmartScreen / antivirus warnings)

Unsigned installers and exes trigger the SmartScreen "Windows protected your PC -
unknown publisher" blue prompt, and sometimes antivirus flags. Signing is what makes it
look professional. This matters specifically for the showcase scenario.

### What the options are

- **Self-signed certificate:** free, but **does not remove the warning** for anyone
  else (their machine doesn't trust your cert). Useful only for your own machine.
  Effectively pointless for distribution.
- **OV (Organization Validation) certificate:** traditional cert from a CA (Sectigo,
  DigiCert, etc.). Roughly **$200-400+/year**. Modern CA/Browser-Forum rules now
  require the private key on a hardware token or HSM, which adds friction. SmartScreen
  reputation still has to *build up over downloads* even when signed.
- **EV (Extended Validation) certificate:** stricter vetting, hardware token,
  historically **$400-900+/year**. Its old advantage was instant SmartScreen
  reputation. For a personal showcase the cost is hard to justify.
- **Azure Trusted Signing (now "Artifact Signing") - the modern cheap option:**
  - Microsoft's managed signing service. **~$9.99/month** (Basic tier), pay-as-you-go.
    Far cheaper than a traditional cert and no hardware token to manage (keys live in
    the service, FIPS 140-2 Level 3).
  - **Eligibility:** now open to **individual developers**, not just organizations,
    but for **individuals it's currently US and Canada only**. Organizations:
    US, Canada, EU, UK. **Lucas is in Indonesia**, so as an individual he likely
    **cannot enroll today** unless he qualifies through a US/Canada/EU/UK org. This is
    the key blocker to check before banking on it.
  - Requires a **paid Azure subscription** (no free/trial subscriptions allowed).
  - Issues standard (OV-equivalent) certs, **not EV**. SmartScreen reputation still
    builds over time rather than being instant.
  - Integrates with SignTool / GitHub Actions for signing your exe and your installer.

### Practical signing guidance for rapid-pdf

- **Now / personal use:** don't sign. Accept the one-time SmartScreen "More info ->
  Run anyway" click. Free and fine for himself.
- **When showcasing:** sign. Azure Trusted Signing is the cheapest sane route *if* the
  US/CA/EU/UK availability works for him; otherwise an OV cert from a CA is the
  fallback. Either way, expect SmartScreen reputation to warm up over the first batch
  of downloads even after signing.
- Sign **both** the app `.exe` and the Inno Setup `setup.exe`.

---

## 5. App icon and metadata (making it look real)

Cheap, high-impact polish. Do this even before the installer.

- **Icon:** provide a Windows `.ico` containing multiple sizes (at minimum 16, 32, 48,
  256 px) so it's crisp in the taskbar, Start menu, title bar, and Add/Remove Programs.
  - Set it on the window/app at runtime (`QApplication`/`QIcon`) so it shows in the
    taskbar while running.
  - Pass it to PyInstaller (`--icon`) so the frozen `.exe` carries it.
  - Reference it in the Inno Setup script for shortcuts and the uninstaller entry.
- **Version / publisher metadata:** embed a version-info resource in the exe (product
  name "Rapid PDF", version, company/publisher "Lucas", description) so right-click ->
  Properties -> Details looks like a shipped app. PyInstaller does this via a version
  file; Inno Setup carries its own `AppName`/`AppVersion`/`AppPublisher` for the
  installer and Add/Remove Programs entry. Keep the two in sync.
- The app already sets `setApplicationName("Rapid PDF")` and
  `setOrganizationName("Lucas")`, which is a good start for taskbar grouping and
  settings paths. Match the icon/version metadata to those names.

---

## Recommended path and phased plan

**Recommended stack: PyInstaller (onedir) + app icon/version info + Inno Setup
installer, with code signing added at showcase time.**

Honest trade-offs:
- Output is large (~100-200 MB installed). Unavoidable with PySide6 + PyMuPDF; not a
  problem for a desktop install, only matters if you ever care about download size.
- Unsigned = one SmartScreen click for users until signed. Fine for personal, worth
  fixing before showcasing.
- Nuitka would be smaller/faster to start but costs a C toolchain and long builds;
  hold it as a later optimization, not the starting point.

### Phase 0 - icon and metadata (~1 hour, do anytime)
Create `rapid-pdf.ico` (multi-size). Wire it into the running app. Decide product
name / version / publisher strings. This alone makes it feel more real and is reused by
every later phase.

### Phase 1 - first frozen build (~1-2 hours)
Build with PyInstaller in **onedir** mode from a clean PySide6-only venv. Launch the
resulting `.exe`, confirm PDFs open and all features work (windowed / no console).
This is where the PyMuPDF and Qt-plugin gotchas surface, if any. Lock the options into
a `.spec` file.

### Phase 2 - installer (~half a day, mostly first-time learning)
Write an Inno Setup `.iss` pointing at the onedir output. Configure: app name/version/
publisher, **per-user** install, Start-menu shortcut, optional desktop-shortcut
checkbox, icon, uninstaller. Compile to `rapid-pdf-setup.exe` and test install ->
launch from Start menu -> uninstall on a clean profile (or a VM).

### Phase 3 - signing (only when showcasing; ~half a day + cert/enrollment lead time)
Confirm Azure Trusted Signing eligibility from Indonesia (likely blocked for an
individual; may need a qualifying org, else fall back to an OV cert). Once you have a
cert, sign both the app exe and the setup exe, then re-test SmartScreen behavior.
Reputation warms up over the first downloads.

**End state:** double-clickable `rapid-pdf-setup.exe` that installs Rapid PDF with a
Start-menu entry, optional desktop shortcut, an app icon everywhere, and a clean
uninstall. Phases 0-2 are a focused day; phase 3 is gated on the signing decision and
enrollment.

---

## Sources

- PyInstaller vs Nuitka vs cx_Freeze (size, startup, build complexity):
  [sparxeng.com](https://sparxeng.com/blog/software/python-standalone-executable-generators-pyinstaller-nuitka-cx-freeze),
  [x321.org](https://x321.org/empirical-pyinstaller-vs-nuitka-vs-cx_freeze/),
  [coderslegacy.com](https://coderslegacy.com/nuitka-vs-pyinstaller/)
- PySide6 + PyInstaller hooks and gotchas:
  [Qt for Python deployment](https://doc.qt.io/qtforpython-6/deployment/deployment-pyinstaller.html),
  [pythonguis.com](https://www.pythonguis.com/tutorials/packaging-pyside6-applications-windows-pyinstaller-installforge/),
  [PyInstaller changelog](https://pyinstaller.org/en/stable/CHANGES.html)
- PyMuPDF + PyInstaller (onefile/console gotcha):
  [PyMuPDF issue #3981](https://github.com/pymupdf/PyMuPDF/issues/3981),
  [PyMuPDF installation docs](https://pymupdf.readthedocs.io/en/latest/installation.html)
- Inno Setup / NSIS / MSIX comparison:
  [advancedinstaller.com](https://www.advancedinstaller.com/choosing-the-right-windows-packaging-tool-as-developer.html),
  [Inno Setup [Icons] section](https://jrsoftware.org/ishelp/topic_iconssection.htm),
  [appmus.com Inno vs NSIS](https://appmus.com/vs/inno-setup-vs-nsis)
- BeeWare Briefcase (PySide6, MSI, embeddable-Python limitation):
  [Briefcase Windows platform](https://briefcase.beeware.org/en/stable/reference/platforms/windows/),
  [Qt for Python + Briefcase](https://doc.qt.io/qtforpython-6/deployment/deployment-briefcase.html)
- Azure Trusted Signing / Artifact Signing (pricing, eligibility, EV, SmartScreen):
  [Artifact Signing FAQ](https://learn.microsoft.com/en-us/azure/artifact-signing/faq),
  [Artifact Signing pricing](https://azure.microsoft.com/en-us/pricing/details/artifact-signing/),
  [Trusted Signing for individuals (Microsoft)](https://techcommunity.microsoft.com/blog/microsoft-security-blog/trusted-signing-is-now-open-for-individual-developers-to-sign-up-in-public-previ/4273554),
  [Authenticode in 2025 (text/plain)](https://textslashplain.com/2025/03/12/authenticode-in-2025-azure-trusted-signing/)

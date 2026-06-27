# rapid-pdf UI direction

> **IMPLEMENTED (Qori reskin).** The recommendation below was adopted and built:
> refined custom QSS + icon-led `qtawesome` buttons, with optional Win11 Mica.
> The one change from the prototype: the theme is now **Qori Sovereign** (cream +
> amber/gold `#F1AE04`), **light by default**, with a light/dark toggle (View menu
> or Ctrl+D). The whole look lives in a reusable module, **`ui/theme.py`**.
>
> **Reusing the theme in another app (e.g. VideoOS):** copy `ui/theme.py`, then in
> `main.py` do `theme = apply_theme(app)` and pass it to your main window. Tokens
> are named by ROLE in the `Palette` dataclass (two ship: `LIGHT` default, `DARK`),
> so re-skinning is a one-Palette edit. `ThemeManager` handles apply/toggle/persist
> and emits `theme_changed` so code-drawn surfaces (scene backgrounds, custom-drawn
> icons, item delegates) can re-tint — wire those through an `apply_palette(palette)`
> method like rapid-pdf's toolbar/organizer/page-panel do. `themed_icon(name, color)`
> wraps qtawesome with a graceful empty-icon fallback; `apply_mica(win, dark)` is a
> silent no-op off Win11.

---

# Original options report

The current toolbar buttons read as dated ("Windows XP"): flat grey raised
buttons (`#2d2d2d`), hard 1px `#444` borders, a 3px radius, and a flat `#0078d4`
blue for the active state. The app already uses a hand-rolled dark QSS theme, so
the look is fully ours to change. This doc lays out the realistic options for a
more professional, "glassy / clear-type" feel and recommends one. **The
direction is a decision waiting on Lucas** (see the prototype before choosing).

To see the proposed look next to the current one:

```
.venv\Scripts\python.exe prototypes\ui_preview.py
```

## Options

### 1. Refined custom QSS (recommended base)

Push the existing stylesheet further: subtle vertical gradients on buttons,
softer/lower-contrast borders, an 8px radius, a clearer accent blue, and an
icon + label layout. Add depth with `QGraphicsDropShadowEffect` on panels and on
the active tool (QSS itself can't draw shadows on `QPushButton`).

- **Can do:** gradients, rounded corners, per-state colors (hover/checked/
  pressed), accent system, a real glow on the active tool via a coloured drop
  shadow. Full control, no new dependencies, no risk to existing behaviour.
- **Can't do:** Qt QSS has **no transitions or animations**. Hover/checked is an
  instant state swap, never a fade. No CSS `box-shadow` on widgets (drop shadows
  must come from `QGraphicsDropShadowEffect` in code). No true blur.
- **Effort:** Low. Mostly editing the QSS strings in `ui/toolbar.py` and
  `ui/main_window.py`, plus a small shared palette.
- **Verdict:** Best value. This is the foundation; everything else stacks on top.

### 2. Theme libraries

- **pyqtdarktheme (PyQtDarkTheme):** last release Dec 2022, pins
  `python <3.12`, so it **won't install** on this app's Python 3.12. Effectively
  abandoned. Skip.
- **pyqtdarktheme-fork:** installs on 3.12 but inactive (no release in ~12
  months). Usable but no momentum.
- **qt-material (dunderlab fork):** maintained, Material Design look with many
  built-in themes via `apply_stylesheet()`.
- **QDarkStyleSheet:** mature, stable, conservative flat-dark look.

- **Can do:** instant whole-app restyle from one call.
- **Can't do:** match a specific brand look without overrides; they **replace**
  the theme, so our existing custom QSS would fight them and need re-tuning.
  Material in particular imposes its own button/ripple aesthetic that pulls away
  from "future-tech glass".
- **Effort:** Low to drop in, **medium-to-high to reconcile** with the current
  custom styling (lots of override churn).
- **Verdict:** Not worth it here. We already have a custom dark theme; a library
  would mean fighting it rather than building on it.

### 3. Frameless window + Windows 11 Mica / Acrylic ("the glassy part")

Libraries: `pywinstyles` (`apply_style(win, "mica" | "acrylic")`,
`change_header_color`, Win11 for full effect, Win10 themes only),
`win32mica`, and `PySide6-Frameless-Window` / `qframelesswindow` for a custom
title bar with rounded corners.

- **Can do:** a real Mica/acrylic backdrop on Win11, a custom title bar, rounded
  window corners. This is the genuine "modern Windows app" frame.
- **Can't do, important honesty:** the blur samples the **desktop/wallpaper
  behind the window**, not the app's own content. It is not a frosted-glass pane
  floating over the PDF. Behaviour varies by Windows build; acrylic can stutter
  while dragging; the effect needs a translucent window background to show
  through, which can complicate a content-dense editor. DWM API quirks per
  version.
- **Effort:** Medium. A frameless window means re-implementing min/max/close,
  drag-to-move, and resize edges. Small added dependency.
- **Verdict:** Strong optional **layer 2** once the QSS base looks right. Adds the
  real "glass" frame. Best done after Lucas approves the base look, and gated to
  Win11.

### 4. Custom-painted widgets (QPainter)

Subclass and `paintEvent`-draw buttons/toolbar for total control: gradients,
inner glow, animated states via `QPropertyAnimation` (which **does** give the
fades QSS can't).

- **Can do:** anything pixel-level, including smooth animated hover/press.
- **Can't do:** come for free. Every control becomes hand-maintained code.
- **Effort:** High.
- **Verdict:** Overkill for a PDF tool. Reserve for one or two hero controls
  (e.g. an animated active-tool indicator) if we want polish later, not a
  wholesale repaint.

### 5. Icon sets (quick, high-impact)

Move tool buttons from text-only to **icon + label**. Options: `qtawesome`
(maintained; bundles Font Awesome, Material Design Icons, Phosphor, Codicons),
`qt-material-icons` (Google Material Symbols for PySide), or hand-shipped Lucide
SVGs.

- **Can do:** an instant professional lift; icons read faster than text in a tool
  rail. `qtawesome` icons are font glyphs, so they recolour per state for free.
- **Can't do:** fix layout/colour on their own. Pair with option 1.
- **Effort:** Low (`qtawesome`) to trivial (a few bundled SVGs).
- **Verdict:** Adopt alongside option 1. The prototype fakes icons with QPainter
  so it runs dependency-free; the real app would use `qtawesome`.

### 6. Non-Qt escape hatch (web-tech shell)

A rewrite into Electron / Tauri / `QtWebEngine` + HTML/CSS would unlock real CSS
glassmorphism (`backdrop-filter: blur`), transitions, and modern component kits.

- **Can do:** the richest "future-tech" visuals with the least styling friction.
- **Can't do:** justify itself here. rapid-pdf is a mature PySide6 + PyMuPDF app;
  the canvas, undo stack, organizer, and annotation model are all Qt. A shell
  swap is a **multi-week rewrite** with new PDF-rendering plumbing and bigger
  binaries.
- **Effort:** Very high.
- **Verdict:** Not warranted. The Qt path reaches "professional and glassy"
  without a rewrite.

## Recommendation

**Refined custom QSS (1) + icon-led buttons (5) as the base, with Win11
Mica/acrylic (3) as an optional second layer.**

Rationale: cheapest path, zero behavioural risk, full control, and it builds on
the theme we already own instead of fighting a library. It delivers the
"clear-type, future-tech" feel through gradient surfaces, soft borders, an 8px
radius, a clearer accent, drop-shadow depth, and an accent glow on the active
tool. The genuine "glass" (Mica/acrylic frame) is a clean add-on once the base is
approved, and stays optional so non-Win11 machines degrade gracefully.

The prototype (`prototypes/ui_preview.py`) shows the BEFORE and AFTER side by
side using pure PySide6 (no new dependencies, always runs). It includes a
`MICA = True` switch that, with `pip install pywinstyles`, previews the real
Win11 backdrop and falls back silently if unavailable.

### Dependency implications if adopted in the real app

- `qtawesome`: for real icons (maintained, pip-installable).
- `pywinstyles` *(optional)*: only if we ship the Mica/acrylic layer; Win11 for
  the full effect.
- The QSS + drop-shadow work itself needs **no new dependencies**.

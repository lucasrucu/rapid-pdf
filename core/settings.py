"""Application settings: one JSON file, typed accessors, atomic writes.

Everything the app remembers between runs lives in a single file at
`%LOCALAPPDATA%\\Rapid PDF\\settings.json`. Before this module there were four
inline `QSettings("Lucas", "Rapid PDF")` constructions scattered across the UI
writing three keys into the registry, with no schema, no defaults in one place,
and nowhere to add a fifth setting without adding a fifth construction.

Three properties this store has to hold, in order of how much they matter:

1. **It can never stop the app starting.** A settings file that is truncated,
   half-written, hand-edited into nonsense, or written by a newer build must
   degrade to defaults, not raise. A corrupt file is moved aside to
   `settings.json.bad` (so it can be looked at) and defaults take over.
2. **A write is atomic.** Serialise to `settings.json.tmp`, `os.replace` it
   over the real file. Same reasoning as `core/pdf_document.save`: a rename is
   atomic on Windows and POSIX, a copy is not, and a process that dies
   mid-write must never leave a truncated file behind.
3. **Writes are debounced.** `ui/organizer.py` persists the zoom level on every
   Ctrl+wheel notch. At 250 ms of quiet a burst of twenty notches is one write,
   not twenty.

Values are read through typed section accessors (`settings().close.x_closes`),
never raw dict indexing, so a typo is an AttributeError at the call site rather
than a silent None three layers down.

Forward compatibility, in three rules:

- **Missing keys resolve to defaults at read time.** They are not written back
  and they do NOT bump `schema_version`; a file from a build with fewer
  settings is simply a file with fewer keys.
- **Unknown keys survive a write cycle.** Only the key being set is touched, so
  running an older build against a newer file does not strip it.
- **A file newer than this binary loads read-only against defaults.** The app
  runs on defaults and writes nothing, which is the only way to be sure it
  cannot damage settings it does not understand.

`MIGRATIONS` maps a from-version to the function that lifts the data one
version. It is empty at schema 1 and exists so that the second version has an
obvious place to go.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths, QTimer

# The legacy QSettings scope, kept only so the one-time migration below can
# find it. Nothing else in the app constructs a QSettings any more.
LEGACY_ORG = "Lucas"
LEGACY_APP = "Rapid PDF"

APP_DIR_NAME = "Rapid PDF"
SETTINGS_FILENAME = "settings.json"

SCHEMA_VERSION = 1

# How long after the last change before the file is written. Long enough that a
# Ctrl+wheel zoom burst collapses into one write, short enough that a crash
# right after a deliberate change loses nothing anybody would notice.
WRITE_DEBOUNCE_MS = 250

# The organizer's zoom ladder default (1.0x, index 3 of ui.organizer.ZOOM_STEPS).
# core/ must not import ui/, so the value is repeated here; test_settings.py
# asserts the two agree so they cannot drift apart silently.
_DEFAULT_ORGANIZER_ZOOM_INDEX = 3

DEFAULTS: dict = {
    "schema_version": SCHEMA_VERSION,
    "close": {
        "x_closes": "window",
        "confirm_multiple_tabs": True,
    },
    "appearance": {
        "theme": "light",
    },
    "files": {
        "default_folder_mode": "last_used",
        "default_folder": "",
    },
    "view": {
        "page_panel_visible": True,
        "default_fit_mode": "fit_page",
        "organizer_zoom_index": _DEFAULT_ORGANIZER_ZOOM_INDEX,
    },
}

# from-version -> callable(data) -> data, applied in ascending order until the
# data reaches SCHEMA_VERSION. Empty while there is only one version.
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


class _Missing:
    """Sentinel for "the file does not carry this key", distinct from None."""

    def __repr__(self):  # pragma: no cover - debugging aid only
        return "<missing>"


MISSING = _Missing()


# ---------------------------------------------------------------------------
# Where the file lives
# ---------------------------------------------------------------------------
def default_settings_dir() -> Path:
    """`%LOCALAPPDATA%\\Rapid PDF`, via Qt so the platform decides the root.

    `QStandardPaths.AppConfigLocation` folds the organization name in as its own
    directory, giving `%LOCALAPPDATA%\\Lucas\\Rapid PDF`. Nobody wants a "Lucas"
    folder in their AppData, so the org segment is dropped when it is sitting
    directly above the app segment. Only that exact shape is stripped, so a user
    whose profile directory happens to be named after the org keeps their path.
    """
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation)
    org = QCoreApplication.organizationName() or LEGACY_ORG
    app = QCoreApplication.applicationName() or APP_DIR_NAME

    parts = list(Path(base).parts) if base else []
    if len(parts) >= 2 and parts[-1] == app and parts[-2] == org:
        del parts[-2]

    if parts:
        root = Path(*parts)
    else:
        # Qt gave us nothing at all. Fall back rather than write to the cwd.
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home())

    if root.name != APP_DIR_NAME:
        root = root / APP_DIR_NAME
    return root


def default_settings_path() -> Path:
    return default_settings_dir() / SETTINGS_FILENAME


# ---------------------------------------------------------------------------
# Coercion: the file is user-editable, so nothing coming out of it is trusted
# ---------------------------------------------------------------------------
def _as_bool(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise ValueError(raw)
    if isinstance(raw, int):
        return bool(raw)
    raise TypeError(raw)


def _as_int(raw) -> int:
    if isinstance(raw, bool):
        raise TypeError(raw)
    return int(raw)


def _as_str(raw) -> str:
    if isinstance(raw, str):
        return raw
    raise TypeError(raw)


class _Field:
    """One typed setting, read through its section and validated on the way out.

    A value that is missing, of the wrong type, or outside `allowed` reads back
    as the default. That is deliberate: a hand-edited file with
    `"x_closes": "banana"` should behave like a fresh install, not crash the
    close path.
    """

    def __init__(self, coerce, allowed: tuple | None = None):
        self._coerce = coerce
        self._allowed = allowed
        self._key = ""

    def __set_name__(self, owner, name):
        self._key = name

    def _default(self, section: str):
        return DEFAULTS[section][self._key]

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        default = self._default(obj._name)
        raw = obj._store._raw_get(obj._name, self._key)
        if raw is MISSING:
            return default
        try:
            value = self._coerce(raw)
        except (TypeError, ValueError):
            return default
        if self._allowed is not None and value not in self._allowed:
            return default
        return value

    def __set__(self, obj, value):
        try:
            value = self._coerce(value)
        except (TypeError, ValueError) as err:
            raise ValueError(
                f"{obj._name}.{self._key}: {value!r} is not a valid value") from err
        if self._allowed is not None and value not in self._allowed:
            raise ValueError(
                f"{obj._name}.{self._key}: {value!r} is not one of {self._allowed}")
        obj._store._raw_set(obj._name, self._key, value)


class _Section:
    """A named group of fields, bound to the store that backs them."""

    _name = ""

    def __init__(self, store: "Settings"):
        self._store = store

    def as_dict(self) -> dict:
        """The section's effective values (file values folded onto defaults)."""
        return {key: getattr(self, key) for key in DEFAULTS[self._name]}


class CloseSection(_Section):
    _name = "close"

    # "window": the X closes the window and the app. "document": the X closes
    # the PDF and leaves an empty window, which is what the app did before.
    x_closes = _Field(_as_str, allowed=("window", "document"))
    confirm_multiple_tabs = _Field(_as_bool)


class AppearanceSection(_Section):
    _name = "appearance"

    theme = _Field(_as_str, allowed=("light", "dark"))


class FilesSection(_Section):
    _name = "files"

    default_folder_mode = _Field(_as_str, allowed=("last_used", "fixed"))
    default_folder = _Field(_as_str)


class ViewSection(_Section):
    _name = "view"

    page_panel_visible = _Field(_as_bool)
    # Every mode the status-bar fit group offers. `fit_height` was missing from
    # this tuple while ui.canvas.FIT_MODES has carried it all along, so choosing
    # Fit height and setting it as the default silently fell back to fit_page.
    # test_preferences.py asserts the two sets stay equal.
    default_fit_mode = _Field(
        _as_str, allowed=("fit_page", "fit_width", "fit_height", "actual"))
    organizer_zoom_index = _Field(_as_int)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class Settings:
    """The settings file, loaded once and written back on a debounce.

    Construct one and hold it (see `settings()` for the app-wide instance).
    Reads are cheap dict lookups; writes mark the store dirty and arm a timer.
    """

    def __init__(self, path: str | os.PathLike | None = None, *,
                 debounce_ms: int = WRITE_DEBOUNCE_MS,
                 legacy_settings: Callable[[], QSettings] | None = None,
                 migrate_legacy: bool = True):
        self._path = Path(path) if path is not None else default_settings_path()
        self._debounce_ms = debounce_ms
        self._legacy_settings = legacy_settings or (
            lambda: QSettings(LEGACY_ORG, LEGACY_APP))
        self._data: dict = {}
        self._read_only = False
        self._dirty = False
        self._timer: QTimer | None = None

        self.close = CloseSection(self)
        self.appearance = AppearanceSection(self)
        self.files = FilesSection(self)
        self.view = ViewSection(self)

        self.load(migrate_legacy=migrate_legacy)

    # -- state -----------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def read_only(self) -> bool:
        """True when the file was written by a newer build than this one."""
        return self._read_only

    @property
    def schema_version(self) -> int:
        raw = self._data.get("schema_version", SCHEMA_VERSION)
        try:
            return _as_int(raw)
        except (TypeError, ValueError):
            return SCHEMA_VERSION

    def as_dict(self) -> dict:
        """Effective values for every section, defaults folded in."""
        out = {"schema_version": self.schema_version}
        for section in (self.close, self.appearance, self.files, self.view):
            out[section._name] = section.as_dict()
        return out

    def raw(self) -> dict:
        """The literal file contents, unknown keys and all. For tests."""
        return self._data

    # -- loading ---------------------------------------------------------
    def load(self, *, migrate_legacy: bool = True) -> None:
        """Read the file. Never raises: every failure lands on defaults."""
        self._read_only = False
        self._dirty = False

        if not self._path.exists():
            self._data = {}
            if migrate_legacy:
                self._migrate_from_qsettings()
            return

        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("settings file is not a JSON object")
        except Exception as err:
            # Truncated, hand-mangled, or not JSON at all. Keep it for a
            # post-mortem, then carry on as a fresh install. The one thing that
            # must not happen here is an exception reaching main().
            print(f"Settings unreadable ({err}); using defaults")
            self._quarantine()
            self._data = {}
            return

        version = data.get("schema_version", SCHEMA_VERSION)
        try:
            version = _as_int(version)
        except (TypeError, ValueError):
            version = SCHEMA_VERSION

        if version > SCHEMA_VERSION:
            # Written by a newer build. Run on defaults and touch nothing: a
            # write from here would drop whatever that build understands.
            print(f"Settings schema {version} is newer than {SCHEMA_VERSION}; "
                  f"running on defaults, no changes will be saved")
            self._data = data
            self._read_only = True
            return

        if version < SCHEMA_VERSION:
            data = self._apply_migrations(data, version)

        self._data = data

    def _apply_migrations(self, data: dict, version: int) -> dict:
        """Walk the data up to SCHEMA_VERSION one step at a time."""
        while version < SCHEMA_VERSION:
            step = MIGRATIONS.get(version)
            if step is None:
                # No path from here. Rather than guess, take the defaults and
                # keep whatever the file had so nothing is silently discarded.
                print(f"No settings migration from schema {version}; using defaults")
                break
            try:
                data = step(data)
            except Exception as err:
                print(f"Settings migration from schema {version} failed ({err})")
                break
            version += 1
            data["schema_version"] = version
            self._dirty = True
        return data

    def _quarantine(self) -> None:
        """Move an unreadable file aside so the next run starts clean."""
        try:
            bad = self._path.with_name(self._path.name + ".bad")
            os.replace(self._path, bad)
        except Exception as err:
            print(f"Could not set aside the bad settings file: {err}")
            try:
                self._path.unlink()
            except Exception:
                pass

    # -- the one-time QSettings migration --------------------------------
    def _migrate_from_qsettings(self) -> bool:
        """First run with no settings.json: fold the three legacy keys in.

        Rapid PDF up to 1.5.0 kept `theme/mode`, `ui/page_panel_visible` and
        `ui/organizer_zoom_index` in `HKCU\\Software\\Lucas\\Rapid PDF`. Read
        them once through QSettings, write them into the new file, and leave the
        registry key exactly where it is: an older build downgraded onto the
        same machine still needs it, and deleting user data to tidy up is never
        worth it.

        Returns whether anything was carried over.
        """
        try:
            legacy = self._legacy_settings()
            keys = set(legacy.allKeys())
        except Exception as err:
            print(f"Legacy settings unreadable ({err}); starting from defaults")
            return False
        if not keys:
            return False

        carried = False

        if "theme/mode" in keys:
            raw = legacy.value("theme/mode")
            if isinstance(raw, str) and raw in ("light", "dark"):
                self.appearance.theme = raw
                carried = True

        if "ui/page_panel_visible" in keys:
            raw = legacy.value("ui/page_panel_visible")
            try:
                # QSettings hands back "true"/"false" strings on some backends.
                self.view.page_panel_visible = _as_bool(raw)
                carried = True
            except (TypeError, ValueError):
                pass

        if "ui/organizer_zoom_index" in keys:
            raw = legacy.value("ui/organizer_zoom_index")
            try:
                self.view.organizer_zoom_index = int(raw)
                carried = True
            except (TypeError, ValueError):
                pass

        # Write the file now rather than on the debounce: the whole point is
        # that the next run reads JSON instead of the registry, and the next
        # run may be after a crash that never let the timer fire.
        self._dirty = True
        self.flush()
        return carried

    # -- raw access, used only by _Field ---------------------------------
    def _raw_get(self, section: str, key: str):
        if self._read_only:
            return MISSING
        block = self._data.get(section)
        if not isinstance(block, dict):
            return MISSING
        return block.get(key, MISSING)

    def _raw_set(self, section: str, key: str, value) -> None:
        if self._read_only:
            return
        block = self._data.get(section)
        if not isinstance(block, dict):
            block = {}
            self._data[section] = block
        current = block.get(key, MISSING)
        if current is not MISSING and type(current) is type(value) and current == value:
            return                      # no change, no write
        block[key] = value
        self._dirty = True
        self._schedule_write()

    # -- writing ---------------------------------------------------------
    def _schedule_write(self) -> None:
        """Arm the debounce, or write straight through if there is no event loop.

        A QTimer without a QCoreApplication never fires, so a headless caller
        (a test, a CLI tool) gets an immediate write instead of a silent drop.
        """
        if self._debounce_ms <= 0 or QCoreApplication.instance() is None:
            self.flush()
            return
        if self._timer is None:
            self._timer = QTimer()
            self._timer.setSingleShot(True)
            self._timer.setInterval(self._debounce_ms)
            self._timer.timeout.connect(self.flush)
        self._timer.start()

    def flush(self) -> bool:
        """Write the file now if anything changed. Returns whether it wrote.

        Atomic, and silent on failure: a settings write that cannot happen (a
        read-only profile, a full disk) is not worth a dialog, and definitely
        not worth an exception escaping into a close handler.
        """
        if self._timer is not None:
            self._timer.stop()
        if self._read_only or not self._dirty:
            return False

        self._data.setdefault("schema_version", SCHEMA_VERSION)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            # os.replace is atomic and overwrites on Windows and POSIX alike,
            # so a reader never sees a half-written file. Same call, and the
            # same reasoning, as the in-place PDF save.
            os.replace(tmp_path, self._path)
            self._dirty = False
            return True
        except Exception as err:
            print(f"Settings write failed: {err}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return False


# ---------------------------------------------------------------------------
# The app-wide instance
# ---------------------------------------------------------------------------
_INSTANCE: Settings | None = None


def settings() -> Settings:
    """The one store the app reads and writes. Built on first use."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Settings()
    return _INSTANCE


def set_settings(store: Settings | None) -> Settings | None:
    """Swap the app-wide store (tests point it at a throwaway path).

    Returns the store that was in place, so a caller can put it back.
    """
    global _INSTANCE
    previous = _INSTANCE
    _INSTANCE = store
    return previous


# ---------------------------------------------------------------------------
# Where a file dialog opens
# ---------------------------------------------------------------------------
# Every QFileDialog in the app used to be handed "" as its starting directory,
# which is not "no preference": Qt reads it as the process working directory,
# so a shortcut launched from the desktop opened the Open dialog on the
# desktop and one launched from Explorer opened it wherever Explorer happened
# to be. These two functions are the whole of the fix, and every dialog goes
# through them.
#
# ONE KEY, TWO MODES, AND WHY. `files.default_folder` holds the folder the
# dialogs start in. In "last_used" mode the app rewrites it after every dialog;
# in "fixed" mode only the user writes it, through Preferences. That means a
# fixed folder is overwritten if the user switches to last_used and then opens
# something, which is the price of not carrying a second key for a value that
# is the same thing either way: the folder to start in. Remembering it in the
# file rather than in memory is what makes the FIRST dialog of a run land
# somewhere sensible, which is the whole complaint.
def dialog_start_dir(fallback: str | os.PathLike | None = None) -> str:
    """The directory a file dialog should open in, or "" for Qt's own default.

    `fallback` is the caller's better guess when nothing is remembered yet,
    typically the open document's own path. A file path is accepted and its
    parent used, so callers do not each have to work that out.
    """
    remembered = settings().files.default_folder
    if remembered:
        # Strictly a directory. A remembered folder that has gone (an unplugged
        # drive, a network share that is down) falls through to the caller's
        # guess rather than up to its parent, which would be a folder the user
        # never chose.
        try:
            if Path(remembered).is_dir():
                return str(remembered)
        except OSError:
            pass
    if fallback:
        try:
            path = Path(fallback)
            directory = path if path.is_dir() else path.parent
            if directory.is_dir():
                return str(directory)
        except OSError:
            pass
    return ""


def remember_dialog_dir(path: str | os.PathLike | None) -> None:
    """Record where a dialog just landed. A no-op unless the mode says to.

    Silent on anything unusable: a dialog that was cancelled, a path on a
    drive that has since gone, a store that cannot be written. None of it is
    worth interrupting the thing the user was actually doing.
    """
    if not path:
        return
    store = settings()
    if store.files.default_folder_mode != "last_used":
        return
    try:
        candidate = Path(path)
        directory = candidate if candidate.is_dir() else candidate.parent
        if not directory.is_dir():
            return
        store.files.default_folder = str(directory)
    except (OSError, ValueError):
        return

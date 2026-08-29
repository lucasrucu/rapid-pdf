"""Measure what a backgrounded tab actually gives back. Phase 3, docs/tabs-plan.md.

Phase 2 measured the cost of open tabs and found the estimate had been right
about the ceiling and wrong about where the weight is: ten live fitz documents
plus twenty markup-baked clones came to 2 MB, while ONE document's six-entry
pixmap cache came to 207 MB. That is what made "drop a backgrounded document's
render cache" the first thing phase 3 does rather than the last.

This measures the result. It opens N A1 drawings as tabs, renders each one
while it is in front so its cache actually fills, and reads the Windows working
set at the end. It runs itself twice as subprocesses, because memory does not
come back cleanly enough to measure two configurations in one process:

    hold     `DocumentView._on_backgrounded` patched to do nothing, which is
             how the app behaved before this phase
    release  as shipped

Working set via GetProcessMemoryInfo, the same counter phase 2 used, so the
numbers are comparable to the table in the plan. It is a noisy counter (the
allocator returns pages to Windows on its own schedule), so treat the
difference as an order of magnitude rather than a figure to quote to three
places.

    .venv\\Scripts\\python tools\\measure_tab_memory.py [tabs]
"""

import ctypes
import ctypes.wintypes
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A1 portrait, the size the plan measured. The page size is the whole point:
# a rendered A1 page at zoom 1.5 is 2526 x 3576 px, which is about 34.5 MB as a
# 32-bit QPixmap, and six of those is the cache one document can hold.
A1_W_PT, A1_H_PT = 1684, 2384

DEFAULT_TABS = 6

# core.pdf_document.RENDER_CACHE_MAX. Each document gets this many pages and
# every one of them is turned to, so the cache under measurement is a FULL one.
CACHE_MAX = 6


class _Counters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def working_set_mb() -> float:
    # argtypes and restype spelled out. Without them ctypes passes the process
    # pseudo-handle as a 32-bit int on a 64-bit build and the call just fails,
    # silently, returning zero.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
    psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.wintypes.HANDLE, ctypes.POINTER(_Counters), ctypes.wintypes.DWORD]

    counters = _Counters()
    counters.cb = ctypes.sizeof(_Counters)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                      ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.WorkingSetSize / (1024 * 1024)


def measure(mode: str, tabs: int) -> float:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    import fitz
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    from core.settings import Settings, set_settings
    from ui.document_view import DocumentView
    from ui.main_window import MainWindow

    folder = tempfile.mkdtemp()
    store = Settings(os.path.join(folder, "settings.json"),
                     debounce_ms=0, migrate_legacy=False)
    store.close.confirm_multiple_tabs = False
    set_settings(store)

    if mode == "hold":
        # How the app behaved before phase 3: a backgrounded tab kept
        # everything it had built.
        DocumentView._on_backgrounded = lambda self: None

    paths = []
    for i in range(tabs):
        path = os.path.join(folder, f"drawing{i}.pdf")
        raw = fitz.open()
        for p in range(CACHE_MAX):
            page = raw.new_page(width=A1_W_PT, height=A1_H_PT)
            page.insert_text((80, 300), f"drawing {i} page {p}", fontsize=72)
        raw.save(path)
        raw.close()
        paths.append(path)

    window = MainWindow()
    window.resize(1600, 1000)
    window.show()
    baseline = working_set_mb()

    for path in paths:
        window.open_paths([path])
        view = window.view
        # THE PAGE TURNS ARE THE POINT, not decoration. A tab where only one
        # page has ever been rendered holds exactly one cached pixmap, and the
        # canvas scene holds that same QPixmap as its background item, so
        # dropping the cache frees nothing: QPixmap is implicitly shared and
        # the scene is still holding the data. The 207 MB in the plan is a FULL
        # six-entry cache, which is what reading through a drawing produces, so
        # that is what gets measured.
        for page_num in range(CACHE_MAX):
            view.jump_to_page(page_num)
            view._canvas._flush_pending_render()

    after = working_set_mb()
    print(f"{mode}\t{baseline:.1f}\t{after:.1f}\t{after - baseline:.1f}")

    for view in window.document_area().views():
        view.mark_clean()
    window._force_quit = True
    window.close()
    app.quit()
    return after - baseline


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--mode":
        measure(sys.argv[2], int(sys.argv[3]))
        return 0

    tabs = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TABS
    print(f"{tabs} A1 tabs ({A1_W_PT} x {A1_H_PT} pt), "
          f"{CACHE_MAX} pages turned to in each so every cache is full")
    print("mode\tbaseline\tafter\tgrowth (MB)")
    results = {}
    for mode in ("hold", "release"):
        out = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--mode", mode, str(tabs)],
            capture_output=True, text=True)
        line = [ln for ln in out.stdout.splitlines() if ln.startswith(mode)]
        if not line:
            print(out.stdout, out.stderr)
            return 1
        print(line[0])
        results[mode] = float(line[0].split("\t")[-1])

    saved = results["hold"] - results["release"]
    print(f"\nreleasing backgrounded tabs saves {saved:.1f} MB across {tabs} tabs")
    print(f"about {saved / max(1, tabs - 1):.1f} MB per backgrounded tab")
    return 0


if __name__ == "__main__":
    sys.exit(main())

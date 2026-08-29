import {
  ArrowLeftRight,
  Copy,
  Hand,
  Highlighter,
  History,
  LayoutGrid,
  MousePointer2,
  PanelsTopLeft,
  Settings,
  Image as ImageIcon,
  Save,
  Zap,
} from "lucide-react";

import { Eyebrow } from "@/components/Eyebrow";

const FEATURES = [
  {
    icon: PanelsTopLeft,
    title: "Document tabs",
    body: "Several PDFs open at once, one tab each. A second file gets its own tab instead of having its pages appended to the one you are reading. Drop a PDF on the window and it opens there too.",
  },
  {
    icon: Copy,
    title: "Several windows",
    body: "Ctrl+Shift+N for a new one, or drag a tab off the bar and it tears into a window that follows your cursor. Drop it on another window's tab bar to dock it there.",
  },
  {
    icon: ArrowLeftRight,
    title: "Pages between tabs",
    body: "Drag a page out of one document and into another in the same window, from the thumbnail strip or the Organizer. Hold Ctrl at the drop to copy instead of move. Unsaved markup travels with it.",
  },
  {
    icon: Hand,
    title: "Pan and fit",
    body: "Press H for the pan tool, hold Space, or drag with the middle button. Fit page, fit width, fit height, and 100% sit as four icons in the status bar.",
  },
  {
    icon: LayoutGrid,
    title: "Page manager",
    body: "Open, combine, reorder, delete, and add pages from a thumbnail grid. Drag to reorder, double-click to jump into the canvas.",
  },
  {
    icon: Highlighter,
    title: "Markup tools",
    body: "Highlight, rectangle, and line annotations with an Office-style color picker, opacity presets, and line weights.",
  },
  {
    icon: MousePointer2,
    title: "Object editing",
    body: "Select, move, and resize with 8-point handles. Ctrl+drag to duplicate, marquee group-select, copy/paste, full undo/redo.",
  },
  {
    icon: ImageIcon,
    title: "Embedded-image lift",
    body: "Grab an image baked into the page and move or resize it like any other object, with no white hole left behind.",
  },
  {
    icon: Save,
    title: "Faithful saves",
    body: "Markup is written as PDF-spec annotation objects on the original page. Nothing is re-encoded, resized, or clipped.",
  },
  {
    icon: History,
    title: "Picks up where you left off",
    body: "Turn on session restore under Startup in Preferences and last run's windows and tabs come back, on the screen, page, and zoom you left them. Each one is read when you first look at it.",
  },
  {
    icon: Settings,
    title: "Preferences",
    body: "Ctrl+, opens the lot on one page: theme, startup, where file dialogs open, what the X button does, and the fit a document opens at. Every control applies as you touch it.",
  },
  {
    icon: Zap,
    title: "Instant, always",
    body: "Nothing scans on open, so even a large A1 engineering drawing loads and edits without the wait. Need searchable scans? Run Enhance for Search whenever you choose.",
  },
];

export function Features() {
  return (
    <section id="features" className="border-b border-border">
      <div className="mx-auto max-w-5xl px-6 py-20">
        <div className="max-w-2xl">
          <Eyebrow>What it does</Eyebrow>
          <h2 className="mt-6 text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
            The two things that matter, done instantly.
          </h2>
          <p className="mt-4 text-lg text-muted-foreground">
            Acrobat runs OCR and form-field detection every time it opens a file. Rapid PDF skips all
            of it and does only what field work needs: reorganize pages and add markup, now across
            as many documents as you have open.
          </p>
        </div>

        <div className="mt-12 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f) => (
            <div
              key={f.title}
              className="rounded-xl border border-border bg-card p-6 transition-colors hover:bg-accent/40"
            >
              <span className="inline-flex h-10 w-10 items-center justify-center rounded-md bg-primary/15 text-foreground">
                <f.icon className="h-5 w-5" strokeWidth={2} aria-hidden="true" />
              </span>
              <h3 className="mt-4 text-base font-semibold tracking-tight text-foreground">
                {f.title}
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{f.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

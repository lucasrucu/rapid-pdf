import {
  Highlighter,
  LayoutGrid,
  MousePointer2,
  Image as ImageIcon,
  Save,
  Zap,
} from "lucide-react";

import { Eyebrow } from "@/components/Eyebrow";

const FEATURES = [
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
    icon: Zap,
    title: "Instant, always",
    body: "No OCR, no form-field scanning on open. Even a large A1 engineering drawing loads and edits without the wait.",
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
            of it and does only what field work needs: reorganize pages and add markup.
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

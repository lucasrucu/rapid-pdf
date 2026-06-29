import { AppWindow } from "@/components/AppWindow";
import { Eyebrow } from "@/components/Eyebrow";
import { OrganizerDemo } from "@/components/OrganizerDemo";

export function Screens() {
  return (
    <section id="screens" className="border-b border-border bg-secondary/40">
      <div className="mx-auto max-w-5xl px-6 py-20">
        <div className="max-w-2xl">
          <Eyebrow>A look inside</Eyebrow>
          <h2 className="mt-6 text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
            Two tabs. Nothing in your way.
          </h2>
        </div>

        <div className="mt-12 grid gap-10 lg:grid-cols-2">
          {/* Editor: real screenshot in a clean window frame. */}
          <figure className="flex flex-col">
            <div className="relative rounded-2xl bg-gradient-to-b from-accent/40 to-card/0 p-3 sm:p-4">
              <AppWindow
                src="/shots/editor-selected.png"
                alt="Editor with the toolbar, color picker, and a page selected in the thumbnail strip"
                title="Rapid PDF · Editor"
              />
            </div>
            <figcaption className="mt-4">
              <h3 className="text-base font-semibold tracking-tight text-foreground">Editor</h3>
              <p className="mt-1 text-sm text-muted-foreground">
                Toolbar on the right, thumbnail strip on the left, the page in the middle. Pick a
                tool and draw.
              </p>
            </figcaption>
          </figure>

          {/* Organizer: a live CSS demo of drag-to-reorder, in the same frame. */}
          <figure className="flex flex-col">
            <div className="relative rounded-2xl bg-gradient-to-b from-accent/40 to-card/0 p-3 sm:p-4">
              <div className="overflow-hidden rounded-xl border border-border bg-card shadow-[0_1px_2px_rgba(25,20,12,0.04),0_12px_28px_-8px_rgba(25,20,12,0.18),0_2px_8px_-2px_rgba(25,20,12,0.10)]">
                <div className="flex items-center gap-2 border-b border-border bg-secondary/70 px-3.5 py-2.5">
                  <span className="flex items-center gap-1.5" aria-hidden="true">
                    <span className="h-3 w-3 rounded-full bg-[#ff5f57]" />
                    <span className="h-3 w-3 rounded-full bg-[#febc2e]" />
                    <span className="h-3 w-3 rounded-full bg-[#28c840]" />
                  </span>
                  <span className="ml-1 truncate text-xs font-medium text-muted-foreground">
                    Rapid PDF · Organizer
                  </span>
                </div>
                <div className="flex min-h-[260px] items-center justify-center px-4 py-10">
                  <OrganizerDemo />
                </div>
              </div>
            </div>
            <figcaption className="mt-4">
              <h3 className="text-base font-semibold tracking-tight text-foreground">Organizer</h3>
              <p className="mt-1 text-sm text-muted-foreground">
                Every page as a card. Drag to reorder, select to delete, add pages from another PDF.
              </p>
            </figcaption>
          </figure>
        </div>
      </div>
    </section>
  );
}

import Image from "next/image";

import { Eyebrow } from "@/components/Eyebrow";

const SCREENS = [
  {
    src: "/shots/editor-selected.png",
    alt: "Editor with the toolbar, color picker, and a page selected in the thumbnail strip",
    title: "Editor",
    body: "Toolbar on the right, thumbnail strip on the left, the page in the middle. Pick a tool and draw.",
  },
  {
    src: "/shots/organizer-light.png",
    alt: "Organizer grid showing every page as a draggable card",
    title: "Organizer",
    body: "Every page as a card. Drag to reorder, select to delete, add pages from another PDF.",
  },
];

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

        <div className="mt-12 grid gap-8 lg:grid-cols-2">
          {SCREENS.map((s) => (
            <figure key={s.src} className="flex flex-col">
              <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
                <Image
                  src={s.src}
                  alt={s.alt}
                  width={1500}
                  height={952}
                  className="h-auto w-full"
                />
              </div>
              <figcaption className="mt-4">
                <h3 className="text-base font-semibold tracking-tight text-foreground">{s.title}</h3>
                <p className="mt-1 text-sm text-muted-foreground">{s.body}</p>
              </figcaption>
            </figure>
          ))}
        </div>
      </div>
    </section>
  );
}

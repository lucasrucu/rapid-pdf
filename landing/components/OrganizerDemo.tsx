/**
 * Lightweight, looping CSS demo of the Organizer's drag-to-reorder. No JS, no
 * GIF: a page card lifts, slides across two slots, and drops while the others
 * shuffle back to fill the gap. Sits inside an AppWindow chrome for context.
 * Respects prefers-reduced-motion (animations pause to a static layout).
 */
function PageCard({
  label,
  selected = false,
}: {
  label: string;
  selected?: boolean;
}) {
  return (
    <div className="flex flex-col items-center gap-1.5">
      <div
        className={
          "flex h-[88px] w-[68px] flex-col gap-1 rounded-md border bg-card p-1.5 shadow-sm " +
          (selected ? "border-primary ring-2 ring-primary/30" : "border-border")
        }
      >
        <div className="h-1.5 w-3/4 rounded-sm bg-muted-foreground/40" />
        <div className="mt-0.5 h-px w-full bg-border" />
        <div className="h-1 w-full rounded-sm bg-muted-foreground/20" />
        <div className="h-1 w-5/6 rounded-sm bg-muted-foreground/20" />
        <div className="h-1 w-full rounded-sm bg-muted-foreground/20" />
        <div className="h-1 w-2/3 rounded-sm bg-muted-foreground/20" />
      </div>
      <span className="text-[10px] font-medium text-muted-foreground">{label}</span>
    </div>
  );
}

export function OrganizerDemo() {
  return (
    <div className="organizer-demo relative isolate">
      {/* Hint label, fades in with the lift */}
      <div className="mb-4 flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
          Drag to reorder
        </span>
        <span className="od-cursorhint inline-flex h-2 w-2 rounded-full bg-primary" aria-hidden="true" />
      </div>

      <div className="flex items-start justify-center gap-3 sm:gap-5">
        {/* The moving card: lifts, travels right two slots, drops. */}
        <div className="od-mover">
          <PageCard label="Page 1" selected />
        </div>
        {/* These two slide left to fill the gap as the mover passes. */}
        <div className="od-shift">
          <PageCard label="Page 2" />
        </div>
        <div className="od-shift">
          <PageCard label="Page 3" />
        </div>
        <div>
          <PageCard label="Page 4" />
        </div>
      </div>
    </div>
  );
}

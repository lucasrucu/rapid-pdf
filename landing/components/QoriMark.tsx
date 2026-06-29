/* Qori lockup — same geometry as the umbrella mark across every Qori property.
   Solid amber tile + dark glyph. rapid-pdf uses a file glyph; the tile and
   wordmark style stay identical to the hub, financial dashboard, etc. */

import { FileText } from "lucide-react";

export function QoriMark({
  label,
  size = 32,
}: {
  label?: string;
  size?: number;
}) {
  return (
    <span className="inline-flex items-center gap-2.5">
      <span
        className="inline-flex shrink-0 items-center justify-center rounded-[0.34em] bg-primary text-primary-foreground"
        style={{ width: size, height: size }}
        aria-hidden={label ? true : undefined}
      >
        <FileText size={Math.round(size * 0.5)} strokeWidth={2.25} />
      </span>
      {label ? (
        <span className="text-base font-medium tracking-tight text-foreground">{label}</span>
      ) : null}
    </span>
  );
}

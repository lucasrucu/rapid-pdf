import Image from "next/image";

import { cn } from "@/lib/utils";

/**
 * Wraps a real app screenshot in a clean desktop-window frame: a title bar with
 * traffic-light dots, a soft layered shadow, and a rounded body. Turns a raw
 * screen grab into a polished product shot. Pure CSS, no runtime cost.
 */
export function AppWindow({
  src,
  alt,
  title,
  width = 1500,
  height = 976,
  priority = false,
  className,
  imgClassName,
}: {
  src: string;
  alt: string;
  title?: string;
  width?: number;
  height?: number;
  priority?: boolean;
  className?: string;
  imgClassName?: string;
}) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-xl border border-border bg-card",
        "shadow-[0_1px_2px_rgba(25,20,12,0.04),0_12px_28px_-8px_rgba(25,20,12,0.18),0_2px_8px_-2px_rgba(25,20,12,0.10)]",
        className,
      )}
    >
      {/* Title bar */}
      <div className="flex items-center gap-2 border-b border-border bg-secondary/70 px-3.5 py-2.5">
        <span className="flex items-center gap-1.5" aria-hidden="true">
          <span className="h-3 w-3 rounded-full bg-[#ff5f57]" />
          <span className="h-3 w-3 rounded-full bg-[#febc2e]" />
          <span className="h-3 w-3 rounded-full bg-[#28c840]" />
        </span>
        <span className="ml-1 truncate text-xs font-medium text-muted-foreground">
          {title ?? "Rapid PDF"}
        </span>
      </div>

      {/* Screenshot */}
      <Image
        src={src}
        alt={alt}
        width={width}
        height={height}
        priority={priority}
        className={cn("h-auto w-full", imgClassName)}
      />
    </div>
  );
}

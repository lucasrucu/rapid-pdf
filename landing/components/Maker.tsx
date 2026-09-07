import { ArrowUpRight, Linkedin, Mail } from "lucide-react";

import { Eyebrow } from "@/components/Eyebrow";
import { MAKER } from "@/lib/site";

/* The maker band. Sits between the download CTA and the footer, so it lands
   right where a visitor who already wants the tool stops reading.

   It is deliberately the second loudest thing on the page. The product sells
   first; this is how somebody who liked it finds the person who made it. The
   amber monogram tile borrows QoriMark's geometry so it reads as part of the
   same family rather than a bolted-on badge. */
export function Maker() {
  return (
    <section id="maker">
      <div className="mx-auto max-w-5xl px-6 py-16 sm:py-20">
        <div className="relative overflow-hidden rounded-2xl border border-border bg-gradient-to-br from-accent/60 via-card to-secondary/40 p-8 sm:p-10">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -right-12 -top-16 h-48 w-48 rounded-full bg-primary/25 blur-3xl"
          />

          <div className="relative">
            <Eyebrow>Who made this</Eyebrow>

            <div className="mt-6 flex flex-col gap-7 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex items-center gap-4">
                <span
                  className="inline-flex h-14 w-14 shrink-0 items-center justify-center rounded-[0.34em] bg-primary text-lg font-semibold tracking-tight text-primary-foreground"
                  aria-hidden="true"
                >
                  {MAKER.initials}
                </span>
                <div>
                  <p className="text-xl font-semibold tracking-tight text-foreground sm:text-2xl">
                    Built by {MAKER.name}
                  </p>
                  <p className="mt-1 max-w-md text-sm text-muted-foreground">{MAKER.blurb}</p>
                </div>
              </div>

              <div className="flex shrink-0 flex-wrap items-center gap-2.5">
                <a
                  href={MAKER.site}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
                >
                  See more of my work
                  <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
                </a>
                <a
                  href={`mailto:${MAKER.email}`}
                  className="inline-flex items-center justify-center gap-2 rounded-md border border-border bg-card px-5 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-accent"
                >
                  <Mail className="h-4 w-4" aria-hidden="true" />
                  Email me
                </a>
                <a
                  href={MAKER.linkedin}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`${MAKER.name} on LinkedIn`}
                  className="inline-flex items-center justify-center rounded-md border border-border bg-card p-2.5 text-foreground transition-colors hover:bg-accent"
                >
                  <Linkedin className="h-4 w-4" aria-hidden="true" />
                </a>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

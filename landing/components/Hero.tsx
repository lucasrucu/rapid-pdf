"use client";

import { useState } from "react";
import { Download, Monitor, Moon, Sun } from "lucide-react";

import { AppWindow } from "@/components/AppWindow";
import { Eyebrow } from "@/components/Eyebrow";
import { GitHubIcon } from "@/components/BrandIcons";
import { LINKS, MAKER, SITE } from "@/lib/site";

type Mode = "light" | "dark";

const SHOT: Record<Mode, { src: string; alt: string }> = {
  light: {
    src: "/shots/editor-light.png",
    alt: "Rapid PDF editor in light theme, marking up a sample document",
  },
  dark: {
    src: "/shots/editor-dark.png",
    alt: "Rapid PDF editor in dark theme, marking up a sample document",
  },
};

export function Hero() {
  const [mode, setMode] = useState<Mode>("light");
  const shot = SHOT[mode];

  return (
    <section id="top" className="border-b border-border">
      <div className="mx-auto max-w-5xl px-6 py-20 sm:py-28">
        <div className="mx-auto max-w-3xl text-center">
          <div className="flex justify-center">
            <Eyebrow>Windows desktop · v{SITE.version}</Eyebrow>
          </div>
          <h1 className="mt-6 text-balance text-4xl font-semibold tracking-tight text-foreground sm:text-6xl">
            {SITE.tagline}
          </h1>
          <p className="mx-auto mt-6 max-w-xl text-lg text-muted-foreground">{SITE.description}</p>

          <div className="mt-9 flex flex-wrap items-center justify-center gap-3">
            <a
              href={LINKS.downloadInstaller}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
            >
              <Download className="h-4 w-4" aria-hidden="true" />
              Download for Windows
            </a>
            <a
              href={LINKS.github}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center justify-center gap-2 rounded-md border border-border bg-card px-5 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-accent"
            >
              <GitHubIcon className="h-4 w-4" />
              View on GitHub
            </a>
          </div>
          {/* Every claim on this line has to be checkable, and the "open source"
              half was not for one day. It came off on the morning of 2026-09-09
              because the repo carried no LICENSE, which made it misleading
              rather than merely unsupported, and it went back on the same day
              once RapidPDF was licensed under AGPL-3.0 (LICENSE at the repo
              root, a Licence section in the README, the reasoning in
              docs/licensing-decision.md). It is true again and it stays.

              THE PRIVACY HALF IS THE ONE THAT SELLS, so it keeps the end of the
              line where the eye stops. The app talks to the network in exactly
              one place, core/update/feed.py, and only to ask GitHub about its
              own releases. No document ever leaves the machine, OCR included,
              because PyMuPDF runs Tesseract locally. "Open source" is what lets
              a sceptical reader go and CHECK that, which is why the two belong
              on one line and not on two.

              If the licence ever changes, this line changes with it. */}
          <p className="mt-3 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <Monitor className="h-3.5 w-3.5" aria-hidden="true" />
            Windows 10 / 11 · free and open source · your files never leave your PC
          </p>
          {/* Byline. Near the fold on a phone, so the tool has a name on it
              before anyone scrolls. Jumps to the maker band at the bottom. */}
          <p className="mt-4 text-sm text-muted-foreground">
            Built by{" "}
            <a
              href="#maker"
              className="font-medium text-foreground decoration-primary decoration-2 underline-offset-4 hover:underline"
            >
              {MAKER.name}
            </a>
          </p>
        </div>

        {/* Interactive product shot: toggle the app theme to preview both looks. */}
        <div className="mx-auto mt-14 max-w-4xl">
          <div className="mb-4 flex items-center justify-center gap-1 text-sm">
            <span className="mr-1 text-muted-foreground">Preview theme</span>
            <div className="inline-flex rounded-md border border-border bg-card p-0.5">
              <button
                type="button"
                onClick={() => setMode("light")}
                aria-pressed={mode === "light"}
                className={`inline-flex items-center gap-1.5 rounded-[calc(var(--radius)-4px)] px-3 py-1.5 text-xs font-medium transition-colors ${
                  mode === "light"
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <Sun className="h-3.5 w-3.5" /> Light
              </button>
              <button
                type="button"
                onClick={() => setMode("dark")}
                aria-pressed={mode === "dark"}
                className={`inline-flex items-center gap-1.5 rounded-[calc(var(--radius)-4px)] px-3 py-1.5 text-xs font-medium transition-colors ${
                  mode === "dark"
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <Moon className="h-3.5 w-3.5" /> Dark
              </button>
            </div>
          </div>

          {/* Brand backdrop: soft amber glow + cream gradient behind the window. */}
          <div className="relative rounded-2xl bg-gradient-to-b from-accent/50 to-secondary/30 p-3 sm:p-6">
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-x-8 -top-6 h-24 rounded-full bg-primary/20 blur-3xl"
            />
            <AppWindow
              key={shot.src}
              src={shot.src}
              alt={shot.alt}
              title="Rapid PDF · Editor"
              priority
              className="animate-in fade-in duration-300"
            />
          </div>
        </div>
      </div>
    </section>
  );
}

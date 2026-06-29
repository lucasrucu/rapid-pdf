import { Download, Package, ShieldCheck } from "lucide-react";

import { Eyebrow } from "@/components/Eyebrow";
import { GitHubIcon } from "@/components/BrandIcons";
import { LINKS, SITE } from "@/lib/site";

export function DownloadCta() {
  return (
    <section id="download" className="border-b border-border">
      <div className="mx-auto max-w-5xl px-6 py-20">
        <div className="rounded-2xl border border-border bg-card p-8 sm:p-12">
          <div className="max-w-2xl">
            <Eyebrow>Get it</Eyebrow>
            <h2 className="mt-6 text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
              Download Rapid PDF {SITE.version}
            </h2>
            <p className="mt-4 text-lg text-muted-foreground">
              A per-user installer, no admin prompt. Adds a Start-menu entry, an optional desktop
              shortcut, and a clean uninstaller. Prefer no install? Grab the portable zip.
            </p>
          </div>

          <div className="mt-8 flex flex-wrap items-center gap-3">
            <a
              href={LINKS.downloadInstaller}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-5 py-3 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
            >
              <Download className="h-4 w-4" aria-hidden="true" />
              Installer (.exe)
            </a>
            <a
              href={LINKS.downloadPortable}
              className="inline-flex items-center justify-center gap-2 rounded-md border border-border bg-card px-5 py-3 text-sm font-medium text-foreground transition-colors hover:bg-accent"
            >
              <Package className="h-4 w-4" aria-hidden="true" />
              Portable (.zip)
            </a>
            <a
              href={LINKS.github}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center justify-center gap-2 rounded-md border border-border bg-card px-5 py-3 text-sm font-medium text-foreground transition-colors hover:bg-accent"
            >
              <GitHubIcon className="h-4 w-4" />
              View on GitHub
            </a>
          </div>

          <p className="mt-6 flex items-start gap-2 text-sm text-muted-foreground">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden="true" />
            <span>
              The build is currently unsigned, so Windows SmartScreen may warn on first run. Click{" "}
              <span className="font-medium text-foreground">More info → Run anyway</span>. Code
              signing is on the roadmap.
            </span>
          </p>
        </div>
      </div>
    </section>
  );
}

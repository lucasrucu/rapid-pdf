import { Download } from "lucide-react";

import { GitHubIcon } from "@/components/BrandIcons";
import { QoriMark } from "@/components/QoriMark";
import { LINKS } from "@/lib/site";

const SECTIONS = [
  { href: "#features", label: "Features" },
  { href: "#screens", label: "Screens" },
  { href: "#download", label: "Download" },
];

export function Nav() {
  return (
    <header className="sticky top-0 z-50 border-b border-border bg-background/80 backdrop-blur">
      <nav className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
        <a href="#top" aria-label="Rapid PDF home" className="inline-flex">
          <QoriMark label="Rapid PDF" />
        </a>
        <div className="flex items-center gap-5 text-sm text-muted-foreground">
          <div className="hidden items-center gap-5 sm:flex">
            {SECTIONS.map((s) => (
              <a key={s.href} href={s.href} className="transition-colors hover:text-foreground">
                {s.label}
              </a>
            ))}
          </div>
          <span className="hidden h-4 w-px bg-border sm:block" aria-hidden="true" />
          <a
            href={LINKS.github}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="View on GitHub"
            className="transition-colors hover:text-foreground"
          >
            <GitHubIcon className="h-5 w-5" />
          </a>
          <a
            href={LINKS.downloadInstaller}
            className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
          >
            <Download className="h-4 w-4" aria-hidden="true" />
            Download
          </a>
        </div>
      </nav>
    </header>
  );
}

import { Mail } from "lucide-react";

import { GitHubIcon } from "@/components/BrandIcons";
import { QoriMark } from "@/components/QoriMark";
import { LINKS, MAKER } from "@/lib/site";

export function Footer() {
  return (
    <footer className="bg-secondary/60">
      <div className="mx-auto flex max-w-5xl flex-col gap-6 px-6 py-12 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-col gap-3">
          <QoriMark label="Rapid PDF" />
          <p className="text-sm text-muted-foreground">
            A Qori tool. Built in Python with PySide6 and PyMuPDF.
          </p>
          {/* THE SOURCE OFFER, AND IT HAS TO LIVE HERE. This site is where the
              installer is handed out, and the AGPL gives rights to whoever
              receives the binary, so the licence and the source have to be
              reachable from the same page as the download button. The README
              carries the same offer for anyone who reaches the repo first. */}
          <p className="text-sm text-muted-foreground">
            Free software under the{" "}
            <a
              href={LINKS.license}
              target="_blank"
              rel="noopener noreferrer"
              className="text-foreground transition-colors hover:text-muted-foreground"
            >
              GNU AGPL-3.0
            </a>
            . Copyright (C) 2026 Lucas Ruiz.{" "}
            <a
              href={LINKS.github}
              target="_blank"
              rel="noopener noreferrer"
              className="text-foreground transition-colors hover:text-muted-foreground"
            >
              Get the source
            </a>
            .
          </p>
          {/* The very small link at the end. Quiet on purpose: the loud one is
              the maker band above. */}
          <p className="text-sm text-muted-foreground">
            Built by{" "}
            <a
              href={MAKER.site}
              target="_blank"
              rel="noopener noreferrer"
              className="text-foreground transition-colors hover:text-muted-foreground"
            >
              {MAKER.name}
            </a>
          </p>
        </div>
        <div className="flex items-center gap-5 text-sm text-muted-foreground">
          <a href="https://qori.land" className="transition-colors hover:text-foreground">
            qori.land
          </a>
          <a
            href={`mailto:${MAKER.email}`}
            aria-label={`Email ${MAKER.name}`}
            className="transition-colors hover:text-foreground"
          >
            <Mail className="h-5 w-5" aria-hidden="true" />
          </a>
          <a
            href={LINKS.github}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="GitHub"
            className="transition-colors hover:text-foreground"
          >
            <GitHubIcon className="h-5 w-5" />
          </a>
        </div>
      </div>
    </footer>
  );
}

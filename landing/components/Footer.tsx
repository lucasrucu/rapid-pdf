import { GitHubIcon } from "@/components/BrandIcons";
import { QoriMark } from "@/components/QoriMark";
import { LINKS } from "@/lib/site";

export function Footer() {
  return (
    <footer className="bg-secondary/60">
      <div className="mx-auto flex max-w-5xl flex-col gap-6 px-6 py-12 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-col gap-3">
          <QoriMark label="Rapid PDF" />
          <p className="text-sm text-muted-foreground">
            A Qori tool. Built in Python with PySide6 and PyMuPDF.
          </p>
        </div>
        <div className="flex items-center gap-5 text-sm text-muted-foreground">
          <a href="https://qori.land" className="transition-colors hover:text-foreground">
            qori.land
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

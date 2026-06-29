// Small section label: an amber tick + uppercase muted text.
export function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2.5">
      <span className="h-px w-6 bg-primary" aria-hidden="true" />
      <span className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
        {children}
      </span>
    </span>
  );
}

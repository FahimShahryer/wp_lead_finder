import { cn } from "@/lib/utils";

// Compact KPI tile for dashboard summaries. `accent` tints the icon chip.
export function StatCard({
  label,
  value,
  hint,
  icon,
  accent = "primary",
  className,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  icon?: React.ReactNode;
  accent?: "primary" | "emerald" | "amber" | "sky" | "violet";
  className?: string;
}) {
  const accents: Record<string, string> = {
    primary: "bg-primary/10 text-primary",
    emerald: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
    amber: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
    sky: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
    violet: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
  };
  return (
    <div
      className={cn(
        "group relative overflow-hidden rounded-xl border border-border bg-card p-4 shadow-card transition-all hover:-translate-y-0.5 hover:shadow-lift",
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          {label}
        </span>
        {icon && (
          <span className={cn("flex h-8 w-8 items-center justify-center rounded-lg", accents[accent])}>
            {icon}
          </span>
        )}
      </div>
      <div className="mt-2 text-2xl font-bold tabular-nums tracking-tight">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}

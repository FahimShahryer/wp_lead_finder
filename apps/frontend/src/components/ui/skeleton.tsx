import { cn } from "@/lib/utils";

// Shimmering placeholder block. Use while data is loading instead of a bare
// "Loading…" string — keeps layout stable and feels faster.
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-md bg-muted/70",
        "after:absolute after:inset-0 after:-translate-x-full after:bg-gradient-to-r after:from-transparent after:via-foreground/10 after:to-transparent after:animate-[shimmer_1.6s_infinite]",
        className,
      )}
      {...props}
    />
  );
}

export { Skeleton };

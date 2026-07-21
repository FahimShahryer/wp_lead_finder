import { Badge } from "@/components/ui/badge";

const VARIANT: Record<string, "default" | "secondary" | "success" | "warning" | "destructive"> = {
  queued: "secondary",
  running: "default",
  done: "success",
  budget_exceeded: "warning",
  failed: "destructive",
};

const LABEL: Record<string, string> = {
  budget_exceeded: "budget hit",
};

export function StatusBadge({ status, stage }: { status: string; stage?: string | null }) {
  const variant = VARIANT[status] || "outline";
  const base = LABEL[status] ?? status;
  const label = status === "running" && stage ? `${stage}` : base;
  return (
    // @ts-expect-error variant type is constrained but we narrow above
    <Badge variant={variant} className="capitalize">
      {status === "running" && (
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-70" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-primary" />
        </span>
      )}
      {label}
    </Badge>
  );
}

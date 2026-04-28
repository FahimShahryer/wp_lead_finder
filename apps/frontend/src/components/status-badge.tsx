import { Badge } from "@/components/ui/badge";

const VARIANT: Record<string, "default" | "secondary" | "success" | "warning" | "destructive"> = {
  queued: "secondary",
  running: "default",
  done: "success",
  budget_exceeded: "warning",
  failed: "destructive",
};

export function StatusBadge({ status, stage }: { status: string; stage?: string | null }) {
  const variant = VARIANT[status] || "outline";
  const label = status === "running" && stage ? `running · ${stage}` : status;
  // @ts-expect-error variant type is constrained but we narrow above
  return <Badge variant={variant}>{label}</Badge>;
}

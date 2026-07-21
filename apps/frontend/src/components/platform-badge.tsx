import { Badge } from "@/components/ui/badge";

// Each platform gets its own color tint so the dashboard can be skimmed
// at a glance: green-tinted = WhatsApp, indigo-tinted = Discord. The label
// stays short — full names ("WhatsApp", "Discord") read fine but eat
// horizontal space in the campaign list.
const STYLES: Record<string, { label: string; className: string }> = {
  whatsapp: {
    label: "WhatsApp",
    className:
      "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30",
  },
  discord: {
    label: "Discord",
    className:
      "bg-indigo-500/10 text-indigo-700 dark:text-indigo-300 border-indigo-500/30",
  },
  slack: {
    label: "Slack",
    className:
      "bg-purple-500/10 text-purple-700 dark:text-purple-300 border-purple-500/30",
  },
};

export function PlatformBadge({
  platform,
  size = "default",
}: {
  platform: string;
  size?: "default" | "sm";
}) {
  const meta = STYLES[platform] ?? {
    label: platform,
    className: "bg-muted text-muted-foreground",
  };
  return (
    <Badge
      variant="outline"
      className={
        meta.className + (size === "sm" ? " text-[10px] px-1.5 py-0" : "")
      }
    >
      {meta.label}
    </Badge>
  );
}

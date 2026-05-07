import { Badge } from "@/components/ui/badge";

// Each platform gets its own color tint so the dashboard can be skimmed
// at a glance: green-tinted = WhatsApp, indigo-tinted = Discord. The label
// stays short — full names ("WhatsApp", "Discord") read fine but eat
// horizontal space in the campaign list.
const STYLES: Record<string, { label: string; className: string }> = {
  whatsapp: {
    label: "WhatsApp",
    className:
      "bg-emerald-100 text-emerald-900 hover:bg-emerald-100 border-emerald-200",
  },
  discord: {
    label: "Discord",
    className:
      "bg-indigo-100 text-indigo-900 hover:bg-indigo-100 border-indigo-200",
  },
  slack: {
    label: "Slack",
    className:
      "bg-purple-100 text-purple-900 hover:bg-purple-100 border-purple-200",
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

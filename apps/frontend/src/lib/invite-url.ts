// Map an invite_id back to the canonical join URL for its platform. Used
// anywhere the UI renders an "Open invite" link or shows a join URL.
//
// Centralized here so adding a new platform (e.g. Slack) is one branch
// in one file instead of N copies scattered across components. Discord
// codes use the shorter discord.gg form (Discord clients accept both).
export function inviteUrl(
  platform: "whatsapp" | "discord" | string,
  inviteId: string,
): string {
  if (platform === "discord") {
    return `https://discord.gg/${inviteId}`;
  }
  // Default to WhatsApp for back-compat with legacy rows that may not have
  // a recognized platform value. Safer than throwing — a misrendered URL
  // still copy-pastes correctly for the operator to fix manually.
  return `https://chat.whatsapp.com/${inviteId}`;
}

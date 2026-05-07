// Map an invite_id back to the canonical join URL for its platform. Used
// anywhere the UI renders an "Open invite" link or shows a join URL.
//
// Centralized here so adding a new platform is one branch in one file
// instead of N copies scattered across components. Discord codes use the
// shorter discord.gg form (Discord clients accept both). Slack invite_ids
// are stored as `<workspace>/<token>`; we splice in the `/shared_invite/`
// segment to reconstruct the canonical join URL.
export function inviteUrl(
  platform: "whatsapp" | "discord" | "slack" | string,
  inviteId: string,
): string {
  if (platform === "discord") {
    return `https://discord.gg/${inviteId}`;
  }
  if (platform === "slack") {
    // inviteId looks like `marketing-agencies/zt-abc123-XYZ`
    return `https://join.slack.com/t/${inviteId.replace("/", "/shared_invite/")}`;
  }
  // Default to WhatsApp for back-compat with legacy rows.
  return `https://chat.whatsapp.com/${inviteId}`;
}

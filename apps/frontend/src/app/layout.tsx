import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "wp2 — lead finder",
  description: "WhatsApp group prospecting dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="border-b">
          <div className="container flex h-14 items-center justify-between">
            <Link href="/" className="text-base font-semibold">
              wp2 · lead finder
            </Link>
            <nav className="flex gap-4 text-sm">
              <Link href="/" className="text-muted-foreground hover:text-foreground">
                Campaigns
              </Link>
              <Link
                href="/campaigns/new"
                className="text-foreground font-medium hover:underline"
              >
                + New
              </Link>
            </nav>
          </div>
        </header>
        <main className="container py-6">{children}</main>
      </body>
    </html>
  );
}

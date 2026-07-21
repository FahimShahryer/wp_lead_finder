"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import {
  LayoutDashboard,
  Inbox,
  Megaphone,
  Plus,
  Menu,
  X,
  Radar,
} from "lucide-react";

import { ThemeToggle } from "@/components/theme-toggle";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Campaigns", icon: LayoutDashboard, match: (p: string) => p === "/" || p.startsWith("/campaigns") },
  { href: "/inbox", label: "Inbox", icon: Inbox, match: (p: string) => p.startsWith("/inbox") },
  { href: "/broadcasts", label: "Broadcasts", icon: Megaphone, match: (p: string) => p.startsWith("/broadcasts") },
];

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-2.5">
      <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-lift">
        <Radar className="h-5 w-5" />
      </span>
      <span className="flex flex-col leading-none">
        <span className="text-sm font-bold tracking-tight">Community Finder</span>
        <span className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
          lead engine
        </span>
      </span>
    </Link>
  );
}

function NavLinks({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  return (
    <nav className="flex flex-col gap-1">
      {NAV.map((item) => {
        const active = item.match(pathname);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            className={cn(
              "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-all",
              active
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            <Icon className={cn("h-4 w-4 transition-transform group-hover:scale-110", active && "text-primary")} />
            {item.label}
            {active && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-primary" />}
          </Link>
        );
      })}
    </nav>
  );
}

function NewCampaignButton({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <Link
      href="/campaigns/new"
      onClick={onNavigate}
      className="flex items-center justify-center gap-2 rounded-lg bg-primary px-3 py-2.5 text-sm font-semibold text-primary-foreground shadow-soft transition-all hover:shadow-lift hover:brightness-105 active:scale-[0.98]"
    >
      <Plus className="h-4 w-4" />
      New campaign
    </Link>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[256px_1fr]">
      {/* Desktop sidebar */}
      <aside className="sticky top-0 hidden h-screen flex-col border-r border-border bg-card/60 px-4 py-5 backdrop-blur lg:flex">
        <div className="px-1">
          <Brand />
        </div>
        <div className="mt-7 px-1">
          <NewCampaignButton />
        </div>
        <div className="mt-6 px-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Workspace
        </div>
        <div className="mt-2">
          <NavLinks pathname={pathname} />
        </div>
        <div className="mt-auto flex items-center justify-between rounded-lg border border-border bg-background/50 px-3 py-2">
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-success" />
            </span>
            All systems go
          </span>
          <ThemeToggle />
        </div>
      </aside>

      {/* Mobile top bar */}
      <div className="sticky top-0 z-30 flex items-center justify-between border-b border-border bg-card/80 px-4 py-3 backdrop-blur lg:hidden">
        <Brand />
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <button
            onClick={() => setMobileOpen(true)}
            aria-label="Open menu"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground hover:text-foreground"
          >
            <Menu className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div
            className="absolute inset-0 bg-foreground/40 backdrop-blur-sm"
            onClick={() => setMobileOpen(false)}
          />
          <div className="absolute left-0 top-0 h-full w-72 animate-in-fade border-r border-border bg-card px-4 py-5 shadow-card">
            <div className="flex items-center justify-between">
              <Brand />
              <button
                onClick={() => setMobileOpen(false)}
                aria-label="Close menu"
                className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-border text-muted-foreground hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="mt-6">
              <NewCampaignButton onNavigate={() => setMobileOpen(false)} />
            </div>
            <div className="mt-6">
              <NavLinks pathname={pathname} onNavigate={() => setMobileOpen(false)} />
            </div>
          </div>
        </div>
      )}

      {/* Main content */}
      <main className="min-w-0">
        <div className="mx-auto w-full max-w-[1200px] px-4 py-6 sm:px-6 lg:px-10 lg:py-9">
          <div key={pathname} className="animate-in-fade">
            {children}
          </div>
        </div>
      </main>
    </div>
  );
}

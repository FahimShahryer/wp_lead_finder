import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/app-shell";

export const metadata: Metadata = {
  title: "Community Finder — lead engine",
  description: "Find, score, and engage WhatsApp / Discord / Slack community leads.",
};

// Inline, render-blocking theme bootstrap: reads the saved preference (or the
// OS setting) and sets the `dark` class before first paint, so there's no
// flash of the wrong theme.
const themeScript = `
(function(){try{
  var t = localStorage.getItem('theme');
  if(t === 'dark' || (!t && window.matchMedia('(prefers-color-scheme: dark)').matches)){
    document.documentElement.classList.add('dark');
  }
}catch(e){}})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}

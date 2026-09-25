import type { Metadata, Viewport } from "next";
import { Bricolage_Grotesque, Geist_Mono, Instrument_Sans } from "next/font/google";

import "./globals.css";

/**
 * Two faces with distinct jobs: Bricolage Grotesque for display — page titles,
 * headline sentences, scores — and Instrument Sans for everything you read or
 * operate. Geist Mono only appears inside code blocks in chat.
 */
const bricolage = Bricolage_Grotesque({
  variable: "--font-bricolage",
  subsets: ["latin"],
  axes: ["opsz", "wdth"],
});

const instrument = Instrument_Sans({
  variable: "--font-instrument",
  subsets: ["latin"],
  axes: ["wdth"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: { default: "Foray", template: "%s – Foray" },
  description:
    "An AI job-search agent. It searches the boards, ranks every role against your background, and tailors your resume for the ones worth applying to.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ecefea" },
    { media: "(prefers-color-scheme: dark)", color: "#0d1310" },
  ],
};

/**
 * Applied before first paint so a dark-mode user never sees a white flash.
 * The choice lives in localStorage ("light" | "dark" | "system"); absent means
 * follow the operating system.
 */
const THEME_SCRIPT = `(() => {
  try {
    const stored = localStorage.getItem("foray-theme");
    const dark = stored === "dark" || ((!stored || stored === "system") && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("dark", dark);
  } catch {}
})();`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      className={`${bricolage.variable} ${instrument.variable} ${geistMono.variable} h-full antialiased`}
      lang="en"
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="min-h-full">{children}</body>
    </html>
  );
}

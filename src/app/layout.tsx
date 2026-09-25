import type { Metadata, Viewport } from "next";
import { Bricolage_Grotesque, Geist_Mono, Instrument_Sans } from "next/font/google";

import { ThemeSync } from "@/components/shell/theme-sync";
import { THEME_COLORS, THEME_SCRIPT } from "@/lib/theme";

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

/** Dark is the default theme; the browser chrome matches until someone picks light. */
export const viewport: Viewport = {
  themeColor: THEME_COLORS.dark,
  colorScheme: "dark light",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      // Rendered dark: that is the default, and the head script switches to
      // light before first paint for anyone who chose it.
      className={`${bricolage.variable} ${instrument.variable} ${geistMono.variable} dark h-full antialiased`}
      lang="en"
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="min-h-full">
        <ThemeSync />
        {children}
      </body>
    </html>
  );
}

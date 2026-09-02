import type { Metadata } from "next";
import { IBM_Plex_Mono, Inter } from "next/font/google";

import "./globals.css";

/**
 * Plex was drawn for technical documentation, which is what this tool is full
 * of. The pairing carries the product's central distinction: Plex Sans for
 * human prose, Plex Mono restricted to identifiers and machine output — clause
 * ids, confidence scores, hashes, timestamps. The typographic split mirrors the
 * provenance split the whole interface is organised around.
 *
 * Self-hosted at build time by next/font, so no request leaves the origin at
 * runtime and the strict CSP is satisfied.
 */
const sans = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "AuditLens",
  description: "PCI DSS v4.0.1 assessment workspace. Internal use only.",
  // Internal firm software behind a tunnel; there is nothing here to index.
  robots: { index: false, follow: false },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}

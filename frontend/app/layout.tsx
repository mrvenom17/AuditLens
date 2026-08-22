import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AuditLens",
  description: "PCI DSS v4.0.1 audit assistant — internal use only.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

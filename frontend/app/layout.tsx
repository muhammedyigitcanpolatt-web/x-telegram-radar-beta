import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Shortmox Radar — Public Beta",
  description: "Self-hosted intelligence review dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased scanlines">{children}</body>
    </html>
  );
}

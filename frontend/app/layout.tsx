import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mai",
  description: "A persistent personal AI environment.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="h-full antialiased">{children}</body>
    </html>
  );
}

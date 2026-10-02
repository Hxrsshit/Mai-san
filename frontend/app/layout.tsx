import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MAI — Your Personal AI Assistant",
  description: "A persistent personal AI environment.",
  icons: { icon: "/favicon.svg", apple: "/branding/mai-icon.svg" },
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

/** Root layout for the FinSight Next.js app. Sets HTML metadata and wraps all pages in Providers. */
import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/Providers";

export const metadata: Metadata = {
  title: "FinSight — Investment Research",
  description: "Multi-agent investment research with AG-UI",
};

/** Wraps children with the {@link Providers} component and configures HTML shell. */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

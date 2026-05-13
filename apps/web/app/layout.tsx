import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RescueNet — Offline Medical Triage",
  description: "AI-powered medical triage for zero-connectivity disaster zones. Powered by Gemma 4.",
  manifest: "/manifest.json",
  themeColor: "#030712",
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "RescueNet" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="apple-touch-icon" href="/icon-192.png" />
        <meta name="mobile-web-app-capable" content="yes" />
      </head>
      <body className="bg-gray-950 antialiased">
        {children}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              if ('serviceWorker' in navigator) {
                window.addEventListener('load', () => {
                  navigator.serviceWorker.register('/sw.js')
                    .then(r => console.log('SW registered:', r.scope))
                    .catch(e => console.log('SW failed:', e));
                });
              }
            `,
          }}
        />
      </body>
    </html>
  );
}

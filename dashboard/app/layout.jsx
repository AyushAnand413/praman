import { Instrument_Serif, Inter, JetBrains_Mono } from "next/font/google";

const instrumentSerif = Instrument_Serif({ subsets: ["latin"], weight: ["400"], style: ["normal", "italic"], display: "swap", variable: "--font-serif" });
const inter = Inter({ subsets: ["latin"], weight: ["300", "400", "500", "600"], display: "swap", variable: "--font-sans" });
const jetbrains = JetBrains_Mono({ subsets: ["latin"], weight: ["400", "500"], display: "swap", variable: "--font-mono" });

export const viewport = {
  width: "device-width",
  initialScale: 1,
};

export const metadata = {
  title: "PRAMAN — Merchant Console",
  description: "Minimalist financial governance and automated order guardrails for modern commerce.",
  icons: { icon: "/favicon.ico" },
};

export default function RootLayout({ children }) {
  return (
    <html lang="en" className={`${instrumentSerif.variable} ${inter.variable} ${jetbrains.variable}`} suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('praman-theme')||'light';if(t==='dark'){document.documentElement.setAttribute('data-theme','dark');}}catch(e){}})();`,
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}

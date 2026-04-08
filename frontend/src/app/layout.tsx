import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'XAU/USD Paper Trading Dashboard',
  description: 'Live paper trading dashboard for XAU/USD with EMA, BB, and Institutional strategies',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

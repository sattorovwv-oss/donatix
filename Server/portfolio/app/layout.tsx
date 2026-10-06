import type { Metadata, Viewport } from 'next';
import './globals.css';
import { I18nProvider } from '@/lib/i18n';
import SmoothScroll from '@/components/providers/SmoothScroll';

export const metadata: Metadata = {
  title: 'Alijon — Web, Telegram-боты и AI-решения',
  description:
    'Создаю современные веб-сайты, Telegram-ботов, Mini Apps и AI-решения. Full Stack разработка.',
  keywords: [
    'Alijon', 'Uways', 'веб-разработчик', 'Telegram бот', 'Mini Apps',
    'AI', 'Next.js', 'Душанбе', 'Таджикистан',
  ],
  authors: [{ name: 'Alijon Mahmadjonov' }],
  openGraph: {
    title: 'Alijon — Web, Telegram-боты и AI-решения',
    description: 'Современные веб-сайты, Telegram-боты, Mini Apps и AI-решения.',
    type: 'website',
  },
};

export const viewport: Viewport = {
  themeColor: '#05070D',
  width: 'device-width',
  initialScale: 1,
  // Масштабирование НЕ блокируем: запрет ломает доступность для слабовидящих
  maximumScale: 5,
};

export default function RootLayout({ children }: LayoutProps<'/'>) {
  return (
    <html lang="ru">
      <head>
        {/* Шрифты лежат на своём хостинге. Грузим заранее, иначе заголовок
            успевает мигнуть системным шрифтом при первой отрисовке */}
        <link
          rel="preload" as="font" type="font/woff2" crossOrigin="anonymous"
          href="/fonts/exo-2-cyrillic-700-normal.woff2"
        />
        <link
          rel="preload" as="font" type="font/woff2" crossOrigin="anonymous"
          href="/fonts/golos-text-cyrillic-400-normal.woff2"
        />
      </head>
      <body>
        <I18nProvider>
          <SmoothScroll>{children}</SmoothScroll>
        </I18nProvider>
      </body>
    </html>
  );
}

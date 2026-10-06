import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  /**
   * Статический экспорт: `npm run build` кладёт готовые HTML/CSS/JS в папку `out/`.
   * Её содержимое загружается в public_html на любом обычном хостинге
   * (Somon Host, cPanel, Apache/nginx) — Node.js на сервере не нужен.
   */
  output: 'export',

  /**
   * Оптимизатор картинок Next.js требует сервера, на статике его нет.
   * Изображения отдаются как есть, поэтому они уже сжаты в public/.
   */
  images: { unoptimized: true },

  /**
   * Apache отдаёт /about/ как /about/index.html. Без слеша на конце
   * на shared-хостинге такие ссылки ломаются, поэтому включаем принудительно.
   */
  trailingSlash: true,

  /** Убирает заголовок X-Powered-By из ответов сервера. */
  poweredByHeader: false,
};

export default nextConfig;

'use client';

import type { Project } from '@/config/site';

/**
 * Знак проекта — линейная графика вместо скриншота.
 *
 * Скриншотов проектов пока нет, а картинка-заглушка выглядела бы дёшево.
 * Знак рисуется вектором: он масштабируется без потерь, красится акцентом
 * карточки и оживает по наведению средствами CSS.
 */
export default function ProjectGlyph({ shape }: { shape: Project['shape'] }) {
  const common = {
    viewBox: '0 0 96 96',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.6,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
    className: 'glyph',
  };

  switch (shape) {
    case 'messenger':
      return (
        <svg {...common}>
          <rect className="g-a" x="14" y="20" width="50" height="34" rx="10" />
          <path className="g-a" d="M26 62l-6 12 16-9" />
          <rect className="g-b" x="38" y="40" width="44" height="30" rx="9" />
          <circle className="g-c" cx="30" cy="36" r="2.4" />
          <circle className="g-c" cx="39" cy="36" r="2.4" />
          <circle className="g-c" cx="48" cy="36" r="2.4" />
        </svg>
      );

    case 'marketplace':
      return (
        <svg {...common}>
          <path className="g-a" d="M18 34h60l-5 44H23z" />
          <path className="g-b" d="M34 34V24a14 14 0 0128 0v10" />
          <path className="g-c" d="M32 52h32M32 63h20" />
        </svg>
      );

    case 'bot':
      return (
        <svg {...common}>
          <rect className="g-a" x="20" y="30" width="56" height="42" rx="12" />
          <path className="g-b" d="M48 30V16M48 12a4 4 0 100 8 4 4 0 000-8z" />
          <circle className="g-c" cx="36" cy="48" r="4.5" />
          <circle className="g-c" cx="60" cy="48" r="4.5" />
          <path className="g-b" d="M38 61h20M12 44v14M84 44v14" />
        </svg>
      );

    case 'miniapp':
      return (
        <svg {...common}>
          <rect className="g-a" x="30" y="12" width="36" height="72" rx="9" />
          <path className="g-b" d="M30 26h36M30 70h36" />
          <rect className="g-c" x="38" y="34" width="9" height="9" rx="2.5" />
          <rect className="g-c" x="51" y="34" width="9" height="9" rx="2.5" />
          <rect className="g-c" x="38" y="49" width="9" height="9" rx="2.5" />
          <rect className="g-c" x="51" y="49" width="9" height="9" rx="2.5" />
        </svg>
      );

    case 'ai':
      return (
        <svg {...common}>
          <circle className="g-a" cx="48" cy="48" r="13" />
          <circle className="g-b" cx="48" cy="16" r="5" />
          <circle className="g-b" cx="48" cy="80" r="5" />
          <circle className="g-b" cx="19" cy="32" r="5" />
          <circle className="g-b" cx="77" cy="32" r="5" />
          <circle className="g-b" cx="19" cy="64" r="5" />
          <circle className="g-b" cx="77" cy="64" r="5" />
          <path
            className="g-c"
            d="M48 21v14M48 61v14M24 35l12 7M72 35L60 42M24 61l12-7M72 61L60 54"
          />
        </svg>
      );

    case 'web':
    default:
      return (
        <svg {...common}>
          <rect className="g-a" x="12" y="20" width="72" height="56" rx="9" />
          <path className="g-b" d="M12 36h72" />
          <circle className="g-c" cx="23" cy="28" r="2.2" />
          <circle className="g-c" cx="31" cy="28" r="2.2" />
          <circle className="g-c" cx="39" cy="28" r="2.2" />
          <path className="g-c" d="M26 50h30M26 61h44" />
        </svg>
      );
  }
}

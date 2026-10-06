'use client';

import { useEffect, useRef, type RefObject } from 'react';

/**
 * Прогресс прокрутки страницы от 0 до 1 — в ref, а не в state.
 *
 * Специально без useState: сцена читает значение в useFrame шестьдесят раз
 * в секунду, и ре-рендер React на каждый пиксель скролла убил бы кадры.
 */
export function useScrollProgress(): RefObject<number> {
  const progress = useRef(0);

  useEffect(() => {
    const read = () => {
      const max = document.documentElement.scrollHeight - window.innerHeight;
      progress.current = max > 0 ? Math.min(Math.max(window.scrollY / max, 0), 1) : 0;
    };

    read();
    window.addEventListener('scroll', read, { passive: true });
    window.addEventListener('resize', read);
    return () => {
      window.removeEventListener('scroll', read);
      window.removeEventListener('resize', read);
    };
  }, []);

  return progress;
}

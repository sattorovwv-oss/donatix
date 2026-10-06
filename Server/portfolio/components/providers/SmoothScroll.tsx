'use client';

/**
 * Инерционный скролл (Lenis) + связка с GSAP ScrollTrigger.
 *
 * Важно: Lenis и ScrollTrigger должны идти от ОДНОГО тикера, иначе
 * анимации по скроллу дрожат — ScrollTrigger считает позицию раньше,
 * чем Lenis её применил. Поэтому rAF отдан GSAP, а Lenis дёргается из него.
 */

import { useEffect, type ReactNode } from 'react';
import Lenis from 'lenis';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';

export default function SmoothScroll({ children }: { children: ReactNode }) {
  useEffect(() => {
    // Пользователь просил меньше движения — оставляем нативный скролл
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    gsap.registerPlugin(ScrollTrigger);

    const lenis = new Lenis({
      lerp: 0.09,          // меньше — тяжелее и дороже на ощупь
      wheelMultiplier: 1,
      touchMultiplier: 1.6,
      smoothWheel: true,
    });

    lenis.on('scroll', ScrollTrigger.update);

    const raf = (time: number) => lenis.raf(time * 1000);
    gsap.ticker.add(raf);
    // Без этого GSAP «догоняет» кадры после переключения вкладки и сцена дёргается
    gsap.ticker.lagSmoothing(0);

    return () => {
      gsap.ticker.remove(raf);
      lenis.destroy();
    };
  }, []);

  return <>{children}</>;
}

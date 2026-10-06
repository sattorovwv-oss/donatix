'use client';

/**
 * Карточка, которая наклоняется вслед за курсором.
 *
 * Наклон делается CSS-трансформацией, а не отдельной 3D-сценой: шесть
 * канвасов на странице проектов означали бы шесть контекстов WebGL и
 * просевшие кадры. Ощущение объёма то же, цена — несопоставимо ниже.
 */

import { useRef, type ReactNode } from 'react';
import { motion, useMotionValue, useSpring, useTransform } from 'framer-motion';

const MAX_TILT = 9; // градусов; больше — и карточка начинает выглядеть кривой

export default function TiltCard({
  children,
  className = '',
  accent = '#2F7BFF',
}: {
  children: ReactNode;
  className?: string;
  accent?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  const x = useMotionValue(0);
  const y = useMotionValue(0);

  // Пружина убирает рывки при быстром движении мыши
  const springX = useSpring(x, { stiffness: 180, damping: 18, mass: 0.4 });
  const springY = useSpring(y, { stiffness: 180, damping: 18, mass: 0.4 });

  const rotateX = useTransform(springY, [-0.5, 0.5], [MAX_TILT, -MAX_TILT]);
  const rotateY = useTransform(springX, [-0.5, 0.5], [-MAX_TILT, MAX_TILT]);

  // Положение блика привязано к курсору
  const glowX = useTransform(springX, [-0.5, 0.5], ['20%', '80%']);
  const glowY = useTransform(springY, [-0.5, 0.5], ['20%', '80%']);

  const handleMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect) return;
    x.set((event.clientX - rect.left) / rect.width - 0.5);
    y.set((event.clientY - rect.top) / rect.height - 0.5);
  };

  const handleLeave = () => {
    x.set(0);
    y.set(0);
  };

  return (
    <motion.div
      ref={ref}
      className={`tilt ${className}`}
      onPointerMove={handleMove}
      onPointerLeave={handleLeave}
      style={{ rotateX, rotateY, transformPerspective: 1000 }}
    >
      <motion.span
        aria-hidden="true"
        className="tilt-glow"
        style={{
          left: glowX,
          top: glowY,
          background: `radial-gradient(circle, ${accent}55 0%, transparent 68%)`,
        }}
      />
      <div className="tilt-body">{children}</div>
    </motion.div>
  );
}

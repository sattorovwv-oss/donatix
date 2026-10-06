'use client';

import { motion } from 'framer-motion';
import type { ReactNode } from 'react';

/**
 * Появление блока при попадании в кадр.
 * once: true — анимация играет один раз: повторный проигрыш при каждом
 * возврате к секции раздражает и мешает перечитывать текст.
 */
export default function Reveal({
  children,
  delay = 0,
  y = 26,
  className,
}: {
  children: ReactNode;
  delay?: number;
  y?: number;
  className?: string;
}) {
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '-80px' }}
      transition={{ duration: 0.85, delay, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.div>
  );
}

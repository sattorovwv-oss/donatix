'use client';

import { motion } from 'framer-motion';
import { useI18n } from '@/lib/i18n';
import Reveal from '@/components/ui/Reveal';

export default function Process() {
  const { dict } = useI18n();

  return (
    <section className="section" id="process">
      <div className="u-wrap">
        <Reveal>
          <span className="u-eyebrow">{dict.process.eyebrow}</span>
        </Reveal>
        <Reveal delay={0.06}>
          <h2 className="section-title">{dict.process.title}</h2>
        </Reveal>
        <Reveal delay={0.12}>
          <p className="section-lead">{dict.process.lead}</p>
        </Reveal>

        {/* Нумерация здесь несёт смысл: это последовательность,
            где каждый шаг возможен только после предыдущего */}
        <ol className="process">
          {dict.process.steps.map((step, index) => (
            <motion.li
              key={step.title}
              initial={{ opacity: 0, x: -18 }}
              whileInView={{ opacity: 1, x: 0 }}
              viewport={{ once: true, margin: '-70px' }}
              transition={{
                duration: 0.65,
                delay: Math.min(index * 0.08, 0.45),
                ease: [0.16, 1, 0.3, 1],
              }}
            >
              <span className="process-num">{String(index + 1).padStart(2, '0')}</span>
              <div className="process-body">
                <h3>{step.title}</h3>
                <p>{step.text}</p>
              </div>
            </motion.li>
          ))}
        </ol>
      </div>
    </section>
  );
}

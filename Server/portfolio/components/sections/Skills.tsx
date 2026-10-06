'use client';

import { motion } from 'framer-motion';
import { useI18n } from '@/lib/i18n';
import { skills, type SkillGroup } from '@/config/site';
import Reveal from '@/components/ui/Reveal';

const GROUPS: SkillGroup[] = ['frontend', 'backend', 'tools'];

export default function Skills() {
  const { dict } = useI18n();

  return (
    <section className="section" id="skills">
      <div className="u-wrap">
        <Reveal>
          <span className="u-eyebrow">{dict.skills.eyebrow}</span>
        </Reveal>
        <Reveal delay={0.06}>
          <h2 className="section-title">{dict.skills.title}</h2>
        </Reveal>
        <Reveal delay={0.12}>
          <p className="section-lead">{dict.skills.lead}</p>
        </Reveal>

        {GROUPS.map((group) => {
          const items = skills.filter((skill) => skill.group === group);

          return (
            <div className="skills-group" key={group}>
              <Reveal>
                <h3 className="skills-group-title">{dict.skills[group]}</h3>
              </Reveal>

              <ul className="skills-grid">
                {items.map((skill, index) => {
                  const Icon = skill.icon;
                  return (
                    <motion.li
                      key={skill.name}
                      className="skill"
                      initial={{ opacity: 0, y: 22 }}
                      whileInView={{ opacity: 1, y: 0 }}
                      viewport={{ once: true, margin: '-60px' }}
                      transition={{
                        duration: 0.6,
                        // Ступенька в 45 мс: карточки появляются волной,
                        // но пользователь не ждёт конца анимации
                        delay: Math.min(index * 0.045, 0.4),
                        ease: [0.16, 1, 0.3, 1],
                      }}
                      style={{ ['--tint' as string]: skill.tint }}
                    >
                      <span className="skill-icon"><Icon aria-hidden="true" /></span>
                      <span className="skill-name">{skill.name}</span>
                    </motion.li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </div>
    </section>
  );
}

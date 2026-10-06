'use client';

import { motion } from 'framer-motion';
import { HiArrowDown } from 'react-icons/hi';
import { useI18n } from '@/lib/i18n';
import { person, contacts } from '@/config/site';

/** Появление снизу с задержкой по порядку — задаёт ритм чтения первого экрана */
const rise = {
  hidden: { opacity: 0, y: 26 },
  show: (delay: number) => ({
    opacity: 1,
    y: 0,
    transition: { duration: 0.9, delay, ease: [0.16, 1, 0.3, 1] as const },
  }),
};

export default function Hero() {
  const { dict } = useI18n();
  const primaryContacts = contacts.filter((contact) => contact.primary);

  return (
    <section className="hero" id="top">
      <div className="u-wrap hero-inner">
        <motion.p
          className="u-eyebrow" variants={rise} initial="hidden" animate="show" custom={0.1}
        >
          {person.nickname} · {person.hometown} → {person.basedIn}
        </motion.p>

        <motion.h1
          className="hero-title" variants={rise} initial="hidden" animate="show" custom={0.2}
        >
          <span className="u-grad">{person.name}</span>
        </motion.h1>

        <motion.ul
          className="hero-roles" variants={rise} initial="hidden" animate="show" custom={0.34}
        >
          {dict.hero.roles.map((role) => (
            <li key={role}>{role}</li>
          ))}
        </motion.ul>

        <motion.p
          className="hero-tagline" variants={rise} initial="hidden" animate="show" custom={0.46}
        >
          {dict.hero.tagline}
        </motion.p>

        <motion.div
          className="hero-actions" variants={rise} initial="hidden" animate="show" custom={0.58}
        >
          <a href="#order" className="btn btn-solid">{dict.hero.ctaOrder}</a>
          <a href="#projects" className="btn btn-glass">{dict.hero.ctaProjects}</a>
        </motion.div>

        <motion.ul
          className="hero-socials" variants={rise} initial="hidden" animate="show" custom={0.68}
        >
          {primaryContacts.map((contact) => {
            const Icon = contact.icon;
            return (
              <li key={contact.id}>
                <a
                  href={contact.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="social"
                  aria-label={contact.label}
                >
                  <Icon aria-hidden="true" />
                  <span>{contact.label}</span>
                </a>
              </li>
            );
          })}
        </motion.ul>
      </div>

      <motion.a
        href="#about"
        className="hero-scroll"
        variants={rise}
        initial="hidden"
        animate="show"
        custom={0.9}
      >
        <span>{dict.hero.scroll}</span>
        <HiArrowDown aria-hidden="true" />
      </motion.a>
    </section>
  );
}

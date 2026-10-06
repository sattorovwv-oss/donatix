'use client';

import { useI18n } from '@/lib/i18n';
import { person, getAge } from '@/config/site';
import Reveal from '@/components/ui/Reveal';

export default function About() {
  const { dict, fill } = useI18n();
  const age = getAge();

  const facts = [
    { label: dict.about.factAge, value: fill(dict.about.factYears, { age }) },
    { label: dict.about.factCity, value: person.hometown },
    { label: dict.about.factBased, value: person.basedIn },
    { label: dict.about.factLangs, value: dict.about.langsValue },
    { label: dict.about.factFocus, value: dict.about.focusValue },
  ];

  return (
    <section className="section" id="about">
      <div className="u-wrap">
        <Reveal>
          <span className="u-eyebrow">{dict.about.eyebrow}</span>
        </Reveal>

        <div className="about-grid">
          <Reveal delay={0.06}>
            <h2 className="section-title">{dict.about.title}</h2>
          </Reveal>

          <Reveal delay={0.14} className="about-text">
            <p>{fill(dict.about.p1, { age })}</p>
            <p>{dict.about.p2}</p>
            <p>{dict.about.p3}</p>
          </Reveal>
        </div>

        <Reveal delay={0.2}>
          <ul className="facts u-glass">
            {facts.map((fact) => (
              <li key={fact.label}>
                <span className="facts-k">{fact.label}</span>
                <span className="facts-v">{fact.value}</span>
              </li>
            ))}
          </ul>
        </Reveal>
      </div>
    </section>
  );
}

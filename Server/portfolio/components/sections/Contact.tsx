'use client';

import { useState } from 'react';
import { HiOutlineDuplicate, HiCheck } from 'react-icons/hi';
import { useI18n } from '@/lib/i18n';
import { contacts, person } from '@/config/site';
import Reveal from '@/components/ui/Reveal';

export default function Contact() {
  const { dict } = useI18n();
  const [copied, setCopied] = useState<string | null>(null);

  const copy = async (id: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(id);
      window.setTimeout(() => setCopied(null), 2000);
    } catch {
      // Буфер обмена недоступен (нет https или пользователь запретил) —
      // ссылка рядом остаётся рабочей, поэтому просто ничего не делаем
    }
  };

  return (
    <section className="section" id="contact">
      <div className="u-wrap">
        <Reveal>
          <span className="u-eyebrow">{dict.contact.eyebrow}</span>
        </Reveal>
        <Reveal delay={0.06}>
          <h2 className="section-title">{dict.contact.title}</h2>
        </Reveal>
        <Reveal delay={0.12}>
          <p className="section-lead">{dict.contact.lead}</p>
        </Reveal>

        <ul className="contacts">
          {contacts.map((contact, index) => {
            const Icon = contact.icon;
            const isCopied = copied === contact.id;

            return (
              <Reveal key={contact.id} delay={Math.min(index * 0.05, 0.3)}>
                <li className="contact u-glass">
                  <a
                    href={contact.href}
                    target={contact.href.startsWith('http') ? '_blank' : undefined}
                    rel={contact.href.startsWith('http') ? 'noopener noreferrer' : undefined}
                    className="contact-main"
                  >
                    <span className="contact-icon"><Icon aria-hidden="true" /></span>
                    <span className="contact-text">
                      <span className="contact-label">{contact.label}</span>
                      <span className="contact-value">{contact.display}</span>
                    </span>
                  </a>

                  <button
                    type="button"
                    className="contact-copy"
                    onClick={() => copy(contact.id, contact.display)}
                    aria-label={`${dict.contact.copy}: ${contact.display}`}
                  >
                    {isCopied ? <HiCheck aria-hidden="true" /> : <HiOutlineDuplicate aria-hidden="true" />}
                    <span>{isCopied ? dict.contact.copied : dict.contact.copy}</span>
                  </button>
                </li>
              </Reveal>
            );
          })}
        </ul>

        <footer className="footer">
          <span>© {new Date().getFullYear()} {person.fullName}. {dict.footer.rights}.</span>
          <span>{dict.footer.built}</span>
        </footer>
      </div>
    </section>
  );
}

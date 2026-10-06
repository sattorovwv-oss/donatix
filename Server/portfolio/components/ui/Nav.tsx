'use client';

import { useEffect, useState } from 'react';
import { HiMenuAlt4, HiX } from 'react-icons/hi';
import { useI18n, LANGS, LANG_LABELS } from '@/lib/i18n';
import { person } from '@/config/site';

const LINKS = [
  { href: '#about', key: 'about' },
  { href: '#skills', key: 'skills' },
  { href: '#projects', key: 'projects' },
  { href: '#process', key: 'process' },
  { href: '#contact', key: 'contact' },
] as const;

export default function Nav() {
  const { dict, lang, setLang } = useI18n();
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 40);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  // Пока открыто меню на телефоне, фон скроллиться не должен
  useEffect(() => {
    document.body.style.overflow = open ? 'hidden' : '';
    return () => { document.body.style.overflow = ''; };
  }, [open]);

  return (
    <header className={`nav ${scrolled ? 'is-scrolled' : ''}`}>
      <div className="u-wrap nav-inner">
        <a href="#top" className="nav-brand" onClick={() => setOpen(false)}>
          {person.name}<span>.</span>
        </a>

        <nav className={`nav-links ${open ? 'is-open' : ''}`} aria-label="Основное меню">
          {LINKS.map((link) => (
            <a key={link.href} href={link.href} onClick={() => setOpen(false)}>
              {dict.nav[link.key]}
            </a>
          ))}
          <a href="#order" className="nav-cta" onClick={() => setOpen(false)}>
            {dict.nav.order}
          </a>
        </nav>

        <div className="nav-tools">
          <div className="lang" role="group" aria-label="Язык сайта">
            {LANGS.map((code) => (
              <button
                key={code}
                type="button"
                onClick={() => setLang(code)}
                className={code === lang ? 'is-active' : ''}
                aria-pressed={code === lang}
              >
                {LANG_LABELS[code]}
              </button>
            ))}
          </div>

          <button
            type="button"
            className="burger"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
            aria-label={open ? 'Закрыть меню' : 'Открыть меню'}
          >
            {open ? <HiX /> : <HiMenuAlt4 />}
          </button>
        </div>
      </div>
    </header>
  );
}

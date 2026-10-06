'use client';

/**
 * Мультиязычность для статического экспорта.
 *
 * Почему не i18next: при `output: 'export'` HTML собирается один раз на языке
 * по умолчанию. Если определять язык браузера во время первого рендера,
 * разметка сервера и клиента разойдутся — React выдаст ошибку гидратации
 * и страница мигнёт. Поэтому первый рендер всегда русский (как в собранном
 * HTML), а переключение на язык браузера происходит уже после монтирования.
 */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from 'react';
import ru, { type Dictionary } from './locales/ru';
import tg from './locales/tg';
import en from './locales/en';

export const LANGS = ['ru', 'tg', 'en'] as const;
export type Lang = (typeof LANGS)[number];

const DICTS: Record<Lang, Dictionary> = { ru, tg, en };

/** Короткие подписи для переключателя языка */
export const LANG_LABELS: Record<Lang, string> = { ru: 'RU', tg: 'TJ', en: 'EN' };

const STORAGE_KEY = 'alijon.lang';

/** Язык, на котором собирается статический HTML. */
const DEFAULT_LANG: Lang = 'ru';

type I18nValue = {
  lang: Lang;
  dict: Dictionary;
  setLang: (lang: Lang) => void;
  /** Подставляет значения в плейсхолдеры вида {age} */
  fill: (template: string, vars: Record<string, string | number>) => string;
};

const I18nContext = createContext<I18nValue | null>(null);

function detectLang(): Lang {
  if (typeof window === 'undefined') return DEFAULT_LANG;

  const saved = window.localStorage.getItem(STORAGE_KEY);
  if (saved && (LANGS as readonly string[]).includes(saved)) return saved as Lang;

  // navigator.languages учитывает весь список предпочтений, а не только первый
  const candidates = window.navigator.languages?.length
    ? window.navigator.languages
    : [window.navigator.language];

  for (const raw of candidates) {
    const code = raw.toLowerCase().split('-')[0];
    if (code === 'tg') return 'tg';
    if (code === 'en') return 'en';
    if (code === 'ru') return 'ru';
  }
  return DEFAULT_LANG;
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(DEFAULT_LANG);

  // Определяем язык только после монтирования — см. комментарий вверху файла
  useEffect(() => {
    const detected = detectLang();
    if (detected !== DEFAULT_LANG) setLangState(detected);
  }, []);

  // Держим атрибут lang в актуальном состоянии: от него зависят
  // скринридеры, переносы слов и автоперевод браузера
  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // приватный режим может запрещать запись — язык просто не запомнится
    }
  }, []);

  const fill = useCallback(
    (template: string, vars: Record<string, string | number>) =>
      template.replace(/\{(\w+)\}/g, (match, key) =>
        key in vars ? String(vars[key]) : match,
      ),
    [],
  );

  const value = useMemo<I18nValue>(
    () => ({ lang, dict: DICTS[lang], setLang, fill }),
    [lang, setLang, fill],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error('useI18n нужно вызывать внутри <I18nProvider>');
  return ctx;
}

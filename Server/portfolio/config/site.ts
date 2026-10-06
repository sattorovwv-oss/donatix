/**
 * ЕДИНЫЙ ФАЙЛ НАСТРОЕК САЙТА
 * ===========================
 * Здесь меняются контакты, ссылки, проекты и навыки.
 * Трогать код компонентов для этого НЕ нужно.
 *
 * После правки: npm run build — и заливай папку out/ на хостинг.
 */

import type { IconType } from 'react-icons';
import {
  SiTelegram, SiInstagram, SiGithub, SiPhp, SiJavascript, SiTypescript,
  SiReact, SiNextdotjs, SiNodedotjs, SiPython, SiMysql, SiHtml5, SiCss,
  SiTailwindcss, SiGit, SiDocker,
} from 'react-icons/si';
import { HiOutlineMail, HiOutlinePhone } from 'react-icons/hi';
import { TbApi, TbBrain } from 'react-icons/tb';

/* ─────────────────────────────────────────────
   1. ЛИЧНЫЕ ДАННЫЕ
   ───────────────────────────────────────────── */
export const person = {
  name: 'Alijon',
  fullName: 'Alijon Mahmadjonov',
  nickname: 'Uways',
  birthYear: 2006,
  hometown: 'Душанбе',
  basedIn: 'Казахстан',
} as const;

/* ─────────────────────────────────────────────
   2. КОНТАКТЫ — меняй значения здесь
   ───────────────────────────────────────────── */
export type Contact = {
  id: string;
  label: string;
  /** То, что видит посетитель */
  display: string;
  /** Куда ведёт ссылка */
  href: string;
  icon: IconType;
  /** Показывать ли крупной кнопкой в hero */
  primary?: boolean;
};

export const contacts: Contact[] = [
  {
    id: 'telegram',
    label: 'Telegram',
    display: '@rutsiyax',
    href: 'https://t.me/rutsiyax',
    icon: SiTelegram,
    primary: true,
  },
  {
    id: 'instagram',
    label: 'Instagram',
    display: '@uways',
    href: 'https://instagram.com/uways',
    icon: SiInstagram,
    primary: true,
  },
  {
    id: 'github',
    label: 'GitHub',
    display: 'alijon26062006-bit',
    href: 'https://github.com/alijon26062006-bit',
    icon: SiGithub,
  },
  {
    id: 'email',
    label: 'Email',
    display: 'alijon26.06.2006@gmail.com',
    href: 'mailto:alijon26.06.2006@gmail.com',
    icon: HiOutlineMail,
  },
  {
    id: 'phone',
    label: 'Телефон',
    display: '+992 000 00 00 00',
    href: 'tel:+992000000000',
    icon: HiOutlinePhone,
  },
];

/** Куда уходит форма заказа. Пока — в Telegram; позже заменим на PHP-обработчик. */
export const orderTarget = {
  /** 'telegram' — открыть чат с текстом заявки. 'php' — отправить на свой сервер. */
  mode: 'telegram' as 'telegram' | 'php',
  telegramUser: 'rutsiyax',
  /** Путь к PHP-обработчику на хостинге (используется только при mode: 'php'). */
  phpEndpoint: '/api/order.php',
};

/* ─────────────────────────────────────────────
   3. НАВЫКИ
   ───────────────────────────────────────────── */
export type SkillGroup = 'frontend' | 'backend' | 'tools';

export type Skill = {
  name: string;
  icon: IconType;
  group: SkillGroup;
  /** HEX-цвет бренда технологии — используется для подсветки карточки */
  tint: string;
};

export const skills: Skill[] = [
  { name: 'JavaScript', icon: SiJavascript, group: 'frontend', tint: '#F7DF1E' },
  { name: 'TypeScript', icon: SiTypescript, group: 'frontend', tint: '#3178C6' },
  { name: 'React', icon: SiReact, group: 'frontend', tint: '#61DAFB' },
  { name: 'Next.js', icon: SiNextdotjs, group: 'frontend', tint: '#FFFFFF' },
  { name: 'HTML', icon: SiHtml5, group: 'frontend', tint: '#E34F26' },
  { name: 'CSS', icon: SiCss, group: 'frontend', tint: '#1572B6' },
  { name: 'Tailwind', icon: SiTailwindcss, group: 'frontend', tint: '#38BDF8' },
  { name: 'PHP', icon: SiPhp, group: 'backend', tint: '#777BB4' },
  { name: 'Node.js', icon: SiNodedotjs, group: 'backend', tint: '#5FA04E' },
  { name: 'Python', icon: SiPython, group: 'backend', tint: '#3776AB' },
  { name: 'MySQL', icon: SiMysql, group: 'backend', tint: '#4479A1' },
  { name: 'REST API', icon: TbApi, group: 'backend', tint: '#5EA8FF' },
  { name: 'Telegram Bot API', icon: SiTelegram, group: 'backend', tint: '#26A5E4' },
  { name: 'AI', icon: TbBrain, group: 'backend', tint: '#C9D6E8' },
  { name: 'Git', icon: SiGit, group: 'tools', tint: '#F05032' },
  { name: 'GitHub', icon: SiGithub, group: 'tools', tint: '#FFFFFF' },
  { name: 'Docker', icon: SiDocker, group: 'tools', tint: '#2496ED' },
];

/* ─────────────────────────────────────────────
   4. ПРОЕКТЫ
   Тексты берутся из переводов по ключу i18nKey
   (lib/locales/*.ts → projects.<i18nKey>)
   ───────────────────────────────────────────── */
export type Project = {
  id: string;
  i18nKey: string;
  /** Год или период работы над проектом */
  year: string;
  stack: string[];
  /** Ссылка на проект. null — если показывать нечего */
  href: string | null;
  /** Форма 3D-объекта на карточке проекта */
  shape: 'messenger' | 'marketplace' | 'bot' | 'miniapp' | 'ai' | 'web';
  /** Акцентный цвет карточки */
  accent: string;
  featured?: boolean;
};

export const projects: Project[] = [
  {
    id: 'payom',
    i18nKey: 'payom',
    year: '2025',
    stack: ['React', 'Node.js', 'WebSocket', 'MySQL'],
    href: null,
    shape: 'messenger',
    accent: '#2F7BFF',
    featured: true,
  },
  {
    id: 'tajbozor',
    i18nKey: 'tajbozor',
    year: '2025',
    stack: ['Next.js', 'TypeScript', 'MySQL', 'REST API'],
    href: null,
    shape: 'marketplace',
    accent: '#38E0FF',
    featured: true,
  },
  {
    id: 'cargo',
    i18nKey: 'cargo',
    year: '2025',
    stack: ['Python', 'aiogram', 'Telegram Bot API'],
    href: null,
    shape: 'bot',
    accent: '#26A5E4',
  },
  {
    id: 'miniapps',
    i18nKey: 'miniapps',
    year: '2025 — сейчас',
    stack: ['React', 'TypeScript', 'Telegram Mini Apps'],
    href: null,
    shape: 'miniapp',
    accent: '#7C6BFF',
  },
  {
    id: 'ai',
    i18nKey: 'ai',
    year: '2025 — сейчас',
    stack: ['Python', 'LLM API', 'Автоматизация'],
    href: null,
    shape: 'ai',
    accent: '#5EA8FF',
  },
  {
    id: 'websites',
    i18nKey: 'websites',
    year: '2024 — сейчас',
    stack: ['Next.js', 'Three.js', 'Tailwind'],
    href: null,
    shape: 'web',
    accent: '#9AB4E0',
  },
];

/* ─────────────────────────────────────────────
   5. ФОНОВАЯ МУЗЫКА
   Положи свой файл в public/audio/ и укажи имя.
   Пусто ('') — кнопка звука не показывается.
   ───────────────────────────────────────────── */
export const audio = {
  src: '',
  volume: 0.35,
};

/* ─────────────────────────────────────────────
   6. КАЧЕСТВО 3D-СЦЕНЫ
   ───────────────────────────────────────────── */
export const scene = {
  /** Количество зданий в городе (десктоп / мобильный) */
  buildings: { desktop: 260, mobile: 90 },
  /** Количество цифровых частиц (десктоп / мобильный) */
  particles: { desktop: 2600, mobile: 900 },
  /** Bloom — самое дорогое по производительности. На мобильных выключен. */
  bloom: { intensity: 0.5, threshold: 0.5 },
};

/** Возраст считается сам, чтобы не править каждый год. */
export function getAge(): number {
  const now = new Date();
  let age = now.getFullYear() - person.birthYear;
  // день рождения 26 июня
  const hadBirthday = now.getMonth() > 5 || (now.getMonth() === 5 && now.getDate() >= 26);
  if (!hadBirthday) age -= 1;
  return age;
}

import type { Dictionary } from './ru';

const en: Dictionary = {
  meta: {
    title: 'Alijon — Web, Telegram Bots and AI Solutions',
    description:
      'I build modern websites, Telegram bots, Mini Apps and AI solutions. Full Stack development from Dushanbe.',
    langName: 'English',
  },

  nav: {
    about: 'About',
    skills: 'Skills',
    projects: 'Projects',
    process: 'Process',
    contact: 'Contact',
    order: 'Start a project',
  },

  preloader: {
    loading: 'Loading',
    enter: 'Enter',
    hint: 'Click to begin',
  },

  hero: {
    roles: ['Web Developer', 'Telegram Bot Developer', 'AI Enthusiast'],
    tagline: 'I build modern websites, Telegram bots, Mini Apps and AI solutions.',
    ctaOrder: 'Start a project',
    ctaProjects: 'See my work',
    scroll: 'Scroll down',
  },

  about: {
    eyebrow: 'About me',
    title: 'Early in the journey — and serious about it.',
    p1: "My name is Ali, online I go by Uways. I'm from Dushanbe and currently work in Kazakhstan. I'm {age}, and I'm building this career on purpose, not by accident.",
    p2: 'I work on websites, Telegram bots and Mini Apps, automation and AI solutions — moving steadily toward full stack development.',
    p3: 'My goal is to become a world-class developer and build digital products people actually use every day.',
    factAge: 'Age',
    factYears: '{age} years',
    factCity: 'Hometown',
    factBased: 'Based in',
    factLangs: 'Languages',
    langsValue: 'Русский · Тоҷикӣ · English',
    factFocus: 'Focus',
    focusValue: 'Full Stack · AI',
  },

  skills: {
    eyebrow: 'Stack',
    title: 'The tools I actually work with.',
    lead: 'Not a list for the sake of it — this is what my projects are built on.',
    frontend: 'Frontend',
    backend: 'Backend and data',
    tools: 'Tools',
  },

  projects: {
    eyebrow: 'Work',
    title: 'Projects I have built.',
    lead: 'From a messenger and a marketplace to Telegram bots and AI.',
    view: 'View project',
    soon: 'Soon',
    stack: 'Stack',

    payom: {
      title: 'Payom Messenger',
      tagline: 'Messenger',
      description:
        'A messenger with real-time conversations: chats, message delivery, stored history. The largest project I have built in terms of logic.',
    },
    tajbozor: {
      title: 'TAJBOZOR',
      tagline: 'Marketplace',
      description:
        'A marketplace for Tajikistan: product catalogue, search, seller profiles and orders. A project about moving local trade online.',
    },
    cargo: {
      title: 'Telegram Cargo Bot',
      tagline: 'Telegram bot',
      description:
        'A freight bot: takes requests, calculates, sends notifications. It lives inside Telegram, so the client installs nothing.',
    },
    miniapps: {
      title: 'Telegram Mini Apps',
      tagline: 'Mini Apps',
      description:
        'Full web apps running inside Telegram — the speed of a messenger with the capability of a website.',
    },
    ai: {
      title: 'AI projects',
      tagline: 'Artificial intelligence',
      description:
        'Connecting language models to bots and services: customer replies, text processing, routine automation.',
    },
    websites: {
      title: 'Websites',
      tagline: 'Web',
      description: 'Modern sites with motion and 3D — much like the one you are looking at.',
    },
  },

  process: {
    eyebrow: 'How I work',
    title: 'From idea to launch.',
    lead: 'A transparent process: you always know which stage the project is at.',
    steps: [
      { title: 'Idea', text: 'We work out what problem the product solves and who will use it.' },
      { title: 'Design', text: 'I build the interface and agree on it before any code is written.' },
      { title: 'Development', text: 'Writing the code: frontend, backend, database, integrations.' },
      { title: 'Testing', text: 'Checked on phones and desktops, catching issues before launch.' },
      { title: 'Launch', text: 'Deployed to hosting, with domain and analytics connected.' },
      { title: 'Support', text: 'I stay available: fixes, updates and new features.' },
    ],
  },

  order: {
    eyebrow: 'Request',
    title: 'Tell me about your project.',
    lead: "I'll reply on Telegram, usually within a day.",
    name: 'Name',
    namePlaceholder: 'What should I call you',
    telegram: 'Telegram',
    telegramPlaceholder: '@username',
    project: 'Project description',
    projectPlaceholder: 'What needs building, and who for',
    budget: 'Budget',
    budgetPlaceholder: 'For example, $300–500',
    deadline: 'Timeline',
    deadlinePlaceholder: 'For example, 3 weeks',
    submit: 'Send request',
    sending: 'Sending…',
    success: 'Request ready — opening Telegram.',
    optional: 'optional',
    errors: {
      nameShort: 'Enter your name — at least 2 characters',
      telegramShort: 'Add your Telegram so I can reply',
      projectShort: 'Describe the task briefly — at least 10 characters',
    },
  },

  contact: {
    eyebrow: 'Contact',
    title: "Let's build something worth building.",
    lead: 'Reach out on any channel — I reply quickly.',
    copy: 'Copy',
    copied: 'Copied',
  },

  footer: {
    rights: 'All rights reserved',
    built: 'Built with Next.js, Three.js and GSAP',
  },

  sound: {
    on: 'Turn sound on',
    off: 'Turn sound off',
  },
};

export default en;

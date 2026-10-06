'use client';

import { HiArrowUpRight } from 'react-icons/hi2';
import { useI18n } from '@/lib/i18n';
import { projects, type Project } from '@/config/site';
import Reveal from '@/components/ui/Reveal';
import TiltCard from '@/components/ui/TiltCard';
import ProjectGlyph from '@/components/ui/ProjectGlyph';

export default function Projects() {
  const { dict } = useI18n();

  return (
    <section className="section" id="projects">
      <div className="u-wrap">
        <Reveal>
          <span className="u-eyebrow">{dict.projects.eyebrow}</span>
        </Reveal>
        <Reveal delay={0.06}>
          <h2 className="section-title">{dict.projects.title}</h2>
        </Reveal>
        <Reveal delay={0.12}>
          <p className="section-lead">{dict.projects.lead}</p>
        </Reveal>

        <ul className="projects-grid">
          {projects.map((project, index) => (
            <li
              key={project.id}
              className={project.featured ? 'is-featured' : undefined}
            >
              <Reveal delay={Math.min(index * 0.06, 0.3)}>
                <ProjectCard project={project} />
              </Reveal>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function ProjectCard({ project }: { project: Project }) {
  const { dict } = useI18n();

  // Тексты проектов лежат в словарях, а не в конфиге: иначе их пришлось бы
  // дублировать на три языка прямо в настройках
  const copy = dict.projects[project.i18nKey as keyof typeof dict.projects] as {
    title: string;
    tagline: string;
    description: string;
  };

  const Wrapper = project.href ? 'a' : 'div';
  const linkProps = project.href
    ? { href: project.href, target: '_blank', rel: 'noopener noreferrer' }
    : {};

  return (
    <TiltCard accent={project.accent} className="project">
      <Wrapper className="project-inner" {...linkProps}>
        <div className="project-visual" style={{ ['--accent' as string]: project.accent }}>
          <ProjectGlyph shape={project.shape} />
        </div>

        <div className="project-head">
          <span className="project-tagline">{copy.tagline}</span>
          <span className="project-year">{project.year}</span>
        </div>

        <h3 className="project-title">{copy.title}</h3>
        <p className="project-desc">{copy.description}</p>

        <ul className="project-stack" aria-label={dict.projects.stack}>
          {project.stack.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>

        <span className="project-action">
          {project.href ? dict.projects.view : dict.projects.soon}
          {project.href && <HiArrowUpRight aria-hidden="true" />}
        </span>
      </Wrapper>
    </TiltCard>
  );
}

'use client';

import { useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { HiPaperAirplane } from 'react-icons/hi2';
import { useI18n } from '@/lib/i18n';
import { orderTarget } from '@/config/site';
import Reveal from '@/components/ui/Reveal';

type Status = 'idle' | 'sending' | 'done';

export default function OrderForm() {
  const { dict } = useI18n();
  const [status, setStatus] = useState<Status>('idle');

  // Схема пересобирается при смене языка, чтобы тексты ошибок были на нём же
  const schema = useMemo(
    () =>
      z.object({
        name: z.string().trim().min(2, dict.order.errors.nameShort),
        telegram: z.string().trim().min(2, dict.order.errors.telegramShort),
        project: z.string().trim().min(10, dict.order.errors.projectShort),
        budget: z.string().trim().optional(),
        deadline: z.string().trim().optional(),
      }),
    [dict],
  );

  type Values = z.infer<typeof schema>;

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<Values>({ resolver: zodResolver(schema) });

  const onSubmit = async (values: Values) => {
    setStatus('sending');

    const message = [
      'Заявка с сайта',
      `Имя: ${values.name}`,
      `Telegram: ${values.telegram}`,
      `Проект: ${values.project}`,
      values.budget ? `Бюджет: ${values.budget}` : null,
      values.deadline ? `Срок: ${values.deadline}` : null,
    ]
      .filter(Boolean)
      .join('\n');

    if (orderTarget.mode === 'php') {
      // Обработчик на хостинге сам отправит заявку в Telegram-бота
      try {
        await fetch(orderTarget.phpEndpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(values),
        });
      } catch {
        // Сеть отвалилась — ниже всё равно откроем Telegram как запасной путь
      }
    }

    // Telegram не умеет подставлять текст в личный чат по ссылке,
    // поэтому кладём заявку в буфер: человеку останется вставить её одним жестом
    try {
      await navigator.clipboard.writeText(message);
    } catch {
      // без буфера обмена просто откроем чат
    }

    setStatus('done');
    window.open(`https://t.me/${orderTarget.telegramUser}`, '_blank', 'noopener');
    window.setTimeout(() => setStatus('idle'), 4000);
  };

  return (
    <section className="section" id="order">
      <div className="u-wrap">
        <Reveal>
          <span className="u-eyebrow">{dict.order.eyebrow}</span>
        </Reveal>
        <Reveal delay={0.06}>
          <h2 className="section-title">{dict.order.title}</h2>
        </Reveal>
        <Reveal delay={0.12}>
          <p className="section-lead">{dict.order.lead}</p>
        </Reveal>

        <Reveal delay={0.18}>
          <form className="order u-glass" onSubmit={handleSubmit(onSubmit)} noValidate>
            <div className="order-row">
              <Field
                id="name"
                label={dict.order.name}
                placeholder={dict.order.namePlaceholder}
                error={errors.name?.message}
                register={register('name')}
              />
              <Field
                id="telegram"
                label={dict.order.telegram}
                placeholder={dict.order.telegramPlaceholder}
                error={errors.telegram?.message}
                register={register('telegram')}
              />
            </div>

            <Field
              id="project"
              label={dict.order.project}
              placeholder={dict.order.projectPlaceholder}
              error={errors.project?.message}
              register={register('project')}
              textarea
            />

            <div className="order-row">
              <Field
                id="budget"
                label={dict.order.budget}
                hint={dict.order.optional}
                placeholder={dict.order.budgetPlaceholder}
                register={register('budget')}
              />
              <Field
                id="deadline"
                label={dict.order.deadline}
                hint={dict.order.optional}
                placeholder={dict.order.deadlinePlaceholder}
                register={register('deadline')}
              />
            </div>

            <div className="order-foot">
              <button type="submit" className="btn btn-solid" disabled={status === 'sending'}>
                {status === 'sending' ? dict.order.sending : dict.order.submit}
                <HiPaperAirplane aria-hidden="true" />
              </button>

              {/* aria-live: экранный диктор сообщит об успехе, не забирая фокус */}
              <p className="order-status" role="status" aria-live="polite">
                {status === 'done' ? dict.order.success : ''}
              </p>
            </div>
          </form>
        </Reveal>
      </div>
    </section>
  );
}

function Field({
  id, label, placeholder, error, register, textarea, hint,
}: {
  id: string;
  label: string;
  placeholder: string;
  error?: string;
  register: ReturnType<ReturnType<typeof useForm>['register']>;
  textarea?: boolean;
  hint?: string;
}) {
  const errorId = `${id}-error`;

  return (
    <p className={`field ${error ? 'has-error' : ''}`}>
      <label htmlFor={id}>
        {label}
        {hint && <span className="field-hint">{hint}</span>}
      </label>

      {textarea ? (
        <textarea
          id={id}
          rows={4}
          placeholder={placeholder}
          aria-invalid={!!error}
          aria-describedby={error ? errorId : undefined}
          {...register}
        />
      ) : (
        <input
          id={id}
          type="text"
          placeholder={placeholder}
          aria-invalid={!!error}
          aria-describedby={error ? errorId : undefined}
          {...register}
        />
      )}

      {error && <span className="field-error" id={errorId}>{error}</span>}
    </p>
  );
}

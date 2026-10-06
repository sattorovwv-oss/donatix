import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../core/api.dart';
import '../widgets/ui.dart';
import '../core/navigation.dart';

class AccountScreen extends StatefulWidget {
  final DonatixApi api;
  final String section;
  const AccountScreen({super.key, required this.api, required this.section});
  @override
  State<AccountScreen> createState() => _AccountScreenState();
}

class _AccountScreenState extends State<AccountScreen> {
  int page = 1;
  int revision = 0;
  String movement = '';
  @override
  Widget build(BuildContext context) {
    final title = const {
      'transactions': 'Транзакции',
      'notifications': 'Уведомления',
      'logins': 'История входов',
      'referrals': 'Пригласить друзей',
    }[widget.section]!;
    return Scaffold(
      appBar: AppBar(title: Text(title)),
      body: AsyncPage(
        key: ValueKey('$page|$revision|$movement'),
        load: () => widget.api.get(
          widget.section == 'transactions'
              ? '/api/v1/transactions'
              : '/api/v1/mobile/${widget.section}',
          {'page': page, 'limit': 20, 'type': movement},
        ),
        builder: (context, d) => Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Heading(title),
            if (widget.section == 'transactions')
              Wrap(
                spacing: 8,
                children: [
                  for (final item in [
                    ('', 'Все движения'),
                    ('credit', 'Начисления'),
                    ('debit', 'Списания'),
                  ])
                    ChoiceChip(
                      label: Text(item.$2),
                      selected: movement == item.$1,
                      onSelected: (_) => setState(() {
                        movement = item.$1;
                        page = 1;
                      }),
                    ),
                ],
              ),
            if (widget.section == 'notifications' &&
                (d['unread'] as int? ?? 0) > 0)
              BusyButton(
                'Отметить прочитанными',
                onPressed: () async {
                  await widget.api.post(
                    '/api/v1/mobile/notifications/read',
                    {},
                  );
                  if (mounted) setState(() => revision++);
                },
              ),
            if (widget.section == 'referrals') ...[
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Пригласите друзей и получайте ${d['percent']}% по правилам сервиса.',
                    ),
                    const SizedBox(height: 16),
                    SelectableText(text(d['link'])),
                    const SizedBox(height: 12),
                    BusyButton(
                      'Копировать ссылку',
                      onPressed: () async {
                        await Clipboard.setData(
                          ClipboardData(text: text(d['link'])),
                        );
                        if (context.mounted) {
                          message(context, 'Ссылка скопирована');
                        }
                      },
                    ),
                  ],
                ),
              ),
              for (final e in (d['stats'] as Map).entries)
                if (e.value is! List && e.value is! Map)
                  Surface(
                    child: InfoRow(
                      const {
                            'invited': 'Приглашено',
                            'active': 'Активных',
                            'earned': 'Начислено',
                            'total_micro': 'Начислено (микро USD)',
                            'count': 'Приглашено',
                          }[e.key] ??
                          text(e.key),
                      e.key == 'earned'
                          ? widget.api.displayPrice(e.value)
                          : text(e.value),
                    ),
                  ),
              const Heading('Начисления'),
              if ((d['rewards'] as List).isEmpty)
                const Surface(child: Text('Начислений пока нет.')),
              for (final r in d['rewards'] as List)
                Surface(
                  child: ListTile(
                    contentPadding: EdgeInsets.zero,
                    title: Text(
                      '+${widget.api.displayPrice(r['earned'])} · ${r['login']}',
                    ),
                    subtitle: Text('${r['order_id']} · ${r['created_at']}'),
                    onTap: () => openDonatixLink(
                      context,
                      widget.api,
                      '/panel/orders/${r['order_id']}',
                    ),
                  ),
                ),
              const Heading('Приглашённые'),
              for (final f in d['friends'] as List)
                Surface(
                  child: InfoRow(text(f['login']), text(f['created_at'])),
                ),
            ] else ...[
              if ((d['items'] as List).isEmpty)
                const Surface(child: Text('Записей пока нет.')),
              ...(d['items'] as List).map(
                (r) => Surface(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (widget.section == 'transactions') ...[
                        InfoRow(
                          'Баланс до',
                          widget.api.displayPrice(r['balanceBefore']),
                        ),
                        InfoRow(
                          text(r['note'] ?? r['type']),
                          widget.api.displayPrice(r['amount']),
                        ),
                        InfoRow(
                          'Баланс после',
                          widget.api.displayPrice(r['balanceAfter']),
                        ),
                        if (r['orderId'] != null)
                          TextButton(
                            onPressed: () => openDonatixLink(
                              context,
                              widget.api,
                              '/panel/orders/${r['orderId']}',
                            ),
                            child: Text('Заказ ${r['orderId']}'),
                          ),
                      ],
                      if (widget.section == 'notifications') ...[
                        if (text(r['title']).isNotEmpty)
                          Text(
                            text(r['title']),
                            style: const TextStyle(fontWeight: FontWeight.w700),
                          ),
                        SelectableText(text(r['text'])),
                        if (text(r['link']).isNotEmpty)
                          TextButton.icon(
                            onPressed: () => openDonatixLink(
                              context,
                              widget.api,
                              text(r['link']),
                            ),
                            icon: const Icon(Icons.open_in_new, size: 16),
                            label: const Text('Открыть'),
                          ),
                        TextButton.icon(
                          onPressed: () async {
                            await Clipboard.setData(
                              ClipboardData(text: text(r['text'])),
                            );
                            if (context.mounted) {
                              message(context, 'Скопировано');
                            }
                          },
                          icon: const Icon(Icons.copy, size: 16),
                          label: const Text('Копировать'),
                        ),
                      ],
                      if (widget.section == 'logins') ...[
                        InfoRow('IP', text(r['ip'])),
                        Text(text(r['user_agent'])),
                        InfoRow(
                          'Сессия',
                          r['ended_at'] == null ? 'Активна' : 'Завершена',
                        ),
                      ],
                      const SizedBox(height: 6),
                      Text(
                        text(r['createdAt'] ?? r['created_at']),
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                    ],
                  ),
                ),
              ),
              if (widget.section == 'transactions')
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    TextButton(
                      onPressed: page > 1 ? () => setState(() => page--) : null,
                      child: const Text('Назад'),
                    ),
                    Text('$page'),
                    TextButton(
                      onPressed: page * 20 < (d['total'] as int)
                          ? () => setState(() => page++)
                          : null,
                      child: const Text('Далее'),
                    ),
                  ],
                ),
            ],
          ],
        ),
      ),
    );
  }
}

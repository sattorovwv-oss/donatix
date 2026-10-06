import 'package:flutter/material.dart';
import 'package:decimal/decimal.dart';
import '../core/api.dart';
import '../widgets/ui.dart';
import '../core/navigation.dart';
import 'account.dart';

class HomeScreen extends StatelessWidget {
  final DonatixApi api;
  final ValueChanged<int> navigate;
  const HomeScreen({super.key, required this.api, required this.navigate});
  @override
  Widget build(BuildContext context) => AsyncPage(
    load: () async {
      final me = await api.get('/api/v1/me');
      final home = await api.get('/api/v1/mobile/home');
      return {...me, ...home};
    },
    builder: (context, me) => Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Heading('Салом, ${me['login']} 👋'),
        if (Decimal.parse(text(me['balance'])) <
            Decimal.parse(text(me['low_usd'] ?? '0')))
          Surface(
            child: Row(
              children: [
                const Icon(
                  Icons.account_balance_wallet_outlined,
                  color: Colors.amber,
                ),
                const SizedBox(width: 12),
                const Expanded(
                  child: Text(
                    'Баланс заканчивается. Пополните его перед покупкой.',
                  ),
                ),
                TextButton(
                  onPressed: () => navigate(2),
                  child: const Text('Пополнить'),
                ),
              ],
            ),
          ),
        Container(
          padding: const EdgeInsets.all(18),
          margin: const EdgeInsets.only(bottom: 22),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: accent.withValues(alpha: .3)),
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [
                accent.withValues(alpha: .1),
                Theme.of(context).colorScheme.surface,
              ],
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                api.displayPrice(me['balance']),
                style: const TextStyle(
                  fontSize: 32,
                  fontWeight: FontWeight.w800,
                ),
              ),
              const SizedBox(height: 4),
              const Text('Доступно'),
              const SizedBox(height: 16),
              BusyButton('Пополнить', onPressed: () => navigate(2)),
              const SizedBox(height: 18),
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('Всего потрачено'),
                    Text(
                      api.displayPrice(me['summary']['totalSpent']),
                      style: const TextStyle(
                        fontSize: 24,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                    const Text(
                      'На выполненные заказы',
                      style: TextStyle(fontSize: 12),
                    ),
                  ],
                ),
              ),
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('Выполнено заказов'),
                    Text(
                      text(me['summary']['totalOrders']),
                      style: const TextStyle(
                        fontSize: 24,
                        color: accent,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                    const Text(
                      'История покупок',
                      style: TextStyle(fontSize: 12),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        if ((me['popular'] as List).isNotEmpty) ...[
          const Heading('Купить быстро'),
          SizedBox(
            height: 160,
            child: ListView(
              scrollDirection: Axis.horizontal,
              children: (me['popular'] as List)
                  .map(
                    (p) => Padding(
                      padding: const EdgeInsets.only(right: 12),
                      child: SizedBox(
                        width: 130,
                        child: Surface(
                          padding: EdgeInsets.zero,
                          child: InkWell(
                            borderRadius: BorderRadius.circular(16),
                            onTap: () =>
                                openDonatixLink(context, api, text(p['href'])),
                            child: Padding(
                              padding: const EdgeInsets.all(12),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  ProductImage(p['image_url'], size: 58),
                                  const SizedBox(height: 12),
                                  Text(
                                    text(p['title']),
                                    maxLines: 3,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  )
                  .toList(),
            ),
          ),
        ],
        if (me['dcoin_enabled'] == true)
          Surface(
            padding: EdgeInsets.zero,
            child: ListTile(
              leading: const CircleAvatar(
                backgroundColor: Color(0xffffc247),
                child: Text('D', style: TextStyle(fontWeight: FontWeight.w800)),
              ),
              title: Text(
                '${me['dcoin']['balance_text']} D',
                style: const TextStyle(fontWeight: FontWeight.w800),
              ),
              subtitle: Text('${me['dcoin']['change']}% за сегодня'),
              trailing: const Icon(Icons.chevron_right),
              onTap: () => openDonatixLink(context, api, '/panel/dcoin'),
            ),
          ),
        Surface(
          padding: EdgeInsets.zero,
          child: ListTile(
            leading: const Icon(Icons.group_add_outlined, color: accent),
            title: const Text('Пригласите друзей'),
            subtitle: Text(
              'Получайте ${me['referral_percent']}% по правилам сервиса',
            ),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.push(
              context,
              MaterialPageRoute<void>(
                builder: (_) => AccountScreen(api: api, section: 'referrals'),
              ),
            ),
          ),
        ),
        Surface(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: CircleAvatar(
                  child: Text(
                    text(me['login'])
                        .substring(
                          0,
                          text(me['login']).length < 2
                              ? text(me['login']).length
                              : 2,
                        )
                        .toUpperCase(),
                  ),
                ),
                title: Text(
                  text(me['login']),
                  style: const TextStyle(fontWeight: FontWeight.w700),
                ),
                subtitle: Text(text(me['email'])),
              ),
              const SizedBox(height: 12),
              const Heading(
                'Быстрые действия',
                subtitle: 'Заказы, движения и поддержка',
              ),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Заказы'),
                subtitle: const Text('История и статусы'),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => navigate(3),
              ),
              for (final e in [
                ('/panel/transactions', 'Транзакции', 'Все движения баланса'),
                ('/panel/stats', 'Аналитика', 'Расходы и заказы по дням'),
                ('/panel/support', 'Поддержка', 'Написать в Telegram'),
                ('/panel/logins', 'История входов', 'Данные и безопасность'),
                (
                  '/panel/api',
                  'API-ключи и webhook',
                  'Подключение вашего магазина',
                ),
                ('/panel/timezone', 'Часовой пояс', 'Даты и отчёты'),
                (
                  '/panel/bots',
                  'Конструктор ботов',
                  'Ваши магазины в Telegram',
                ),
              ])
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text(e.$2),
                  subtitle: Text(e.$3),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: () => openDonatixLink(context, api, e.$1),
                ),
            ],
          ),
        ),
        Surface(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('УРОВЕНЬ', style: TextStyle(fontSize: 11)),
              const SizedBox(height: 8),
              Text(
                text(me['tier']),
                style: Theme.of(context).textTheme.titleLarge,
              ),
              InfoRow('Статус', statusTitle(me['status'])),
              if (me['markup'] != null) InfoRow('Наценка', '${me['markup']}%'),
              if (me['orders_all'] != null)
                InfoRow('Всего заказов', text(me['orders_all'])),
              InfoRow('Email', text(me['email'])),
              const InfoRow('Валюта счёта', 'USD'),
              InfoRow('Регистрация', text(me['createdAt'])),
              if (text(me['project']).isNotEmpty)
                InfoRow('Проект', text(me['project'])),
            ],
          ),
        ),
        if (text(me['tg_channel']).isNotEmpty)
          Surface(
            child: ListTile(
              contentPadding: EdgeInsets.zero,
              leading: const Icon(Icons.telegram, color: accent),
              title: const Text('Наш Telegram-канал'),
              trailing: const Icon(Icons.open_in_new),
              onTap: () =>
                  openDonatixLink(context, api, text(me['tg_channel'])),
            ),
          ),
      ],
    ),
  );
}

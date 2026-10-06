import 'package:flutter/material.dart';
import '../core/api.dart';
import '../widgets/ui.dart';
import '../widgets/charts.dart';

class StatsScreen extends StatefulWidget {
  final DonatixApi api;
  const StatsScreen({super.key, required this.api});
  @override
  State<StatsScreen> createState() => _StatsScreenState();
}

class _StatsScreenState extends State<StatsScreen> {
  String period = '30d';
  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Аналитика')),
    body: AsyncPage(
      key: ValueKey(period),
      load: () => widget.api.get('/api/v1/mobile/stats', {
        'period': period,
        'tz_offset': DateTime.now().timeZoneOffset.inHours,
      }),
      builder: (context, d) {
        final a = d['analytics'] as Map;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Heading('Аналитика'),
            Wrap(
              spacing: 8,
              children: (a['periods'] as Map).entries
                  .map(
                    (e) => ChoiceChip(
                      label: Text(text(e.value)),
                      selected: period == e.key,
                      onSelected: (_) => setState(() => period = text(e.key)),
                    ),
                  )
                  .toList(),
            ),
            const SizedBox(height: 16),
            Surface(
              child: Column(
                children: [
                  InfoRow('Создано заказов', text(a['created'])),
                  InfoRow('Выполнено', text(a['done'])),
                  InfoRow('Возвраты', text(a['refunded'])),
                  InfoRow('Оборот', widget.api.displayPrice(a['turnover'])),
                  InfoRow('Пополнено', widget.api.displayPrice(a['topped'])),
                ],
              ),
            ),
            Surface(
              child: Column(
                children: [
                  const Heading('Динамика заказов'),
                  OrdersChart(series: a['series'] as List),
                  Text(
                    'Время: UTC${(a['tz_hours'] as int) >= 0 ? '+' : ''}${a['tz_hours']}',
                    style: const TextStyle(fontSize: 12),
                  ),
                ],
              ),
            ),
            const Heading('По типам товаров'),
            if ((a['by_kind'] as List).isEmpty)
              const Surface(child: Text('За этот период заказов нет.')),
            for (final k in a['by_kind'] as List)
              Surface(
                child: Column(
                  children: [
                    Heading(text(k['title'])),
                    InfoRow('Создано', text(k['created'])),
                    InfoRow('Выполнено', text(k['done'] ?? 0)),
                    InfoRow('Возвраты', text(k['refunded'] ?? 0)),
                    InfoRow('Оборот', widget.api.displayPrice(k['turnover'])),
                  ],
                ),
              ),
          ],
        );
      },
    ),
  );
}

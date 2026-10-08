import 'dart:convert';
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../core/api.dart';
import '../widgets/ui.dart';
import '../core/checkout.dart';

class OrdersScreen extends StatefulWidget {
  final DonatixApi api;
  const OrdersScreen({super.key, required this.api});
  @override
  State<OrdersScreen> createState() => _OrdersScreenState();
}

class _OrdersScreenState extends State<OrdersScreen> {
  String status = '';
  String period = 'all', query = '';
  DateTimeRange? dates;
  final search = TextEditingController();
  int page = 1;
  @override
  void dispose() {
    search.dispose();
    super.dispose();
  }

  String date(DateTime value) => value.toIso8601String().substring(0, 10);
  @override
  Widget build(BuildContext context) => AsyncPage(
    key: ValueKey('$status|$page|$period|$query|$dates'),
    load: () async {
      final d = await widget.api.get('/api/v1/orders', {
        'page': page,
        'limit': 20,
        'status': status,
        'q': query,
        'period': period,
        if (dates != null) 'date_from': date(dates!.start),
        if (dates != null) 'date_to': date(dates!.end),
      });
      d['pending'] = await widget.api.storage.read(
        key: widget.api.pendingOrderKey,
      );
      return d;
    },
    builder: (context, d) => Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Heading('Заказы', subtitle: 'История покупок и статусы выдачи'),
        TextField(
          controller: search,
          decoration: InputDecoration(
            hintText: 'Номер, товар или ID игрока',
            suffixIcon: IconButton(
              onPressed: () => setState(() {
                query = search.text.trim();
                page = 1;
              }),
              icon: const Icon(Icons.search),
            ),
          ),
          onSubmitted: (v) => setState(() {
            query = v.trim();
            page = 1;
          }),
        ),
        const SizedBox(height: 12),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final p in [
              ('all', 'Всё время'),
              ('today', 'Сегодня'),
              ('yesterday', 'Вчера'),
              ('7d', '7 дней'),
              ('30d', '30 дней'),
              ('month', 'Этот месяц'),
            ])
              ChoiceChip(
                label: Text(p.$2),
                selected: period == p.$1 && dates == null,
                onSelected: (_) => setState(() {
                  period = p.$1;
                  dates = null;
                  page = 1;
                }),
              ),
            ActionChip(
              label: Text(
                dates == null
                    ? 'Выбрать даты'
                    : '${date(dates!.start)} — ${date(dates!.end)}',
              ),
              onPressed: () async {
                final picked = await showDateRangePicker(
                  context: context,
                  firstDate: DateTime(2020),
                  lastDate: DateTime.now(),
                  initialDateRange: dates,
                );
                if (mounted && picked != null) {
                  setState(() {
                    dates = picked;
                    page = 1;
                  });
                }
              },
            ),
          ],
        ),
        const SizedBox(height: 12),
        if (d['totals'] != null)
          Surface(
            child: Column(
              children: [
                InfoRow(text(d['period']), text(d['total'])),
                InfoRow('Выполнено', text(d['totals']['done'])),
                InfoRow(
                  'Потрачено',
                  widget.api.displayPrice(d['totals']['spent']),
                ),
                InfoRow('Возвращено заказов', text(d['totals']['failed'])),
              ],
            ),
          ),
        if (d['pending'] != null)
          Surface(
            child: ListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Проверить незавершённый запрос'),
              subtitle: const Text('Связь могла прерваться после списания.'),
              trailing: const Icon(Icons.chevron_right),
              onTap: () {
                Navigator.push(
                  context,
                  MaterialPageRoute<void>(
                    builder: (_) => PendingOrderScreen(api: widget.api),
                  ),
                );
              },
            ),
          ),
        Wrap(
          spacing: 8,
          children: ['', 'processing', 'completed', 'failed']
              .map(
                (s) => ChoiceChip(
                  label: Text(s.isEmpty ? 'Все' : statusTitle(s)),
                  selected: status == s,
                  onSelected: (_) => setState(() {
                    status = s;
                    page = 1;
                  }),
                ),
              )
              .toList(),
        ),
        const SizedBox(height: 16),
        if ((d['items'] as List).isEmpty)
          const Surface(child: Text('Заказов пока нет.')),
        ...(d['items'] as List).map(
          (o) => Surface(
            padding: EdgeInsets.zero,
            child: ListTile(
              leading: Icon(
                o['status'] == 'completed'
                    ? Icons.check_circle_outline
                    : Icons.schedule,
                color: o['status'] == 'completed' ? Colors.green : accent,
              ),
              title: Text(text(o['product_name'])),
              subtitle: Text(
                '${statusTitle(o['status'])}\n${o['order_id']} · ${o['created_at']}',
              ),
              trailing: Text(widget.api.displayPrice(o['total_usd'])),
              isThreeLine: true,
              onTap: () => Navigator.push(
                context,
                MaterialPageRoute<void>(
                  builder: (_) => OrderScreen(
                    api: widget.api,
                    orderId: text(o['order_id']),
                  ),
                ),
              ),
            ),
          ),
        ),
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            TextButton(
              onPressed: page > 1 ? () => setState(() => page--) : null,
              child: const Text('Назад'),
            ),
            Text('$page'),
            TextButton(
              onPressed: page * (d['limit'] as int? ?? 20) < (d['total'] as int)
                  ? () => setState(() => page++)
                  : null,
              child: const Text('Далее'),
            ),
          ],
        ),
      ],
    ),
  );
}

class OrderScreen extends StatefulWidget {
  final DonatixApi api;
  final String orderId;
  const OrderScreen({super.key, required this.api, required this.orderId});
  @override
  State<OrderScreen> createState() => _OrderScreenState();
}

class _OrderScreenState extends State<OrderScreen> with WidgetsBindingObserver {
  Map<String, dynamic>? order;
  Object? error;
  Timer? timer;
  bool loading = false;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    refresh();
  }

  @override
  void dispose() {
    timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    timer?.cancel();
    if (state == AppLifecycleState.resumed) refresh();
  }

  Future<void> refresh() async {
    if (loading) return;
    loading = true;
    timer?.cancel();
    try {
      final d = await widget.api.get(
        '/api/v1/orders/${Uri.encodeComponent(widget.orderId)}',
      );
      if (mounted) {
        setState(() {
          order = Map<String, dynamic>.from(d['order'] as Map);
          error = null;
        });
      }
    } catch (e) {
      if (mounted) setState(() => error = e);
    } finally {
      loading = false;
      if (mounted &&
          (order == null ||
              !['completed', 'failed'].contains(order!['status'])) &&
          WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed) {
        timer = Timer(const Duration(seconds: 20), refresh);
      }
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('Заказ'),
      actions: [
        IconButton(onPressed: refresh, icon: const Icon(Icons.refresh)),
      ],
    ),
    body: order == null
        ? StateView(error: error, retry: refresh)
        : ListView(
            padding: const EdgeInsets.all(18),
            children: [
              Heading(text(order!['product_name']), subtitle: widget.orderId),
              if (error != null) Surface(child: Text('$error')),
              Surface(
                child: Column(
                  children: [
                    Icon(
                      order!['status'] == 'completed'
                          ? Icons.check_circle
                          : Icons.schedule,
                      size: 48,
                      color: accent,
                    ),
                    const SizedBox(height: 12),
                    Text(
                      statusTitle(order!['status']),
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                    InfoRow('Количество', text(order!['quantity'])),
                    InfoRow(
                      'Сумма',
                      widget.api.displayPrice(order!['total_usd']),
                    ),
                    InfoRow('Создан', text(order!['created_at'])),
                    ...Map<String, dynamic>.from(
                      order!['fields'] as Map,
                    ).entries.map((e) => InfoRow(e.key, text(e.value))),
                    if (order!['error'] != null) Text(text(order!['error'])),
                  ],
                ),
              ),
              if (order!['delivery'] != null)
                Surface(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Heading('Результат выдачи'),
                      SelectableText(
                        const JsonEncoder.withIndent(
                          '  ',
                        ).convert(order!['delivery']),
                      ),
                      const SizedBox(height: 12),
                      OutlinedButton.icon(
                        onPressed: () async {
                          await Clipboard.setData(
                            ClipboardData(text: jsonEncode(order!['delivery'])),
                          );
                          if (context.mounted) message(context, 'Скопировано');
                        },
                        icon: const Icon(Icons.copy),
                        label: const Text('Копировать'),
                      ),
                    ],
                  ),
                ),
            ],
          ),
  );
}

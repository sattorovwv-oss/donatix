import 'dart:convert';
import 'dart:math';
import 'package:flutter/material.dart';
import 'api.dart';
import '../widgets/ui.dart';
import '../screens/orders.dart';

String operationId() => List.generate(
  24,
  (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0'),
).join();

Future<bool> confirmAction(
  BuildContext context,
  String title,
  String detail, {
  String action = 'Подтвердить',
}) async =>
    await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(title),
        content: Text(detail),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(c, false),
            child: const Text('Отмена'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(c, true),
            child: Text(action),
          ),
        ],
      ),
    ) ==
    true;

/// A request with an unknown outcome is recovered before a new charge is allowed.
Future<void> checkout(
  BuildContext context,
  DonatixApi api,
  Map<String, dynamic> body,
  String title,
) async {
  if (api.userId == 0) {
    api.nextLink =
        '/panel/buy/${Uri.encodeComponent(text(body['product_id']))}';
    api.onSessionExpired?.call();
    return;
  }
  final lease = api.beginOrder();
  if (lease == null) {
    throw const ApiFailure(
      'Другая покупка уже оформляется. Дождитесь её результата.',
    );
  }
  try {
    await _checkout(context, api, body, title, lease);
  } finally {
    api.endOrder(lease);
  }
}

Future<void> _checkout(
  BuildContext context,
  DonatixApi api,
  Map<String, dynamic> body,
  String title,
  Object lease,
) async {
  final owner = api.userId;
  final pendingKey = api.pendingOrderKey;
  final pending = await api.storage.read(key: pendingKey);
  api.requireAccount(owner);
  if (pending != null) {
    api.endOrder(lease);
    if (context.mounted) {
      await Navigator.push(
        context,
        MaterialPageRoute<void>(builder: (_) => PendingOrderScreen(api: api)),
      );
    }
    return;
  }
  final quote = await api.post('/api/v1/mobile/orders/quote', body);
  api.requireAccount(owner);
  body = {...body, 'expected_total_usd': quote['total_usd']};
  if (!context.mounted) return;
  if (!await confirmAction(
    context,
    'Подтвердите покупку',
    '$title\nПолучатель: ${(body['fields'] as Map).values.join(', ')}\nКоличество: ${body['quantity']}\n'
        'К оплате: ${api.displayPrice(quote['total_usd'])}\nНа балансе: ${api.displayPrice(quote['balance_usd'])}',
    action: 'Купить',
  )) {
    return;
  }
  api.requireAccount(owner);
  final key = operationId();
  await api.storage.write(
    key: pendingKey,
    value: jsonEncode({'key': key, 'body': body, 'title': title}),
  );
  try {
    api.requireAccount(owner);
    final d = await api.post('/api/v1/orders', body, idempotency: key);
    await api.storage.delete(key: pendingKey);
    api.requireAccount(owner);
    api.endOrder(lease);
    if (context.mounted) {
      await Navigator.push(
        context,
        MaterialPageRoute<void>(
          builder: (_) =>
              OrderScreen(api: api, orderId: text(d['order']['order_id'])),
        ),
      );
    }
  } catch (e) {
    if (e is ApiFailure &&
        e.status != null &&
        e.status! >= 400 &&
        e.status! < 500 &&
        e.status != 401 &&
        e.status != 408 &&
        e.status != 409 &&
        e.status != 429) {
      await api.storage.delete(key: pendingKey);
    }
    rethrow;
  }
}

class PendingOrderScreen extends StatefulWidget {
  final DonatixApi api;
  const PendingOrderScreen({super.key, required this.api});
  @override
  State<PendingOrderScreen> createState() => _PendingOrderScreenState();
}

class _PendingOrderScreenState extends State<PendingOrderScreen> {
  Map<String, dynamic>? pending;
  Object? error;
  bool busy = false;
  late final owner = widget.api.userId;
  late final pendingKey = widget.api.pendingOrderKey;
  @override
  void initState() {
    super.initState();
    load();
  }

  Future<void> load() async {
    try {
      widget.api.requireAccount(owner);
      final raw = await widget.api.storage.read(key: pendingKey);
      final parsed = raw == null
          ? <String, dynamic>{}
          : Map<String, dynamic>.from(jsonDecode(raw) as Map);
      if (parsed.isNotEmpty &&
          (parsed['key'] is! String ||
              (parsed['key'] as String).length < 16 ||
              parsed['body'] is! Map ||
              ![
                '',
                '/api/v1/orders',
                '/api/v1/mobile/cart',
              ].contains(text(parsed['endpoint'])))) {
        throw const FormatException('Invalid saved operation');
      }
      if (parsed.isNotEmpty) {
        final body = parsed['body'] as Map;
        final items = body['items'];
        if (body['fields'] is! Map ||
            (items != null &&
                (items is! List ||
                    items.any(
                      (item) =>
                          item is! Map ||
                          (int.tryParse(text(item['count'])) ?? 0) <= 0,
                    ))) ||
            (items == null &&
                (text(body['product_id']).isEmpty ||
                    (int.tryParse(text(body['quantity'])) ?? 0) <= 0))) {
          throw const FormatException('Invalid saved purchase');
        }
      }
      if (mounted) {
        setState(() {
          pending = parsed;
          error = null;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(
          () => error = e is ApiFailure
              ? e
              : const ApiFailure(
                  'Не удалось прочитать незавершённую операцию. Проверьте историю заказов перед новой покупкой.',
                ),
        );
      }
    }
  }

  Future<void> recover() async {
    if (busy || pending == null || pending!.isEmpty) return;
    final lease = widget.api.beginOrder();
    if (lease == null) {
      message(context, 'Другая покупка уже оформляется.');
      return;
    }
    setState(() {
      busy = true;
      error = null;
    });
    try {
      widget.api.requireAccount(owner);
      final d = await widget.api.post(
        text(pending!['endpoint']).isEmpty
            ? '/api/v1/orders'
            : text(pending!['endpoint']),
        pending!['body'],
        idempotency: text(pending!['key']),
      );
      await widget.api.storage.delete(key: pendingKey);
      widget.api.requireAccount(owner);
      widget.api.endOrder(lease);
      if (mounted) {
        if (d['order'] != null) {
          await Navigator.pushReplacement(
            context,
            MaterialPageRoute<void>(
              builder: (_) => OrderScreen(
                api: widget.api,
                orderId: text(d['order']['order_id']),
              ),
            ),
          );
        } else {
          message(
            context,
            text(d['error']).isEmpty
                ? 'Операции восстановлены. Проверьте историю заказов.'
                : text(d['error']),
          );
          Navigator.pop(context);
        }
      }
    } catch (e) {
      if (mounted) setState(() => error = e);
    } finally {
      widget.api.endOrder(lease);
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> openHistory() async {
    try {
      widget.api.requireAccount(owner);
      await Navigator.push(
        context,
        MaterialPageRoute<void>(
          builder: (_) => Scaffold(
            appBar: AppBar(title: const Text('История заказов')),
            body: OrdersScreen(api: widget.api),
          ),
        ),
      );
    } catch (e) {
      if (mounted) message(context, e);
    }
  }

  Widget operationSummary() {
    final body = Map<String, dynamic>.from(pending!['body'] as Map);
    final items = body['items'] as List?;
    final count = items == null
        ? text(body['quantity'])
        : '${items.fold<int>(0, (sum, item) => sum + (int.tryParse(text(item['count'])) ?? 0))}';
    final fields = body['fields'] as Map? ?? {};
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          text(pending!['title']).isNotEmpty
              ? text(pending!['title'])
              : items == null
              ? 'Покупка'
              : 'Корзина',
          style: Theme.of(context).textTheme.titleMedium,
        ),
        if (fields.isNotEmpty)
          InfoRow('Получатель', fields.values.map(text).join(', ')),
        if (count.isNotEmpty) InfoRow('Количество', count),
        if (body['expected_total_usd'] != null)
          InfoRow('Сумма', widget.api.displayPrice(body['expected_total_usd'])),
      ],
    );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Результат операции')),
    body: pending == null && error == null
        ? StateView(retry: load)
        : ListView(
            padding: const EdgeInsets.all(18),
            children: [
              const Heading('Проверить незавершённый запрос'),
              const Surface(
                child: Text(
                  'Связь могла прерваться после оформления заказа. Нажмите «Проверить результат», чтобы восстановить ответ по этой покупке. Также проверьте историю заказов.',
                ),
              ),
              if (pending != null && pending!.isNotEmpty)
                Surface(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      operationSummary(),
                      const SizedBox(height: 16),
                      BusyButton(
                        'Проверить результат',
                        busy: busy,
                        onPressed: recover,
                      ),
                    ],
                  ),
                )
              else if (pending != null)
                const Surface(child: Text('Незавершённых запросов нет.')),
              if (error != null) Surface(child: Text('$error')),
              OutlinedButton.icon(
                onPressed: busy ? null : openHistory,
                icon: const Icon(Icons.receipt_long_outlined),
                label: const Text('История заказов'),
              ),
            ],
          ),
  );
}

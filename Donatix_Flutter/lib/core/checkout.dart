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
  final pending = await api.storage.read(key: api.pendingOrderKey);
  if (pending != null) {
    if (context.mounted) {
      await Navigator.push(
        context,
        MaterialPageRoute<void>(builder: (_) => PendingOrderScreen(api: api)),
      );
    }
    return;
  }
  final quote = await api.post('/api/v1/mobile/orders/quote', body);
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
  final key = operationId();
  await api.storage.write(
    key: api.pendingOrderKey,
    value: jsonEncode({'key': key, 'body': body}),
  );
  try {
    final d = await api.post('/api/v1/orders', body, idempotency: key);
    await api.storage.delete(key: api.pendingOrderKey);
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
        e.status! < 500) {
      await api.storage.delete(key: api.pendingOrderKey);
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
  @override
  void initState() {
    super.initState();
    load();
  }

  Future<void> load() async {
    final raw = await widget.api.storage.read(key: widget.api.pendingOrderKey);
    if (mounted) {
      setState(
        () => pending = raw == null
            ? {}
            : Map<String, dynamic>.from(jsonDecode(raw) as Map),
      );
    }
  }

  Future<void> recover() async {
    if (busy || pending == null || pending!.isEmpty) return;
    setState(() {
      busy = true;
      error = null;
    });
    try {
      final d = await widget.api.post(
        text(pending!['endpoint']).isEmpty
            ? '/api/v1/orders'
            : text(pending!['endpoint']),
        pending!['body'],
        idempotency: text(pending!['key']),
      );
      await widget.api.storage.delete(key: widget.api.pendingOrderKey);
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
      if (e is ApiFailure &&
          e.status != null &&
          e.status! >= 400 &&
          e.status! < 500) {
        await widget.api.storage.delete(key: widget.api.pendingOrderKey);
      }
      if (mounted) setState(() => error = e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Результат операции')),
    body: pending == null
        ? StateView(error: error, retry: load)
        : ListView(
            padding: const EdgeInsets.all(18),
            children: [
              const Heading('Проверить незавершённый запрос'),
              const Surface(
                child: Text(
                  'При разрыве связи заказ мог быть оформлен. Проверка повторяет исходный запрос с тем же номером операции: сервер вернёт существующий результат.',
                ),
              ),
              if (pending!.isNotEmpty)
                Surface(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      SelectableText(
                        const JsonEncoder.withIndent(
                          '  ',
                        ).convert(pending!['body']),
                      ),
                      const SizedBox(height: 16),
                      BusyButton(
                        'Проверить результат',
                        busy: busy,
                        onPressed: recover,
                      ),
                    ],
                  ),
                )
              else
                const Surface(child: Text('Незавершённых запросов нет.')),
              if (error != null) Surface(child: Text('$error')),
            ],
          ),
  );
}

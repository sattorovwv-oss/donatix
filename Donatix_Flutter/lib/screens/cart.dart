import 'dart:convert';
import 'package:decimal/decimal.dart';
import 'package:flutter/material.dart';
import '../core/api.dart';
import '../core/checkout.dart';
import '../widgets/ui.dart';
import 'orders.dart';

class CartScreen extends StatefulWidget {
  final DonatixApi api;
  final List<Map<String, dynamic>> products;
  final Map<String, int> counts;
  const CartScreen({
    super.key,
    required this.api,
    required this.products,
    required this.counts,
  });
  @override
  State<CartScreen> createState() => _CartScreenState();
}

class _CartScreenState extends State<CartScreen> {
  final form = GlobalKey<FormState>();
  final fields = <String, TextEditingController>{};
  late final counts = Map<String, int>.from(widget.counts);
  bool busy = false;
  @override
  void initState() {
    super.initState();
    final p = widget.products.firstWhere(
      (p) => counts.containsKey(p['product_id']),
    );
    for (final f in p['fields'] as List) {
      fields[text(f['key'])] = TextEditingController();
    }
  }

  @override
  void dispose() {
    for (final c in fields.values) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> buy() async {
    if (busy || !(form.currentState?.validate() ?? false) || counts.isEmpty) {
      return;
    }
    if (widget.api.userId == 0) {
      widget.api.onSessionExpired?.call();
      return;
    }
    final lease = widget.api.beginOrder();
    if (lease == null) {
      message(
        context,
        'Другая покупка уже оформляется. Дождитесь её результата.',
      );
      return;
    }
    final owner = widget.api.userId;
    final pendingKey = widget.api.pendingOrderKey;
    setState(() => busy = true);
    try {
      if (await widget.api.storage.read(key: pendingKey) != null) {
        widget.api.requireAccount(owner);
        widget.api.endOrder(lease);
        if (mounted) {
          await Navigator.push(
            context,
            MaterialPageRoute<void>(
              builder: (_) => PendingOrderScreen(api: widget.api),
            ),
          );
        }
        return;
      }
      widget.api.requireAccount(owner);
      final body = <String, dynamic>{
        'items': [
          for (final e in counts.entries)
            {'product_id': e.key, 'count': e.value},
        ],
        'fields': {for (final e in fields.entries) e.key: e.value.text.trim()},
      };
      final quote = await widget.api.post('/api/v1/mobile/cart/quote', body);
      widget.api.requireAccount(owner);
      body['items'] = quote['items'];
      if (!mounted ||
          !await confirmAction(
            context,
            'Оформить корзину?',
            '${counts.values.fold<int>(0, (s, n) => s + n)} пакетов на один ID.\n'
                'К оплате: ${widget.api.displayPrice(quote['total_usd'])}\n'
                'На балансе: ${widget.api.displayPrice(quote['balance_usd'])}\n'
                'Каждый пакет — отдельный заказ. Деньги списываются за оформленные пакеты.',
          )) {
        return;
      }
      widget.api.requireAccount(owner);
      final key = operationId();
      await widget.api.storage.write(
        key: pendingKey,
        value: jsonEncode({
          'endpoint': '/api/v1/mobile/cart',
          'body': body,
          'key': key,
          'title': 'Корзина',
        }),
      );
      widget.api.requireAccount(owner);
      final d = await widget.api.post(
        '/api/v1/mobile/cart',
        body,
        idempotency: key,
      );
      await widget.api.storage.delete(key: pendingKey);
      widget.api.requireAccount(owner);
      widget.api.endOrder(lease);
      if (mounted) {
        message(
          context,
          d['partial'] == true
              ? 'Оформлено ${(d['items'] as List).length} из ${d['requested']}: ${d['error']}'
              : 'Оформлено ${(d['items'] as List).length} заказов.',
        );
        await Navigator.pushReplacement(
          context,
          MaterialPageRoute<void>(
            builder: (_) => Scaffold(
              appBar: AppBar(title: const Text('Заказы')),
              body: OrdersScreen(api: widget.api),
            ),
          ),
        );
      }
    } catch (e) {
      // A replay can fail after some cart items were already charged. Keep its
      // original idempotency key until the server confirms the complete result.
      if (mounted) message(context, e);
    } finally {
      widget.api.endOrder(lease);
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final selected = widget.products
        .where((p) => counts.containsKey(p['product_id']))
        .toList();
    var total = Decimal.zero;
    for (final p in selected) {
      total +=
          Decimal.parse(text(p['price_usd'])) *
          Decimal.fromInt(counts[p['product_id']]!);
    }
    return Scaffold(
      appBar: AppBar(title: const Text('Корзина — на один ID')),
      body: Form(
        key: form,
        child: ListView(
          padding: const EdgeInsets.all(18),
          children: [
            const Heading('Корзина — на один ID'),
            for (final p in selected)
              Surface(
                child: Column(
                  children: [
                    Text(text(p['title'])),
                    Row(
                      children: [
                        Text(widget.api.displayPrice(p['price_usd'])),
                        const Spacer(),
                        IconButton(
                          onPressed: busy
                              ? null
                              : () => setState(() {
                                  final id = text(p['product_id']);
                                  if (counts[id] == 1) {
                                    counts.remove(id);
                                  } else {
                                    counts[id] = counts[id]! - 1;
                                  }
                                }),
                          icon: const Icon(Icons.remove_circle_outline),
                        ),
                        Text('${counts[p['product_id']]}'),
                        IconButton(
                          onPressed:
                              busy ||
                                  counts.values.fold<int>(0, (s, n) => s + n) >=
                                      20
                              ? null
                              : () => setState(
                                  () => counts[text(p['product_id'])] =
                                      counts[text(p['product_id'])]! + 1,
                                ),
                          icon: const Icon(Icons.add_circle_outline),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            if (counts.isEmpty) const Surface(child: Text('Корзина пуста.')),
            if (selected.isNotEmpty)
              Surface(
                child: Column(
                  children: [
                    const Heading('Получатель'),
                    for (final f in selected.first['fields'] as List)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 12),
                        child: (f['options'] as List? ?? []).isNotEmpty
                            ? DropdownButtonFormField<String>(
                                decoration: InputDecoration(
                                  labelText: text(f['label']),
                                ),
                                items: [
                                  for (final o in f['options'] as List)
                                    DropdownMenuItem(
                                      value: text(o),
                                      child: Text(text(o)),
                                    ),
                                ],
                                onChanged: busy
                                    ? null
                                    : (v) => fields[text(f['key'])]!.text =
                                          v ?? '',
                                validator: (v) =>
                                    v == null ? 'Выберите значение' : null,
                              )
                            : TextFormField(
                                controller: fields[text(f['key'])],
                                enabled: !busy,
                                decoration: InputDecoration(
                                  labelText: text(f['label']),
                                ),
                                validator: (v) => (v ?? '').trim().isEmpty
                                    ? 'Заполните поле'
                                    : null,
                              ),
                      ),
                  ],
                ),
              ),
            Surface(
              child: Column(
                children: [
                  InfoRow(
                    'Предварительно к оплате',
                    widget.api.displayPrice(total.toStringAsFixed(4)),
                  ),
                  const Text(
                    'Каждый пакет — отдельный заказ. Не выполнится — деньги вернутся на баланс.',
                  ),
                  const SizedBox(height: 12),
                  BusyButton(
                    'Оформить корзину',
                    busy: busy,
                    onPressed: counts.isEmpty ? null : buy,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

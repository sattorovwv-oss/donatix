import 'dart:convert';
import 'package:decimal/decimal.dart';
import 'package:flutter/material.dart';
import '../core/api.dart';
import '../widgets/ui.dart';
import '../widgets/product_art.dart';
import '../core/checkout.dart';

class PurchaseScreen extends StatefulWidget {
  final DonatixApi api;
  final String productId;
  const PurchaseScreen({super.key, required this.api, required this.productId});
  @override
  State<PurchaseScreen> createState() => _PurchaseScreenState();
}

class _PurchaseScreenState extends State<PurchaseScreen> {
  Map<String, dynamic>? product;
  Object? error;
  Map<String, dynamic>? regions;
  final form = GlobalKey<FormState>();
  final quantity = TextEditingController();
  final fields = <String, TextEditingController>{};
  bool busy = false;
  String? check;
  String? pendingKey;
  Map<String, dynamic>? pendingBody;
  @override
  void initState() {
    super.initState();
    load();
  }

  @override
  void dispose() {
    quantity.dispose();
    for (final c in fields.values) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> load() async {
    try {
      final d = await widget.api.get(
        '/api/v1/products/${Uri.encodeComponent(widget.productId)}',
      );
      final p = Map<String, dynamic>.from(d['product'] as Map);
      if (p['kind'] == 'game_key') {
        regions = await widget.api.get(
          '/api/v1/mobile/gamekeys/${Uri.encodeComponent(widget.productId)}/regions',
        );
      }
      final raw = await widget.api.storage.read(
        key: widget.api.pendingOrderKey,
      );
      if (raw != null) {
        final stored = jsonDecode(raw) as Map;
        if (stored['body']['product_id'] == widget.productId) {
          pendingKey = text(stored['key']);
          pendingBody = Map<String, dynamic>.from(stored['body'] as Map);
        }
      }
      if (!mounted) return;
      quantity.text = text(pendingBody?['quantity'] ?? p['min_quantity'] ?? 1);
      for (final f in p['fields'] as List) {
        final options = f['options'] as List?;
        fields[text(f['key'])] = TextEditingController(
          text: text(
            pendingBody?['fields']?[f['key']] ??
                ((options?.isNotEmpty ?? false) ? options!.first : ''),
          ),
        );
      }
      setState(() {
        product = p;
        error = null;
      });
    } catch (e) {
      if (mounted) setState(() => error = e);
    }
  }

  Map<String, String> get values =>
      fields.map((key, c) => MapEntry(key, c.text.trim()));
  String get total {
    try {
      return (Decimal.parse(text(product!['price_usd'])) *
              Decimal.fromInt(int.parse(quantity.text)))
          .toString();
    } catch (_) {
      return '—';
    }
  }

  Future<void> verify() async {
    if (!(form.currentState?.validate() ?? false)) return;
    setState(() => busy = true);
    try {
      final d = await widget.api.post('/api/v1/accounts/check', {
        'product_id': widget.productId,
        'fields': values,
      });
      if (mounted) {
        setState(
          () => check = d['supported'] == false
              ? 'Проверка этой игры недоступна.'
              : '${d['player_name'] ?? d['message'] ?? (d['valid'] == true ? 'Аккаунт найден' : 'Аккаунт не найден')}',
        );
      }
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> buy() async {
    if (!(form.currentState?.validate() ?? false) || busy) return;
    setState(() => busy = true);
    try {
      await checkout(context, widget.api, {
        'product_id': widget.productId,
        'quantity': int.parse(quantity.text),
        'fields': values,
      }, text(product!['title'] ?? product!['name']));
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Покупка')),
    body: product == null
        ? StateView(error: error, retry: load)
        : Form(
            key: form,
            child: ListView(
              padding: const EdgeInsets.all(18),
              children: [
                Row(
                  children: [
                    ProductImage(product!['image_url']),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Heading(
                        text(product!['category_name']),
                        subtitle: text(product!['kind_title']),
                      ),
                    ),
                  ],
                ),
                Surface(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          ProductArt(product!, size: 64),
                          const SizedBox(width: 12),
                          Expanded(
                            child: Heading(
                              text(product!['title'] ?? product!['name']),
                            ),
                          ),
                        ],
                      ),
                      InfoRow(
                        'Цена за единицу',
                        widget.api.displayPrice(product!['price_usd']),
                      ),
                      if (text(product!['region_title']).isNotEmpty)
                        InfoRow('Регион', text(product!['region_title'])),
                      TextFormField(
                        controller: quantity,
                        enabled: !busy && pendingKey == null,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(
                          labelText: 'Количество',
                        ),
                        onChanged: (_) => setState(() {}),
                        validator: (v) {
                          final n = int.tryParse(v ?? '');
                          final min = product!['min_quantity'] as int;
                          final max = product!['max_quantity'] as int;
                          return n == null || n < min || n > max
                              ? 'Допустимо от $min до $max'
                              : null;
                        },
                      ),
                    ],
                  ),
                ),
                Surface(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Heading(
                        (product!['fields'] as List).isEmpty
                            ? 'Что вы получите'
                            : 'Получатель',
                      ),
                      if ((product!['fields'] as List).isEmpty)
                        const Padding(
                          padding: EdgeInsets.only(bottom: 14),
                          child: Text(
                            'После оплаты ключ или код появится на странице заказа и придёт на ваш webhook.',
                          ),
                        ),
                      if (regions != null) ...[
                        if (regions!['known'] != true)
                          const Text(
                            'Информация о регионах сейчас недоступна.',
                          ),
                        if ((regions!['available'] as List? ?? []).isNotEmpty)
                          Text(
                            'Активация: ${(regions!['available'] as List).join(', ')}',
                          ),
                        if ((regions!['unavailable'] as List? ?? []).isNotEmpty)
                          Text(
                            'Недоступно: ${(regions!['unavailable'] as List).join(', ')}',
                          ),
                      ],
                      if (text(product!['delivery_note']).isNotEmpty)
                        Text(text(product!['delivery_note'])),
                      ...(product!['fields'] as List).map((f) {
                        final options = (f['options'] as List? ?? [])
                            .map(text)
                            .toList();
                        final controller = fields[text(f['key'])]!;
                        return Padding(
                          padding: const EdgeInsets.only(bottom: 14),
                          child: options.isNotEmpty
                              ? DropdownButtonFormField<String>(
                                  initialValue:
                                      options.contains(controller.text)
                                      ? controller.text
                                      : null,
                                  decoration: InputDecoration(
                                    labelText: text(f['label']),
                                  ),
                                  items: options
                                      .map(
                                        (s) => DropdownMenuItem(
                                          value: s,
                                          child: Text(s),
                                        ),
                                      )
                                      .toList(),
                                  onChanged: busy || pendingKey != null
                                      ? null
                                      : (v) {
                                          controller.text = v ?? '';
                                          check = null;
                                        },
                                  validator: (v) =>
                                      v == null ? 'Выберите значение' : null,
                                )
                              : TextFormField(
                                  controller: controller,
                                  enabled: !busy && pendingKey == null,
                                  decoration: InputDecoration(
                                    labelText: text(f['label']),
                                    hintText: text(f['placeholder']),
                                  ),
                                  onChanged: (_) =>
                                      setState(() => check = null),
                                  validator: (v) => (v ?? '').trim().isEmpty
                                      ? 'Заполните поле'
                                      : null,
                                ),
                        );
                      }),
                      if (product!['account_check'] == true)
                        OutlinedButton(
                          onPressed: busy ? null : verify,
                          child: const Text('Проверить ID'),
                        ),
                      if (check != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 12),
                          child: Text(check!),
                        ),
                    ],
                  ),
                ),
                Surface(
                  child: Column(
                    children: [
                      InfoRow('Итого', widget.api.displayPrice(total)),
                      if (pendingKey != null)
                        const Text(
                          'Результат предыдущего запроса неизвестен. Повторная проверка использует тот же номер операции и не создаёт второй заказ.',
                        ),
                      BusyButton(
                        pendingKey == null ? 'Купить' : 'Проверить результат',
                        busy: busy,
                        onPressed: buy,
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
  );
}

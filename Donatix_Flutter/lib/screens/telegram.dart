import 'package:decimal/decimal.dart';
import 'package:flutter/material.dart';
import '../core/api.dart';
import '../core/checkout.dart';
import '../widgets/ui.dart';

class TelegramScreen extends StatefulWidget {
  final DonatixApi api;
  final bool premium;
  const TelegramScreen({super.key, required this.api, this.premium = false});
  @override
  State<TelegramScreen> createState() => _TelegramScreenState();
}

class _TelegramScreenState extends State<TelegramScreen> {
  late bool premium = widget.premium;
  String? plan;
  final username = TextEditingController(),
      quantity = TextEditingController(text: '100');
  final form = GlobalKey<FormState>();
  bool busy = false;
  @override
  void dispose() {
    username.dispose();
    quantity.dispose();
    super.dispose();
  }

  Future<void> buy(Map p) async {
    if (!(form.currentState?.validate() ?? false)) return;
    setState(() => busy = true);
    try {
      await checkout(context, widget.api, {
        'product_id': p['product_id'],
        'quantity': premium ? 1 : int.parse(quantity.text),
        'fields': {
          for (final f in p['fields'] as List)
            text(f['key']): username.text.trim(),
        },
      }, text(p['name']));
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Telegram')),
    body: AsyncPage(
      load: () async {
        final all = await Future.wait([
          widget.api.get('/api/v1/products', {'kind': 'telegram_stars'}),
          widget.api.get('/api/v1/products', {'kind': 'telegram_premium'}),
          widget.api.userId == 0
              ? Future.value({'balance': '0.0000'})
              : widget.api.get('/api/v1/balance'),
        ]);
        return {
          'stars': all[0]['items'],
          'plans': all[1]['items'],
          'balance': all[2]['balance'],
        };
      },
      builder: (context, d) {
        final stars = d['stars'] as List, plans = d['plans'] as List;
        plans.sort(
          (a, b) => Decimal.parse(
            text(a['price_usd']),
          ).compareTo(Decimal.parse(text(b['price_usd']))),
        );
        if (!plans.any((p) => p['product_id'] == plan)) {
          plan = plans.isEmpty ? null : text(plans.first['product_id']);
        }
        final Map? p = premium
            ? (plans.isEmpty
                  ? null
                  : plans.firstWhere((p) => p['product_id'] == plan) as Map)
            : (stars.isEmpty ? null : stars.first as Map);
        final n = premium ? 1 : int.tryParse(quantity.text) ?? 0;
        final total = p == null
            ? Decimal.zero
            : ((Decimal.parse(text(p['price_usd'])) *
                              Decimal.fromInt(n) *
                              Decimal.fromInt(10000))
                          .ceil() /
                      Decimal.fromInt(10000))
                  .toDecimal(scaleOnInfinitePrecision: 4);
        return Form(
          key: form,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Heading(
                'Telegram звёзды и Premium',
                subtitle:
                    'Звёзды и Premium-подписка на любой аккаунт по @username',
              ),
              Surface(
                child: SizedBox(
                  width: double.infinity,
                  child: SegmentedButton<bool>(
                    segments: const [
                      ButtonSegment(
                        value: false,
                        label: Text('Звёзды'),
                        icon: Icon(Icons.star_outline),
                      ),
                      ButtonSegment(
                        value: true,
                        label: Text('Premium'),
                        icon: Icon(Icons.workspace_premium_outlined),
                      ),
                    ],
                    selected: {premium},
                    onSelectionChanged: busy
                        ? null
                        : (v) => setState(() => premium = v.first),
                  ),
                ),
              ),
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Heading('1 · Получатель'),
                    TextFormField(
                      controller: username,
                      enabled: !busy,
                      autocorrect: false,
                      decoration: const InputDecoration(
                        labelText: 'Имя пользователя',
                        hintText: '@username',
                      ),
                      onChanged: (_) => setState(() {}),
                      validator: (v) =>
                          RegExp(
                            r'^@?[A-Za-z][A-Za-z0-9_]{3,31}$',
                          ).hasMatch((v ?? '').trim())
                          ? null
                          : 'Введите Telegram username',
                    ),
                  ],
                ),
              ),
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Heading(
                      premium ? '2 · Выберите план' : '2 · Количество звёзд',
                    ),
                    if (p == null)
                      Text(
                        premium
                            ? 'Premium сейчас недоступен.'
                            : 'Звёзды сейчас недоступны.',
                      )
                    else if (premium)
                      RadioGroup<String>(
                        groupValue: plan,
                        onChanged: (v) {
                          if (!busy) setState(() => plan = v);
                        },
                        child: Column(
                          children: [
                            for (final pl in plans)
                              RadioListTile<String>(
                                value: text(pl['product_id']),
                                enabled: !busy,
                                title: Text(text(pl['name'])),
                                subtitle: Text(
                                  widget.api.displayPrice(pl['price_usd']),
                                ),
                              ),
                          ],
                        ),
                      )
                    else ...[
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          for (final q in [
                            50,
                            100,
                            250,
                            500,
                            1000,
                            2500,
                            5000,
                            10000,
                          ])
                            if (q >= (p['min_quantity'] as int) &&
                                q <= (p['max_quantity'] as int))
                              ChoiceChip(
                                label: Text('$q ★'),
                                selected: n == q,
                                onSelected: busy
                                    ? null
                                    : (_) =>
                                          setState(() => quantity.text = '$q'),
                              ),
                        ],
                      ),
                      const SizedBox(height: 16),
                      TextFormField(
                        controller: quantity,
                        enabled: !busy,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(
                          labelText: 'Или своё количество',
                        ),
                        onChanged: (_) => setState(() {}),
                        validator: (v) {
                          final x = int.tryParse(v ?? '');
                          return x == null ||
                                  x < (p['min_quantity'] as int) ||
                                  x > (p['max_quantity'] as int)
                              ? 'От ${p['min_quantity']} до ${p['max_quantity']}'
                              : null;
                        },
                      ),
                      const SizedBox(height: 8),
                      Text(
                        'От ${p['min_quantity']} до ${p['max_quantity']} · ${widget.api.displayPrice(p['price_usd'])} за звезду',
                        style: const TextStyle(fontSize: 12),
                      ),
                    ],
                  ],
                ),
              ),
              Surface(
                child: Column(
                  children: [
                    const Heading('Сводка'),
                    InfoRow('Получатель', username.text),
                    InfoRow('Товар', premium ? text(p?['name']) : '$n звёзд'),
                    InfoRow(
                      'Ваш баланс',
                      widget.api.displayPrice(d['balance']),
                    ),
                    InfoRow(
                      'К оплате',
                      widget.api.displayPrice(total.toStringAsFixed(4)),
                    ),
                    const SizedBox(height: 14),
                    BusyButton(
                      'Купить',
                      busy: busy,
                      onPressed: p == null ? null : () => buy(p),
                    ),
                    const SizedBox(height: 10),
                    const Text(
                      'Не выполнится — деньги вернутся на баланс.',
                      style: TextStyle(fontSize: 12),
                    ),
                  ],
                ),
              ),
            ],
          ),
        );
      },
    ),
  );
}

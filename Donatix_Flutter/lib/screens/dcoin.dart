import 'dart:async';
import 'dart:math';
import 'package:flutter/material.dart';
import '../core/api.dart';
import '../core/checkout.dart';
import '../widgets/ui.dart';
import '../widgets/charts.dart';

class DcoinScreen extends StatefulWidget {
  final DonatixApi api;
  const DcoinScreen({super.key, required this.api});
  @override
  State<DcoinScreen> createState() => _DcoinScreenState();
}

class _DcoinScreenState extends State<DcoinScreen> with WidgetsBindingObserver {
  Map<String, dynamic>? data;
  List candles = [];
  final amount = TextEditingController();
  String tf = '5s';
  Timer? timer;
  bool loading = false, busy = false;
  int tick = 0, revision = 0;
  Object? error;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    refresh();
  }

  @override
  void dispose() {
    timer?.cancel();
    amount.dispose();
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
    final current = revision;
    try {
      final values = await Future.wait([
        widget.api.get('/api/v1/mobile/dcoin/chart', {'tf': tf}),
        if (data == null || tick++ % 10 == 0)
          widget.api.get('/api/v1/mobile/dcoin'),
      ]);
      if (mounted && current == revision) {
        setState(() {
          candles = values.first['candles'] as List;
          if (values.length > 1) data = values[1];
          error = null;
        });
      }
    } catch (e) {
      if (mounted) setState(() => error = e);
    } finally {
      loading = false;
      if (mounted &&
          WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed) {
        timer = Timer(
          Duration(seconds: ['1s', '5s'].contains(tf) ? 1 : 3),
          refresh,
        );
      }
    }
  }

  String price(dynamic value) {
    var n = double.tryParse('$value') ?? 0;
    if (widget.api.displayCurrency == 'TJS') {
      n *= double.tryParse(widget.api.tjsRate) ?? 1;
    }
    final decimals = n <= 0
        ? 4
        : (2 - (log(n.abs()) / ln10).floor()).clamp(2, 12);
    return widget.api.displayCurrency == 'TJS'
        ? '${n.toStringAsFixed(decimals)} с.'
        : '\$${n.toStringAsFixed(decimals)}';
  }

  Future<void> exchange() async {
    if (busy || amount.text.trim().isEmpty) return;
    if (!await confirmAction(
      context,
      'Обменять D-коины?',
      '${amount.text.trim()} D будут обменены на баланс Donatix по текущей цене с комиссией ${data!['summary']['fee_pct']}%.',
    )) {
      return;
    }
    setState(() => busy = true);
    try {
      final d = await widget.api.post('/api/v1/mobile/dcoin/exchange', {
        'amount': amount.text.trim(),
      });
      if (mounted) {
        message(
          context,
          'Обменяли ${d['coins']} D — зачислено ${widget.api.displayPrice(d['credited_usd'])}.',
        );
      }
      amount.clear();
      data = null;
      await refresh();
    } catch (e) {
      if (mounted) {
        message(
          context,
          '$e\nПеред повторным обменом обновите баланс и историю.',
        );
      }
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final d = data?['summary'] as Map?;
    return Scaffold(
      appBar: AppBar(
        title: const Text('D-коин'),
        actions: [
          IconButton(onPressed: refresh, icon: const Icon(Icons.refresh)),
        ],
      ),
      body: d == null
          ? StateView(error: error, retry: refresh)
          : RefreshIndicator(
              onRefresh: () async {
                tick = 0;
                await refresh();
              },
              child: ListView(
                padding: const EdgeInsets.all(18),
                children: [
                  Row(
                    children: [
                      Container(
                        width: 52,
                        height: 52,
                        decoration: const BoxDecoration(
                          shape: BoxShape.circle,
                          gradient: LinearGradient(
                            colors: [Color(0xffffe08a), Color(0xffe9a00d)],
                          ),
                        ),
                        child: const Center(
                          child: Text(
                            'D',
                            style: TextStyle(
                              fontWeight: FontWeight.w900,
                              fontSize: 30,
                              color: Color(0xff7a4a00),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 14),
                      Expanded(
                        child: Heading(
                          'D-коин',
                          subtitle: 'цена 1 D · за сегодня (с 00:00)',
                        ),
                      ),
                      Text(
                        '${d['change']}%',
                        style: TextStyle(
                          color: (d['change'] as num) >= 0
                              ? Colors.green
                              : Colors.red,
                        ),
                      ),
                    ],
                  ),
                  AnimatedSwitcher(
                    duration: const Duration(milliseconds: 300),
                    child: Text(
                      price(d['price']),
                      key: ValueKey(d['price']),
                      style: const TextStyle(
                        fontWeight: FontWeight.w800,
                        fontSize: 28,
                      ),
                    ),
                  ),
                  if (error != null) Surface(child: Text('$error')),
                  Surface(
                    child: Column(
                      children: [
                        SingleChildScrollView(
                          scrollDirection: Axis.horizontal,
                          child: Row(
                            children: [
                              for (final t in data!['timeframes'] as List)
                                Padding(
                                  padding: const EdgeInsets.only(right: 6),
                                  child: ChoiceChip(
                                    label: Text('$t'),
                                    selected: t == tf,
                                    onSelected: (_) {
                                      setState(() {
                                        tf = '$t';
                                        revision++;
                                      });
                                      refresh();
                                    },
                                  ),
                                ),
                            ],
                          ),
                        ),
                        CandleChart(candles: candles, money: price),
                      ],
                    ),
                  ),
                  Surface(
                    child: Column(
                      children: [
                        InfoRow('У вас', '${d['balance_text']} D'),
                        InfoRow(
                          'Стоимость с комиссией',
                          '≈ ${widget.api.displayPrice(d['worth_usd'])}',
                        ),
                        if (d['waiting_text'] != '0')
                          InfoRow(
                            'Ждут выполнения заказов',
                            '${d['waiting_text']} D',
                          ),
                        InfoRow('За покупку', '\$1 = ${d['per_usd']} D'),
                      ],
                    ),
                  ),
                  Surface(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Heading('Обменять на баланс'),
                        const Text(
                          'D-коины — бонусные: их нельзя купить или вывести на карту, только обменять на баланс Donatix.',
                        ),
                        const SizedBox(height: 14),
                        if (d['enabled'] != true)
                          const Text('D-коины отключены.')
                        else if (d['open'] != true)
                          Text(
                            'Обмен откроется ${d['opens']}. Пока копите монеты.',
                          )
                        else ...[
                          TextField(
                            controller: amount,
                            enabled: !busy,
                            keyboardType: const TextInputType.numberWithOptions(
                              decimal: true,
                            ),
                            decoration: InputDecoration(
                              labelText: 'Сколько D',
                              hintText: text(d['free_text']),
                              suffixIcon: TextButton(
                                onPressed: busy
                                    ? null
                                    : () => amount.text = text(d['free_text']),
                                child: const Text('Все'),
                              ),
                            ),
                          ),
                          const SizedBox(height: 12),
                          BusyButton(
                            'Обменять',
                            busy: busy,
                            onPressed: exchange,
                          ),
                          const SizedBox(height: 8),
                          Text(
                            'От \$1 по текущей цене, раз в сутки, комиссия ${d['fee_pct']}%.',
                          ),
                        ],
                      ],
                    ),
                  ),
                  const Heading('Прошлые дни'),
                  for (final x in data!['days'] as List)
                    Surface(
                      child: Column(
                        children: [
                          InfoRow(text(x['day']), '${x['change']}%'),
                          InfoRow('Макс.', price(x['high'])),
                          InfoRow('Мин.', price(x['low'])),
                        ],
                      ),
                    ),
                  const Heading('История D-коинов'),
                  if ((data!['history'] as List).isEmpty)
                    const Surface(
                      child: Text(
                        'Сделайте покупку — первые D-коины появятся здесь.',
                      ),
                    ),
                  for (final h in data!['history'] as List)
                    Surface(
                      child: Column(
                        children: [
                          InfoRow(
                            text(h['reason']),
                            '${h['plus'] == true ? '+' : ''}${h['amount']} D',
                          ),
                          Text(
                            '${h['created_at']}${h['wait'] == true ? ' · ждёт выполнения заказа' : ''}',
                            style: const TextStyle(fontSize: 12),
                          ),
                        ],
                      ),
                    ),
                  const Heading('Топ держателей'),
                  for (final t in data!['top'] as List)
                    Surface(
                      child: InfoRow(text(t['login']), '${t['coins']} D'),
                    ),
                ],
              ),
            ),
    );
  }
}

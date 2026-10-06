import 'dart:async';
import 'package:decimal/decimal.dart';
import 'package:flutter/material.dart';
import '../core/api.dart';
import '../core/checkout.dart';
import '../widgets/ui.dart';

class SteamScreen extends StatefulWidget {
  final DonatixApi api;
  const SteamScreen({super.key, required this.api});
  @override
  State<SteamScreen> createState() => _SteamScreenState();
}

class _SteamScreenState extends State<SteamScreen> {
  final login = TextEditingController(), amount = TextEditingController();
  final form = GlobalKey<FormState>();
  String currency = 'RUB';
  bool busy = false, showRates = false;
  @override
  void dispose() {
    login.dispose();
    amount.dispose();
    super.dispose();
  }

  Future<void> buy() async {
    if (!(form.currentState?.validate() ?? false)) return;
    setState(() => busy = true);
    try {
      await checkout(context, widget.api, {
        'product_id': 'steam-topup',
        'quantity': 1,
        'fields': {
          'steam_login': login.text.trim(),
          'amount': amount.text.trim().replaceAll(',', '.'),
          'currency': currency,
        },
      }, 'Пополнить Steam');
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Пополнить Steam')),
    body: AsyncPage(
      load: () => widget.api.get('/api/v1/products/steam-topup'),
      builder: (context, d) {
        final p = d['product'] as Map;
        final rates = p['rates'] as Map;
        if (!rates.containsKey(currency)) {
          currency = rates.keys.first.toString();
        }
        final rate = Decimal.tryParse(text(rates[currency])) ?? Decimal.one;
        final entered =
            Decimal.tryParse(amount.text.replaceAll(',', '.')) ?? Decimal.zero;
        final usd = rate == Decimal.zero
            ? Decimal.zero
            : (entered / rate).toDecimal(scaleOnInfinitePrecision: 12);
        final unit = Decimal.parse(text(p['price_usd']));
        final total =
            (usd * unit * Decimal.fromInt(10000)).ceil() /
            Decimal.fromInt(10000);
        return Form(
          key: form,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Heading('Пополнить Steam'),
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        const Icon(
                          Icons.account_balance_wallet_outlined,
                          color: accent,
                        ),
                        const SizedBox(width: 12),
                        const Expanded(
                          child: Text(
                            'Данные пополнения',
                            style: TextStyle(fontWeight: FontWeight.w700),
                          ),
                        ),
                        TextButton(
                          onPressed: () =>
                              setState(() => showRates = !showRates),
                          child: const Text('Курсы'),
                        ),
                      ],
                    ),
                    if (showRates)
                      for (final e in rates.entries)
                        InfoRow('1 USD', '${e.value} ${e.key}'),
                    const SizedBox(height: 16),
                    TextFormField(
                      controller: login,
                      enabled: !busy,
                      autocorrect: false,
                      decoration: const InputDecoration(
                        labelText: 'Логин Steam',
                      ),
                      validator: (v) =>
                          RegExp(
                            r'^[A-Za-z0-9_.\-]{2,64}$',
                          ).hasMatch((v ?? '').trim())
                          ? null
                          : 'Введите логин Steam',
                    ),
                    const Padding(
                      padding: EdgeInsets.symmetric(vertical: 8),
                      child: Text(
                        'Введите логин аккаунта Steam (не никнейм) для пополнения.',
                        style: TextStyle(fontSize: 12),
                      ),
                    ),
                    TextFormField(
                      controller: amount,
                      enabled: !busy,
                      keyboardType: const TextInputType.numberWithOptions(
                        decimal: true,
                      ),
                      decoration: const InputDecoration(labelText: 'Сумма'),
                      onChanged: (_) => setState(() {}),
                      validator: (v) {
                        final n = Decimal.tryParse(
                          (v ?? '').replaceAll(',', '.'),
                        );
                        if (n == null || n <= Decimal.zero) {
                          return 'Введите положительную сумму';
                        }
                        if ((n * Decimal.fromInt(100)).isInteger == false) {
                          return 'Не больше двух знаков после точки';
                        }
                        final lo =
                                Decimal.parse(text(p['min_usd'] ?? '0.5')) *
                                rate,
                            hi =
                                Decimal.parse(text(p['max_usd'] ?? '1000')) *
                                rate;
                        return n < lo || n > hi
                            ? 'Допустимо от $lo до $hi $currency'
                            : null;
                      },
                    ),
                    const SizedBox(height: 14),
                    Wrap(
                      spacing: 8,
                      children: rates.keys
                          .map(
                            (c) => ChoiceChip(
                              label: Text('$c'),
                              selected: currency == c,
                              onSelected: busy
                                  ? null
                                  : (_) => setState(() => currency = '$c'),
                            ),
                          )
                          .toList(),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      'Минимум: ${Decimal.parse(text(p['min_usd'] ?? '0.5')) * rate} $currency • Максимум: ${Decimal.parse(text(p['max_usd'] ?? '1000')) * rate} $currency',
                      style: const TextStyle(fontSize: 12),
                    ),
                  ],
                ),
              ),
              Surface(
                child: Column(
                  children: [
                    const Heading('Сводка'),
                    InfoRow('Скидка', '${p['discount_percent']}%'),
                    InfoRow('Получит на аккаунт', '${amount.text} $currency'),
                    InfoRow('Получит в USD', '\$${usd.toStringAsFixed(2)}'),
                    InfoRow(
                      'К оплате',
                      widget.api.displayPrice(
                        total
                            .toDecimal(scaleOnInfinitePrecision: 4)
                            .toStringAsFixed(4),
                      ),
                    ),
                    const SizedBox(height: 12),
                    BusyButton('Продолжить', busy: busy, onPressed: buy),
                    const SizedBox(height: 12),
                    const Text(
                      'Логин проверяется до оплаты. Если пополнение не пройдёт, деньги вернутся на баланс.',
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

class SteamGiftScreen extends StatefulWidget {
  final DonatixApi api;
  const SteamGiftScreen({super.key, required this.api});
  @override
  State<SteamGiftScreen> createState() => _SteamGiftScreenState();
}

class _SteamGiftScreenState extends State<SteamGiftScreen> {
  final search = TextEditingController(),
      regionSearch = TextEditingController(),
      invite = TextEditingController();
  Timer? timer;
  String query = '', regionQuery = '';
  Map? game, edition, region;
  List offers = [];
  bool busy = false, loading = false;
  Object? error;
  int selection = 0;
  @override
  void dispose() {
    timer?.cancel();
    search.dispose();
    regionSearch.dispose();
    invite.dispose();
    super.dispose();
  }

  Future<void> select(Map g) async {
    final revision = ++selection;
    setState(() {
      game = g;
      edition = region = null;
      offers = [];
      loading = true;
      error = null;
    });
    try {
      final d = await widget.api.get('/api/v1/steam-gifts/games/${g['appid']}');
      if (mounted && revision == selection) {
        setState(() => offers = d['offers'] as List);
      }
    } catch (e) {
      if (mounted && revision == selection) setState(() => error = e);
    } finally {
      if (mounted && revision == selection) setState(() => loading = false);
    }
  }

  Future<void> buy() async {
    if (game == null || edition == null || region == null) return;
    setState(() => busy = true);
    try {
      await checkout(
        context,
        widget.api,
        {
          'product_id': 'steam-gift',
          'quantity': 1,
          'fields': {
            'app_id': text(game!['appid']),
            'sub_id': text(edition!['sub_id']),
            'region': text(region!['region']),
            'invite_url': invite.text.trim(),
          },
        },
        '${game!['name']} · ${edition!['name']} · ${region!['region']}',
      );
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Steam Гифты (игры)')),
    body: ListView(
      padding: const EdgeInsets.all(18),
      children: [
        const Heading('Steam Гифты (игры)'),
        TextField(
          controller: search,
          decoration: const InputDecoration(
            labelText: 'Поиск игры по названию или ID',
            prefixIcon: Icon(Icons.search),
          ),
          onChanged: (v) {
            timer?.cancel();
            timer = Timer(const Duration(milliseconds: 350), () {
              if (mounted) setState(() => query = v.trim());
            });
          },
        ),
        const SizedBox(height: 16),
        const Heading('1 · Игры'),
        SizedBox(
          height: 310,
          child: AsyncPage(
            key: ValueKey(query),
            load: () =>
                widget.api.get('/api/v1/steam-gifts/games', {'q': query}),
            builder: (c, d) => Column(
              children: [
                if ((d['items'] as List).isEmpty)
                  const Text('Ничего не найдено'),
                for (final g in d['items'] as List)
                  Surface(
                    padding: EdgeInsets.zero,
                    child: ListTile(
                      leading: ProductImage(g['cover'], size: 48),
                      title: Text(text(g['name'])),
                      subtitle: Text('AppID · ${g['appid']}'),
                      selected: game?['appid'] == g['appid'],
                      onTap: busy ? null : () => select(g as Map),
                    ),
                  ),
              ],
            ),
          ),
        ),
        const Heading('2 · Издание'),
        if (loading) const LinearProgressIndicator(),
        if (error != null)
          Surface(
            child: Column(
              children: [
                Text('$error'),
                TextButton(
                  onPressed: () => select(game!),
                  child: const Text('Повторить'),
                ),
              ],
            ),
          ),
        if (game == null)
          const Surface(child: Text('Выберите игру, чтобы увидеть издания')),
        if (!loading && game != null && offers.isEmpty && error == null)
          const Surface(child: Text('Нет доступных изданий')),
        for (final o in offers)
          Surface(
            padding: EdgeInsets.zero,
            child: ListTile(
              title: Text(text(o['name'])),
              subtitle: Text(
                'ID пакета · ${o['sub_id']} · регионов: ${(o['regions'] as List).length}',
              ),
              selected: edition?['sub_id'] == o['sub_id'],
              onTap: busy
                  ? null
                  : () => setState(() {
                      edition = o as Map;
                      region = null;
                    }),
            ),
          ),
        const Heading('3 · Регион и цена'),
        TextField(
          controller: regionSearch,
          decoration: const InputDecoration(
            labelText: 'Поиск региона',
            hintText: 'Страна или код ISO (KZ, UZ)…',
          ),
          onChanged: (v) =>
              setState(() => regionQuery = v.toLowerCase().trim()),
        ),
        const SizedBox(height: 12),
        if (edition == null)
          const Surface(child: Text('Выберите издание, чтобы увидеть регионы')),
        if (edition != null)
          for (final r in (edition!['regions'] as List).where(
            (r) => text(r['region']).toLowerCase().contains(regionQuery),
          ))
            Surface(
              padding: EdgeInsets.zero,
              child: ListTile(
                title: Text(text(r['region'])),
                trailing: Text(widget.api.displayPrice(r['price_usd'])),
                selected: region?['region'] == r['region'],
                onTap: busy ? null : () => setState(() => region = r as Map),
              ),
            ),
        Surface(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Heading('Сводка'),
              TextField(
                controller: invite,
                enabled: !busy,
                keyboardType: TextInputType.url,
                autocorrect: false,
                decoration: const InputDecoration(
                  labelText: 'Steam Invite ссылка',
                  hintText: 'https://s.team/p/…/…',
                ),
                onChanged: (_) => setState(() {}),
              ),
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 10),
                child: Text(
                  'Ссылка-приглашение в друзья из Steam: Друзья → Добавить друга → «Ссылка для приглашения».',
                  style: TextStyle(fontSize: 12),
                ),
              ),
              InfoRow('App ID', text(game?['appid'])),
              InfoRow('ID пакета', text(edition?['sub_id'])),
              InfoRow('Издание', text(edition?['name'])),
              InfoRow('Регион', text(region?['region'])),
              InfoRow(
                'Цена',
                widget.api.displayPrice(region?['price_usd'] ?? '0.0000'),
              ),
              const SizedBox(height: 12),
              BusyButton(
                'Продолжить',
                busy: busy,
                onPressed:
                    region != null &&
                        RegExp(
                          r'^https://s\.team/p/',
                        ).hasMatch(invite.text.trim())
                    ? buy
                    : null,
              ),
              const SizedBox(height: 12),
              const Text(
                'Если гифт не отправится, деньги вернутся на баланс.',
                style: TextStyle(fontSize: 12),
              ),
            ],
          ),
        ),
      ],
    ),
  );
}

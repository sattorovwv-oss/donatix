import 'dart:async';
import 'package:flutter/material.dart';
import '../core/api.dart';
import '../widgets/ui.dart';
import '../core/navigation.dart';
import 'cart.dart';
import 'telegram.dart';
import 'steam.dart';

class CatalogScreen extends StatefulWidget {
  final DonatixApi api;
  final String initialKind;
  const CatalogScreen({super.key, required this.api, this.initialKind = ''});
  @override
  State<CatalogScreen> createState() => _CatalogScreenState();
}

class _CatalogScreenState extends State<CatalogScreen> {
  final search = TextEditingController();
  Timer? debounce;
  String kind = '', query = '';
  @override
  void initState() {
    super.initState();
    kind = widget.initialKind;
  }

  @override
  void dispose() {
    debounce?.cancel();
    search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Column(
    children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(18, 12, 18, 8),
        child: TextField(
          controller: search,
          decoration: const InputDecoration(
            prefixIcon: Icon(Icons.search),
            hintText: 'Найти игру или сервис',
          ),
          onChanged: (s) {
            debounce?.cancel();
            debounce = Timer(const Duration(milliseconds: 450), () {
              if (mounted) setState(() => query = s.trim());
            });
          },
        ),
      ),
      SizedBox(
        height: 48,
        child: ListView(
          padding: const EdgeInsets.symmetric(horizontal: 18),
          scrollDirection: Axis.horizontal,
          children: kinds.entries
              .map(
                (e) => Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    label: Text(e.value),
                    selected: kind == e.key,
                    onSelected: (_) => setState(() => kind = e.key),
                  ),
                ),
              )
              .toList(),
        ),
      ),
      if ([
        'telegram_stars',
        'telegram_premium',
        'steam_topup',
        'steam_gift',
      ].contains(kind))
        Padding(
          padding: const EdgeInsets.all(12),
          child: BusyButton(
            'Открыть ${kinds[kind]}',
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute<void>(
                builder: (_) => kind == 'steam_topup'
                    ? SteamScreen(api: widget.api)
                    : kind == 'steam_gift'
                    ? SteamGiftScreen(api: widget.api)
                    : TelegramScreen(
                        api: widget.api,
                        premium: kind == 'telegram_premium',
                      ),
              ),
            ),
          ),
        ),
      Expanded(
        child: AsyncPage(
          key: ValueKey('$kind|$query'),
          load: () => widget.api.get('/api/v1/mobile/categories', {
            'kind': kind,
            'q': query,
          }),
          builder: (context, d) => Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Heading(
                kinds[kind] ?? 'Каталог',
                subtitle: 'Выберите игру и пакет пополнения',
              ),
              if ((d['items'] as List).isEmpty)
                const Surface(child: Text('Ничего не найдено.')),
              LayoutBuilder(
                builder: (context, box) {
                  final columns = box.maxWidth > 650 ? 3 : 2;
                  return Wrap(
                    spacing: 12,
                    runSpacing: 12,
                    children: (d['items'] as List)
                        .map(
                          (p) => SizedBox(
                            width:
                                (box.maxWidth - 12 * (columns - 1)) / columns,
                            child: Surface(
                              padding: EdgeInsets.zero,
                              child: InkWell(
                                borderRadius: BorderRadius.circular(16),
                                onTap: () => Navigator.push(
                                  context,
                                  MaterialPageRoute<void>(
                                    builder: (_) => PacksScreen(
                                      api: widget.api,
                                      category: text(p['category_id']),
                                      kind: text(p['kind']),
                                      title: text(p['category_name']),
                                    ),
                                  ),
                                ),
                                child: Padding(
                                  padding: const EdgeInsets.all(12),
                                  child: Column(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.start,
                                    children: [
                                      ProductImage(p['image_url'], size: 76),
                                      const SizedBox(height: 12),
                                      Text(
                                        text(p['category_name']),
                                        maxLines: 2,
                                        overflow: TextOverflow.ellipsis,
                                        style: const TextStyle(
                                          fontWeight: FontWeight.w700,
                                        ),
                                      ),
                                      const SizedBox(height: 6),
                                      Text(
                                        'от ${widget.api.displayPrice(p['from_price'])}',
                                        style: Theme.of(
                                          context,
                                        ).textTheme.bodySmall,
                                      ),
                                    ],
                                  ),
                                ),
                              ),
                            ),
                          ),
                        )
                        .toList(),
                  );
                },
              ),
            ],
          ),
        ),
      ),
    ],
  );
}

class PacksScreen extends StatefulWidget {
  final DonatixApi api;
  final String category, kind, title;
  final String? region;
  const PacksScreen({
    super.key,
    required this.api,
    required this.category,
    required this.kind,
    required this.title,
    this.region,
  });
  @override
  State<PacksScreen> createState() => _PacksScreenState();
}

class _PacksScreenState extends State<PacksScreen> {
  String? region;
  final cart = <String, int>{};
  @override
  void initState() {
    super.initState();
    region = widget.region;
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: Text(widget.title)),
    body: AsyncPage(
      load: () async {
        final all = <dynamic>[];
        for (var offset = 0; ; offset += 500) {
          final d = await widget.api.get('/api/v1/products', {
            'kind': widget.kind,
            'category_id': widget.category,
            'limit': 500,
            'offset': offset,
          });
          final items = d['items'] as List;
          all.addAll(items);
          if (items.length < 500) break;
        }
        return {'items': all};
      },
      builder: (context, d) {
        final products = d['items'] as List;
        final regions = products
            .map((p) => text(p['region']))
            .where((s) => s.isNotEmpty)
            .toSet();
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (cart.isNotEmpty)
              Surface(
                child: BusyButton(
                  'Корзина · ${cart.values.fold<int>(0, (a, b) => a + b)} пакетов',
                  onPressed: () => Navigator.push(
                    context,
                    MaterialPageRoute<void>(
                      builder: (_) => CartScreen(
                        api: widget.api,
                        products: products
                            .map((p) => Map<String, dynamic>.from(p as Map))
                            .toList(),
                        counts: cart,
                      ),
                    ),
                  ),
                ),
              ),
            Heading(widget.title, subtitle: '${products.length} пакетов'),
            if (regions.length > 1)
              Wrap(
                spacing: 8,
                children: [
                  for (final r in ['', ...regions])
                    ChoiceChip(
                      label: Text(r.isEmpty ? 'Все регионы' : r),
                      selected: (region ?? '') == r,
                      onSelected: (_) => setState(() {
                        region = r;
                        cart.clear();
                      }),
                    ),
                ],
              ),
            const SizedBox(height: 12),
            if (products.isEmpty)
              const Surface(child: Text('Пакеты недоступны.')),
            ...products
                .where(
                  (p) => (region ?? '').isEmpty || text(p['region']) == region,
                )
                .map(
                  (p) => Surface(
                    padding: EdgeInsets.zero,
                    child: ListTile(
                      title: Text(text(p['title'] ?? p['name'])),
                      subtitle: Text(text(p['region_title'])),
                      leading: p['kind'] == 'topup' && p['max_quantity'] == 1
                          ? IconButton(
                              tooltip: 'Добавить в корзину',
                              icon: const Icon(Icons.add_shopping_cart),
                              onPressed:
                                  cart.values.fold<int>(0, (a, b) => a + b) >=
                                      20
                                  ? null
                                  : () {
                                      final picked = products.where(
                                        (r) =>
                                            cart.containsKey(r['product_id']),
                                      );
                                      if (picked.isNotEmpty &&
                                          text(picked.first['region']) !=
                                              text(p['region'])) {
                                        message(
                                          context,
                                          'В одной корзине — пакеты одного региона.',
                                        );
                                        return;
                                      }
                                      setState(
                                        () => cart[text(p['product_id'])] =
                                            (cart[text(p['product_id'])] ?? 0) +
                                            1,
                                      );
                                    },
                            )
                          : null,
                      trailing: Text(
                        widget.api.displayPrice(p['price_usd']),
                        style: const TextStyle(fontWeight: FontWeight.w700),
                      ),
                      onTap: () => Navigator.push(
                        context,
                        MaterialPageRoute<void>(
                          builder: (_) =>
                              purchasePage(widget.api, text(p['product_id'])),
                        ),
                      ),
                    ),
                  ),
                ),
          ],
        );
      },
    ),
  );
}

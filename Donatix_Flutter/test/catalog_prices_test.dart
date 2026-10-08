import 'dart:io';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:html/parser.dart' as html;
import 'package:donatix/core/api.dart';
import 'package:donatix/core/catalog_names.dart';
import 'package:donatix/screens/catalog.dart';
import 'package:donatix/screens/document.dart';
import 'package:donatix/screens/home.dart';
import 'package:donatix/screens/telegram.dart';
import 'package:donatix/widgets/ui.dart' show ProductImage;
import 'existing_api_test.dart' show SiteAdapter, htmlResponse, jsonResponse;

const telegramPage = '''
<div class="tab-pane" data-pane="stars"><input name="product_id" value="stars">
<input name="quantity" min="50" max="10000"></div>
<label class="tg-plan"><input name="product_id" value="premium" data-price="12.5">
<span class="tp-name">3 месяца</span></label>
<script>const starPrice = "0.0150000";</script>''';

String gameCard(String kind, String id, String name) =>
    '''
<a class="game-card" href="/panel/catalog?kind=$kind&amp;category=$id">
<div class="game-name">$name</div><div class="game-meta"><span>от \$1.2500</span>
<span>СНГ</span></div></a>''';

ResponseBody catalogReply(RequestOptions r) {
  switch (r.uri.queryParameters['kind']) {
    case 'topup':
      return htmlResponse(
        gameCard('topup', 'ff-cis', 'Free Fire СНГ') +
            gameCard('topup', 'ff-id', 'Free Fire Индонезия'),
      );
    case 'game_key':
      return htmlResponse(
        List.generate(
          601,
          (i) => gameCard('game_key', 'key-$i', 'Игра $i'),
        ).join(),
      );
    case 'gift_card':
      return htmlResponse(gameCard('gift_card', 'card', 'Подарочная карта'));
    case 'telegram':
    case 'telegram_premium':
      return htmlResponse(telegramPage);
    case 'steam_topup':
    case 'steam_gift':
      final gift = r.uri.queryParameters['kind'] == 'steam_gift';
      return htmlResponse(
        '<a class="pack-card" href="/panel/buy/${gift ? 'steam-gift' : 'steam-topup'}">'
        '<span class="pack-name">${gift ? 'Steam Гифты' : 'Пополнение Steam'}</span></a>',
      );
    default:
      fail('The capped unfiltered package page must not be used: ${r.uri}');
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'Known regional suffixes are readable without changing other qualifiers',
    () {
      expect(catalogDisplayName('Free Fire (ID)'), 'Free Fire (Индонезия)');
      expect(catalogDisplayName('Free Fire (CIS)'), 'Free Fire (СНГ)');
      expect(
        catalogDisplayName('Free Fire (MY/SG)'),
        'Free Fire (Малайзия / Сингапур)',
      );
      expect(catalogDisplayName('PUBG Mobile (Fast)'), 'PUBG Mobile (Fast)');
      expect(catalogDisplayName('Игра (iOS)'), 'Игра (iOS)');
    },
  );

  test(
    'Popular titles read the direct title span and preserve exact regional links',
    () async {
      final api = DonatixApi();
      api.dio.httpClientAdapter = SiteAdapter(
        (r) => htmlResponse('''
      <section class="pop-quick">
      <a class="pop-tile" href="/panel/catalog?kind=topup&amp;category=ff&amp;region=CIS">
      <span class="media"><span>FF</span></span><span>Free Fire СНГ</span></a>
      <a class="pop-tile" href="/panel/catalog?kind=topup&amp;category=ff&amp;region=ID">
      <span class="media"><img alt="Free Fire"><span>FF</span></span><span>Free Fire Индонезия</span></a>
      <a class="pop-tile" href="/panel/catalog?kind=topup&amp;category=pubg">
      <span class="media"><span>PM</span></span><span>PUBG Mobile</span></a>
      </section>'''),
      );
      final items = (await api.existingSite.home())['popular'] as List;
      expect(items.map((p) => p['title']), [
        'Free Fire СНГ',
        'Free Fire Индонезия',
        'PUBG Mobile',
      ]);
      expect(Uri.parse(items[0]['href']).queryParameters['region'], 'CIS');
      expect(Uri.parse(items[1]['href']).queryParameters['region'], 'ID');
    },
  );

  test(
    'All reads every section including categories beyond the 500-package cap',
    () async {
      final api = DonatixApi();
      final requests = <String?>[];
      api.dio.httpClientAdapter = SiteAdapter((r) {
        expect(r.method, 'GET');
        requests.add(r.uri.queryParameters['kind']);
        return catalogReply(r);
      });
      final items = (await api.existingSite.categories({}))['items'] as List;
      expect(items.length, 608);
      expect(items.map((p) => p['kind']).toSet(), {
        'topup',
        'telegram_stars',
        'telegram_premium',
        'steam_topup',
        'steam_gift',
        'gift_card',
        'game_key',
      });
      expect(items.any((p) => p['category_id'] == 'key-600'), isTrue);
      expect(
        items.where((p) => p['kind'] == 'topup').map((p) => p['category_name']),
        ['Free Fire СНГ', 'Free Fire Индонезия'],
      );
      expect(requests.toSet().length, 6);
    },
  );

  test(
    'All search and unavailable services use actual catalogue data',
    () async {
      final api = DonatixApi();
      api.dio.httpClientAdapter = SiteAdapter(catalogReply);
      final search =
          (await api.existingSite.categories({'q': 'Индонезия'}))['items']
              as List;
      expect(search.map((p) => p['category_id']), ['ff-id']);
      api.dio.httpClientAdapter = SiteAdapter(
        (r) => htmlResponse('<p>Недоступно</p>'),
      );
      expect(
        (await api.existingSite.categories({
          'kind': 'telegram_premium',
        }))['items'],
        isEmpty,
      );
    },
  );

  test(
    'A failed section raises an error rather than returning an empty All list',
    () async {
      final api = DonatixApi()..usesExistingApi = true;
      api.dio.httpClientAdapter = SiteAdapter(
        (r) => r.uri.queryParameters['kind'] == 'topup'
            ? ResponseBody.fromString('Unavailable', 503)
            : catalogReply(r),
      );
      await expectLater(
        api.get('/api/v1/mobile/categories'),
        throwsA(isA<ApiFailure>().having((e) => e.status, 'status', 503)),
      );
    },
  );

  testWidgets('Service category opens its real Premium screen', (tester) async {
    final api = DonatixApi()..usesExistingApi = true;
    api.dio.httpClientAdapter = SiteAdapter(catalogReply);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: CatalogScreen(api: api, initialKind: 'telegram_premium'),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('Telegram Premium'));
    await tester.pumpAndSettle();
    expect(
      tester.widget<TelegramScreen>(find.byType(TelegramScreen)).premium,
      isTrue,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'All builds visible rows instead of hundreds of off-screen cards',
    (tester) async {
      tester.view.physicalSize = const Size(360, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final api = DonatixApi()..usesExistingApi = true;
      api.dio.httpClientAdapter = SiteAdapter(catalogReply);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: CatalogScreen(api: api)),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Free Fire СНГ'), findsOneWidget);
      expect(find.text('Игра 600'), findsNothing);
      expect(find.byType(ProductImage).evaluate().length, lessThan(30));
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('Full popular names fit a narrow phone with enlarged text', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    const name = 'Free Fire Индонезия — полное название игры и региона';
    final api = DonatixApi();
    api.dio.httpClientAdapter = SiteAdapter((r) {
      if (r.uri.path == '/api/v1/mobile/timezone') {
        return jsonResponse({
          'ok': true,
          'choice': 'auto',
          'zones': [
            ['Asia/Dushanbe', 'Душанбе'],
          ],
        });
      }
      if (r.uri.path == '/api/v1/mobile/keys') {
        return jsonResponse({'ok': true, 'active': true, 'items': []});
      }
      expect(r.uri.path, anyOf('/api/v1/me', '/api/v1/mobile/home'));
      return jsonResponse({
        'ok': true,
        'login': 'client',
        'status': 'active',
        'tier': 'bronze',
        'balance': '100',
        'markup': '5',
        'orders_all': 0,
        'summary': {'totalSpent': '0', 'totalOrders': 0},
        'popular': [
          {
            'title': name,
            'href': '/panel/catalog?kind=topup&category=ff&region=ID',
          },
        ],
        'dcoin_enabled': false,
        'referral_enabled': false,
      });
    });
    await tester.pumpWidget(
      MaterialApp(
        home: MediaQuery(
          data: const MediaQueryData(
            size: Size(360, 800),
            textScaler: TextScaler.linear(1.6),
          ),
          child: Scaffold(
            body: HomeScreen(api: api, navigate: (_) {}),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    final label = tester.widget<Text>(find.text(name));
    expect(label.maxLines, isNull);
    expect(label.overflow, isNull);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'Reloaded pricing form posts decimals and preserves every other setting',
    (tester) async {
      final api = DonatixApi()..csrf = 'fresh-csrf';
      final page = File(
        'test/fixtures/admin/admin_settings.html',
      ).readAsStringSync();
      final original = html
          .parse(page)
          .querySelector('form[action="/admin/settings"]')!;
      Map<String, String>? posted;
      api.dio.httpClientAdapter = SiteAdapter((r) {
        expectSync(r.method, 'POST');
        expectSync(r.uri.path, '/admin/settings');
        posted = Map.fromEntries((r.data as FormData).fields);
        return htmlResponse('<main>Сохранено</main>');
      });
      Widget screen() => MaterialApp(
        home: Scaffold(
          body: SingleChildScrollView(
            child: NativeForm(
              key: const ValueKey('settings'),
              api: api,
              form: html.parse(original.outerHtml).querySelector('form')!,
              sourcePath: '/admin/settings',
              render: (n) => Text(n.text ?? ''),
              completed: ([r]) async {},
            ),
          ),
        ),
      );
      await tester.pumpWidget(screen());
      tester
              .widget<TextFormField>(
                find.byKey(const ValueKey('markup_bronze')),
              )
              .controller!
              .text =
          '12';
      // Same HTML, new DOM nodes: controllers and checked states must be rebuilt.
      await tester.pumpWidget(screen());
      expect(
        tester
            .widget<TextFormField>(find.byKey(const ValueKey('markup_bronze')))
            .controller!
            .text,
        '8',
      );
      tester
              .widget<TextFormField>(
                find.byKey(const ValueKey('markup_bronze')),
              )
              .controller!
              .text =
          '7,5%';
      tester
              .widget<TextFormField>(
                find.byKey(const ValueKey('markup_steam_topup')),
              )
              .controller!
              .text =
          '';
      final action = tester
          .widget<FilledButton>(find.byType(FilledButton))
          .onPressed!;
      action();
      // The form remains busy while confirmation is open. Waiting for every
      // animation to stop here would wait forever on its progress indicator.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.byType(AlertDialog), findsOneWidget);
      expect(posted, isNull);
      await tester.tap(find.text('Подтвердить'));
      await tester.pumpAndSettle();
      expect(posted, isNotNull);
      expect(posted!['csrf'], 'fresh-csrf');
      expect(posted!['markup_bronze'], '7.5');
      expect(posted!['markup_steam_topup'], '');
      for (final e in original.querySelectorAll('input,textarea,select')) {
        final name = e.attributes['name']!;
        if (['csrf', 'markup_bronze', 'markup_steam_topup'].contains(name)) {
          continue;
        }
        if (e.attributes['type'] == 'checkbox') {
          if (e.attributes.containsKey('checked')) {
            expect(posted![name], e.attributes['value'] ?? 'on', reason: name);
          } else {
            expect(posted!.containsKey(name), isFalse, reason: name);
          }
        } else {
          expect(posted![name], e.attributes['value'] ?? '', reason: name);
        }
      }
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'Invalid percentages are blocked; optional kind markup can stay empty',
    (tester) async {
      final api = DonatixApi();
      var posts = 0;
      api.dio.httpClientAdapter = SiteAdapter((r) {
        posts++;
        return htmlResponse('');
      });
      final form = html
          .parse('''<form method="post" action="/admin/settings">
      <input name="markup_bronze" required value="101"><input name="markup_topup" value="">
      <button class="primary">Сохранить</button></form>''')
          .querySelector('form')!;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: NativeForm(
              api: api,
              form: form,
              sourcePath: '/admin/settings',
              render: (n) => Text(n.text ?? ''),
              completed: ([r]) async {},
            ),
          ),
        ),
      );
      await tester.tap(find.text('Сохранить'));
      await tester.pumpAndSettle();
      expect(find.text('Допустимо от 0 до 100 %'), findsOneWidget);
      expect(find.byType(AlertDialog), findsNothing);
      expect(posts, 0);
    },
  );
}

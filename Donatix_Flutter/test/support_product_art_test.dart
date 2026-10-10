import 'dart:io';
import 'dart:ui' as ui;
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:donatix/core/api.dart';
import 'package:donatix/screens/catalog.dart';
import 'package:donatix/screens/management.dart';
import 'package:donatix/widgets/product_art.dart';
import 'existing_api_test.dart'
    show SiteAdapter, cookie, htmlResponse, jsonResponse;

String supportPage(String code) =>
    '''
<form action="/logout"><input name="csrf" value="fixture-csrf"></form>
<a href="https://t.me/unrelated_channel">Наш канал</a>
<div class="card support-card">
<div class="sc-step"><a class="sc-open" href="https://t.me/donatix_help_bot?start=$code">Открыть бота</a></div>
<div class="sc-step"><code id="sc-code">DX-$code</code>
<div class="muted small">Код одноразовый, действует 10 минут.</div></div></div>''';

DonatixApi signedApi() => DonatixApi()
  ..userId = 2
  ..session = cookie(2);

Future<void> savePreview(WidgetTester tester, String name) async {
  if (Platform.environment['DONATIX_PREVIEW_DIR'] == null) return;
  await tester.runAsync(() async {
    for (final element in find.byType(Image).evaluate()) {
      await precacheImage((element.widget as Image).image, element);
    }
  });
  await tester.pumpAndSettle();
  await tester.runAsync(() async {
    final boundary = tester.renderObject<RenderRepaintBoundary>(
      find.byKey(const ValueKey('preview')),
    );
    final image = await boundary.toImage(pixelRatio: 2);
    final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
    final file = File(
      '${Platform.environment['DONATIX_PREVIEW_DIR']}/$name.png',
    );
    await file.parent.create(recursive: true);
    await file.writeAsBytes(bytes!.buffer.asUint8List());
    image.dispose();
  });
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    if (Platform.environment['DONATIX_PREVIEW_DIR'] == null) return;
    final font = FontLoader('Onest')
      ..addFont(rootBundle.load('assets/Onest.ttf'));
    await font.load();
    final icons = FontLoader('MaterialIcons')
      ..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'));
    await icons.load();
  });

  test(
    'Support uses the authenticated website without the mobile extension',
    () async {
      final api = signedApi();
      api.dio.httpClientAdapter = SiteAdapter((r) {
        expect(r.method, 'GET');
        expect(r.uri.path, '/panel/support');
        expect(r.headers['Cookie'], contains('dx_session='));
        return htmlResponse(supportPage('A2B3C4D5'));
      });
      final result = await api.existingSite.support();
      expect(result['code'], 'DX-A2B3C4D5');
      expect(result['url'], 'https://t.me/donatix_help_bot?start=A2B3C4D5');
      expect(result['bot'], 'donatix_help_bot');
      expect(result['minutes'], 10);
      expect(api.usesExistingApi, isFalse);
    },
  );

  test(
    'A plain bot link uses the displayed code and ignores the site channel',
    () async {
      final api = signedApi();
      api.dio.httpClientAdapter = SiteAdapter(
        (r) => htmlResponse(
          supportPage(
            'A2B3C4D5',
          ).replaceAll('?start=A2B3C4D5', '').replaceAll('class="sc-open"', ''),
        ),
      );
      final result = await api.existingSite.support();
      expect(result['url'], 'https://t.me/donatix_help_bot?start=A2B3C4D5');
    },
  );

  test(
    'Unexpected support markup fails instead of reporting a disconnected bot',
    () async {
      final api = signedApi();
      api.dio.httpClientAdapter = SiteAdapter(
        (r) => htmlResponse('<div>Unexpected response</div>'),
      );
      await expectLater(api.existingSite.support(), throwsA(isA<ApiFailure>()));
      api.dio.httpClientAdapter = SiteAdapter(
        (r) => htmlResponse('<div class="card empty">Бот не подключён</div>'),
      );
      expect((await api.existingSite.support())['code'], '');
    },
  );

  testWidgets(
    'Support opens and refreshes the revoked link after notification-code delivery',
    (tester) async {
      var issued = 0;
      String current() => 'A2B3C4D$issued';
      final api = signedApi();
      api.dio.httpClientAdapter = SiteAdapter((r) {
        switch (r.uri.path) {
          case '/panel/support':
            issued++;
            return htmlResponse(supportPage(current()));
          case '/panel/support/code':
            expect(r.method, 'POST');
            expect((r.data as Map)['csrf'], 'fixture-csrf');
            issued++;
            return ResponseBody.fromString(
              '',
              303,
              headers: {
                'location': ['/panel/notifications'],
              },
            );
          case '/panel/notifications':
            return htmlResponse('<p>DX-${current()}</p>');
          case '/api/v1/mobile/notifications':
            return jsonResponse({
              'ok': true,
              'unread': 0,
              'items': <dynamic>[],
            });
          default:
            fail('Unexpected support dependency: ${r.uri.path}');
        }
      });
      await tester.pumpWidget(MaterialApp(home: SupportScreen(api: api)));
      await tester.pumpAndSettle();
      expect(find.text('DX-A2B3C4D1'), findsOneWidget);
      final button = find.text('Получить код в уведомления');
      await tester.ensureVisible(button);
      await tester.tap(button);
      await tester.pumpAndSettle();
      expect(find.text('Уведомления'), findsWidgets);
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.text('DX-A2B3C4D4'), findsOneWidget);
      expect(find.text('DX-A2B3C4D1'), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('Support fits a narrow phone with enlarged text', (tester) async {
    tester.view.physicalSize = const Size(360, 840);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final api = signedApi();
    api.dio.httpClientAdapter = SiteAdapter(
      (r) => htmlResponse(supportPage('A2B3C4D5')),
    );
    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData(fontFamily: 'Onest'),
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(
            context,
          ).copyWith(textScaler: TextScaler.linear(1.4)),
          child: child!,
        ),
        home: RepaintBoundary(
          key: const ValueKey('preview'),
          child: SupportScreen(api: api),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('Открыть Telegram'), findsOneWidget);
    expect(find.text('@donatix_help_bot'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await savePreview(tester, 'support');
  });

  testWidgets(
    'Artwork keeps checkout and cart usable at 360 pixels and 1.4 text scale',
    (tester) async {
      tester.view.physicalSize = const Size(360, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final products = [
        {
          'product_id': 'diamond-100',
          'kind': 'topup',
          'name': '100 Diamonds',
          'title': '100 алмазов',
          'region': 'CIS',
          'region_title': 'СНГ',
          'price_usd': '0.9876',
          'max_quantity': 1,
        },
        {
          'product_id': 'weekly-voucher',
          'kind': 'topup',
          'name': 'Weekly Membership',
          'title': 'Ваучер на неделю (лайт)',
          'region': 'CIS',
          'region_title': 'СНГ',
          'price_usd': '1.2345',
          'max_quantity': 1,
        },
      ];
      final api = DonatixApi()..userId = 2;
      final requests = <String>[];
      api.dio.httpClientAdapter = SiteAdapter((r) {
        requests.add(r.uri.path);
        return jsonResponse({'ok': true, 'items': products});
      });
      await tester.pumpWidget(
        MaterialApp(
          theme: ThemeData(fontFamily: 'Onest'),
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: TextScaler.linear(1.4)),
            child: child!,
          ),
          home: RepaintBoundary(
            key: const ValueKey('preview'),
            child: PacksScreen(
              api: api,
              category: 'ff',
              kind: 'topup',
              title: 'Free Fire СНГ',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(ProductArt), findsNWidgets(2));
      expect(find.text('\$0.9876'), findsOneWidget);
      expect(find.text('\$1.2345'), findsOneWidget);
      await tester.tap(find.byTooltip('Добавить в корзину').first);
      await tester.pumpAndSettle();
      expect(find.text('Корзина · 1 пакетов'), findsOneWidget);
      expect(requests.every((r) => r == '/api/v1/products'), isTrue);
      expect(tester.takeException(), isNull);
      await savePreview(tester, 'packages');
    },
  );

  test('Non-diamond and level-up goods keep their supplier cover', () {
    expect(productArtAsset({'kind': 'topup', 'name': '660 UC'}), isNull);
    expect(
      productArtAsset({
        'kind': 'topup',
        'name': 'Level Up Pass + 500 Diamonds',
      }),
      isNull,
    );
    expect(
      productArtAsset({'kind': 'game_key', 'name': 'Diamond Quest'}),
      isNull,
    );
  });
}

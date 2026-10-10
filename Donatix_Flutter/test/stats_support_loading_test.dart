import 'dart:io';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:donatix/core/api.dart';
import 'package:donatix/screens/management.dart';
import 'package:donatix/screens/stats.dart';
import 'existing_api_test.dart' show SiteAdapter, cookie, htmlResponse;
import 'support_product_art_test.dart' show savePreview;

String currentSupport() {
  // The public HTML captured from donatix.tj on 2026-10-10 has the same
  // knowledge base and topic sheet; signed-in topics link directly to forms.
  return File(
    'test/fixtures/support_current.html',
  ).readAsStringSync().replaceAllMapped(
    RegExp(r'href="/login\?next=([^"]+)"'),
    (match) => 'href="${Uri.decodeComponent(match.group(1)!)}"',
  );
}

DonatixApi account() => DonatixApi()
  ..usesExistingApi = true
  ..userId = 2
  ..session = cookie(2);

const changedStats = r'''<main id="main">
<h1>Аналитика</h1><div class="period-chips">
<a href="/panel/stats?period=30d">30 дней</a>
<a href="/panel/stats?period=7d">7 дней</a></div>
<div class="grid stats">
<div class="stat"><div class="label">Создано заказов</div><b class="value">17</b></div>
<div class="stat"><div class="label">Выполнено</div><b class="value">12</b></div>
<div class="stat"><div class="label">Оборот</div><b class="value">$42.1234</b></div>
</div><div class="card chart-card"><h3>Динамика заказов</h3>
<svg class="line-chart" viewBox="0 0 720 260">
<line class="gridline" x1="6" x2="710" y1="234" y2="234"/>
<path class="ln c-created" d="M6,234 L710,12"/></svg></div></main>''';

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
    'Current live support uses tickets instead of a missing Telegram code',
    () async {
      final api = account();
      api.dio.httpClientAdapter = SiteAdapter((request) {
        expect(request.method, 'GET');
        expect(request.uri.path, '/panel/support');
        expect(request.headers['Cookie'], contains('dx_session='));
        return htmlResponse(currentSupport());
      });
      final result = await api.existingSite.support();
      expect(result['site_document'], isNotNull);
      expect(result['site_path'], '/panel/support');
      expect(result.containsKey('code'), isFalse);
    },
  );

  testWidgets(
    'Support searches the current knowledge base and opens its category',
    (tester) async {
      final api = account();
      api.dio.httpClientAdapter = SiteAdapter((request) {
        expect(request.method, 'GET');
        if (request.uri.path == '/panel/support') {
          return htmlResponse(currentSupport());
        }
        expect(request.uri.path, '/panel/support/kb/payments');
        return htmlResponse(
          '<main id="main"><h1>Платежи и баланс</h1><details><summary>Сроки</summary><p>Проверьте свою заявку.</p></details></main>',
        );
      });
      await tester.pumpWidget(MaterialApp(home: SupportScreen(api: api)));
      await tester.pumpAndSettle();
      expect(find.text('Чем мы можем помочь?'), findsOneWidget);
      expect(
        find.text('Не удалось загрузить код поддержки. Нажмите «Повторить».'),
        findsNothing,
      );
      await tester.enterText(find.byType(TextField), 'пополнить баланс');
      await tester.pumpAndSettle();
      expect(find.text('Как пополнить баланс?'), findsOneWidget);
      final more = find.text('Подробнее').first;
      await tester.ensureVisible(more);
      await tester.pumpAndSettle();
      await tester.tap(more);
      await tester.pumpAndSettle();
      expect(find.text('Сроки'), findsOneWidget);
      await tester.tap(find.text('Сроки'));
      await tester.pumpAndSettle();
      expect(find.text('Проверьте свою заявку.'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'Support topics submit the original authenticated form and show the ticket',
    (tester) async {
      var posts = 0;
      final api = account();
      api.dio.httpClientAdapter = SiteAdapter((request) {
        expect(request.headers['Cookie'], contains('dx_session='));
        if (request.uri.path == '/panel/support') {
          return htmlResponse(currentSupport());
        }
        if (request.uri.path == '/panel/support/new' &&
            request.method == 'GET') {
          expect(request.uri.queryParameters['topic'], 'payment');
          return htmlResponse('''<main id="main"><h1>Новая заявка</h1>
<form method="post" action="/panel/support/new">
<input type="hidden" name="csrf" value="ticket-csrf">
<input type="hidden" name="topic" value="payment">
<label for="description">Сообщение</label><textarea id="description" name="description" required></textarea>
<button type="submit">Создать заявку</button></form></main>''');
        }
        if (request.uri.path == '/panel/support/new' &&
            request.method == 'POST') {
          posts++;
          final fields = Map.fromEntries((request.data as FormData).fields);
          expect(fields['csrf'], 'ticket-csrf');
          expect(fields['topic'], 'payment');
          expect(fields['description'], 'Проверьте пополнение, пожалуйста.');
          return ResponseBody.fromString(
            '',
            303,
            headers: {
              'location': ['/panel/support/123'],
            },
          );
        }
        expect(request.uri.path, '/panel/support/123');
        return htmlResponse(
          '<main id="main"><h1>Заявка №123</h1><p>Проверьте пополнение, пожалуйста.</p><p>Ожидает ответа</p></main>',
        );
      });
      await tester.pumpWidget(MaterialApp(home: SupportScreen(api: api)));
      await tester.pumpAndSettle();
      final create = find.text('Новая заявка');
      await tester.ensureVisible(create);
      await tester.pumpAndSettle();
      await tester.tap(create);
      await tester.pumpAndSettle();
      expect(find.text('С чем нужна помощь?'), findsOneWidget);
      await tester.tap(find.text('Пополнение не зачислилось'));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byType(TextFormField),
        'Проверьте пополнение, пожалуйста.',
      );
      final submit = find.text('Создать заявку');
      await tester.ensureVisible(submit);
      await tester.pumpAndSettle();
      await tester.tap(submit);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      // NativeForm keeps the existing confirmation before sending a site form.
      await tester.tap(find.widgetWithText(FilledButton, 'Подтвердить'));
      await tester.pumpAndSettle();
      expect(posts, 1);
      expect(find.text('Заявка №123'), findsOneWidget);
      expect(find.text('Ожидает ответа'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'Changed analytics layout keeps actual values and period navigation',
    (tester) async {
      final requested = <String>[];
      final api = account();
      api.dio.httpClientAdapter = SiteAdapter((request) {
        expect(request.method, 'GET');
        expect(request.uri.path, '/panel/stats');
        requested.add(request.uri.queryParameters['period']!);
        return htmlResponse(
          changedStats.replaceFirst(
            '>17<',
            request.uri.queryParameters['period'] == '7d' ? '>8<' : '>17<',
          ),
        );
      });
      await tester.pumpWidget(MaterialApp(home: StatsScreen(api: api)));
      await tester.pumpAndSettle();
      expect(find.text('17'), findsOneWidget);
      expect(find.text('12'), findsOneWidget);
      expect(find.text(r'$42.1234'), findsOneWidget);
      expect(find.text('Сайт не передал статистику.'), findsNothing);
      await tester.tap(find.text('7 дней'));
      await tester.pumpAndSettle();
      expect(find.text('8'), findsOneWidget);
      expect(requested, ['30d', '7d']);
      expect(tester.takeException(), isNull);
    },
  );

  test('Original analytics layout remains supported', () async {
    final api = account();
    final source = File(
      'test/fixtures/admin/admin_stats.html',
    ).readAsStringSync();
    api.dio.httpClientAdapter = SiteAdapter((_) => htmlResponse(source));
    final result = await api.existingSite.stats({'period': '7d'});
    expect(result['analytics']['turnover'], '0.0000');
    expect(result['analytics']['periods']['7d'], '7 дней');
    expect(result.containsKey('site_document'), isFalse);
  });

  testWidgets('Analytics fallback respects the selected display currency', (
    tester,
  ) async {
    final api = account()
      ..displayCurrency = 'TJS'
      ..tjsRate = '10';
    api.dio.httpClientAdapter = SiteAdapter((_) => htmlResponse(changedStats));
    await tester.pumpWidget(MaterialApp(home: StatsScreen(api: api)));
    await tester.pumpAndSettle();
    expect(find.text('421.2340 с.'), findsOneWidget);
    expect(find.text(r'$42.1234'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  test(
    'A login page returned with HTTP 200 expires the session in both sections',
    () async {
      const storage = MethodChannel(
        'plugins.it_nomads.com/flutter_secure_storage',
      );
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(storage, (_) async => null);
      addTearDown(
        () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
            .setMockMethodCallHandler(storage, null),
      );
      for (final section in ['support', 'stats']) {
        var expired = false;
        final api = account()..onSessionExpired = () => expired = true;
        api.dio.httpClientAdapter = SiteAdapter(
          (_) => htmlResponse(
            '<main><form action="/login"><input name="csrf" value="guest"></form></main>',
          ),
        );
        await expectLater(
          section == 'support'
              ? api.existingSite.support()
              : api.existingSite.stats({}),
          throwsA(
            isA<ApiFailure>().having((error) => error.status, 'status', 401),
          ),
        );
        expect(expired, isTrue);
        expect(api.userId, 0);
        expect(api.session, isNull);
      }
    },
  );

  testWidgets('Current support fits a narrow phone with enlarged text', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360, 840);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final api = account();
    api.dio.httpClientAdapter = SiteAdapter(
      (_) => htmlResponse(currentSupport()),
    );
    await tester.pumpWidget(
      RepaintBoundary(
        key: const ValueKey('preview'),
        child: MaterialApp(
          theme: ThemeData(fontFamily: 'Onest'),
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: TextScaler.linear(1.4)),
            child: child!,
          ),
          home: SupportScreen(api: api),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await savePreview(tester, 'support-current');
    final create = find.text('Новая заявка');
    await tester.ensureVisible(create);
    await tester.pumpAndSettle();
    await tester.tap(create);
    await tester.pumpAndSettle();
    expect(find.text('С чем нужна помощь?'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await savePreview(tester, 'support-topics');
  });
}

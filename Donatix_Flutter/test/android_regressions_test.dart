import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:donatix/core/api.dart';
import 'package:donatix/core/checkout.dart';
import 'package:donatix/core/notifications.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:donatix/main.dart';
import 'package:donatix/screens/admin.dart';
import 'package:donatix/screens/auth.dart';
import 'package:donatix/screens/home.dart';
import 'package:donatix/screens/document.dart';
import 'package:html/parser.dart' as html;

class OfflineAdapter implements HttpClientAdapter {
  final FutureOr<ResponseBody> Function(RequestOptions) reply;
  OfflineAdapter(this.reply);
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? stream,
    Future<void>? cancel,
  ) async => reply(options);
  @override
  void close({bool force = false}) {}
}

ResponseBody jsonReply(Map<String, dynamic> value, [int status = 200]) =>
    ResponseBody.fromString(
      jsonEncode(value),
      status,
      headers: {
        Headers.contentTypeHeader: ['application/json'],
      },
    );
ResponseBody pageReply(String value, [int status = 200]) =>
    ResponseBody.fromString(
      value,
      status,
      headers: {
        Headers.contentTypeHeader: ['text/html'],
      },
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const storage = MethodChannel('plugins.it_nomads.com/flutter_secure_storage');
  final saved = <String, String>{};
  setUp(() {
    saved.clear();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(storage, (call) async {
          final arguments = Map<String, dynamic>.from(call.arguments as Map);
          if (call.method == 'read') return saved[arguments['key']];
          if (call.method == 'write') {
            saved[arguments['key']] = arguments['value'];
          }
          if (call.method == 'delete') saved.remove(arguments['key']);
          return null;
        });
  });

  test(
    'Logout during notification configuration queues a native stop',
    () async {
      SharedPreferences.setMockInitialValues({
        'background_notifications': true,
      });
      final api = DonatixApi()
        ..userId = 2
        ..session = 'fixture-session';
      final service = NativeNotifications(api);
      final configuring = Completer<void>();
      final entered = Completer<void>();
      final calls = <String>[];
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(NativeNotifications.channel, (call) async {
            calls.add(call.method);
            if (call.method == 'configureNotifications') {
              entered.complete();
              await configuring.future;
            }
            if (call.method == 'pushStatus') return <String, dynamic>{};
            return null;
          });
      addTearDown(
        () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
            .setMockMethodCallHandler(NativeNotifications.channel, null),
      );
      final first = service.synchronize();
      await entered.future;
      api.userId = 0;
      api.session = null;
      await service.synchronize();
      configuring.complete();
      await first;
      expect(calls.where((c) => c == 'stopNotifications'), hasLength(1));
      expect(service.configured, 'true|0|null');
    },
  );

  testWidgets(
    'Concurrent purchases and late network failure preserve the correct account pending key',
    (tester) async {
      final api = DonatixApi()
        ..userId = 2
        ..session = 'fixture-session';
      final quote = Completer<ResponseBody>();
      var quotes = 0, orders = 0;
      String? sentId;
      api.dio.httpClientAdapter = OfflineAdapter((r) {
        if (r.path.endsWith('/quote')) {
          quotes++;
          return quote.future;
        }
        orders++;
        sentId = r.headers['Idempotency-Key'];
        api.userId =
            3; // Account changed while the original request was pending.
        return jsonReply({'ok': false}, 503);
      });
      late BuildContext screen;
      await tester.pumpWidget(
        MaterialApp(
          home: Builder(
            builder: (c) {
              screen = c;
              return const Scaffold(body: Text('Покупка'));
            },
          ),
        ),
      );
      final body = <String, dynamic>{
        'product_id': 'fixture-product',
        'quantity': 1,
        'fields': {'player_id': '123'},
      };
      Object? failure;
      final first = checkout(screen, api, body, 'Пакет').catchError((Object e) {
        failure = e;
      });
      await expectLater(
        checkout(screen, api, body, 'Пакет'),
        throwsA(isA<ApiFailure>()),
      );
      await tester.pump();
      quote.complete(
        jsonReply({'ok': true, 'total_usd': '1', 'balance_usd': '100'}),
      );
      await tester.pumpAndSettle();
      saved['pending_order_3'] = 'another-account-operation';
      await tester.tap(find.text('Купить'));
      await tester.pumpAndSettle();
      await first;
      expect(quotes, 1);
      expect(orders, 1);
      expect(failure, isA<ApiFailure>());
      expect(jsonDecode(saved['pending_order_2']!)['key'], sentId);
      expect(saved['pending_order_3'], 'another-account-operation');
    },
  );
  testWidgets(
    'Unreadable pending purchase offers history without submitting a payment',
    (tester) async {
      final api = DonatixApi()
        ..userId = 2
        ..session = 'fixture-session';
      saved[api.pendingOrderKey] = jsonEncode({
        'key': '0123456789abcdef',
        'body': {'fields': 'broken'},
      });
      var requests = 0;
      api.dio.httpClientAdapter = OfflineAdapter((r) {
        requests++;
        return jsonReply({'ok': true});
      });
      await tester.pumpWidget(MaterialApp(home: PendingOrderScreen(api: api)));
      await tester.pumpAndSettle();
      expect(find.text('История заказов'), findsOneWidget);
      expect(find.text('Проверить результат'), findsNothing);
      expect(find.textContaining('Не удалось прочитать'), findsOneWidget);
      expect(requests, 0);
      expect(saved[api.pendingOrderKey], isNotNull);
    },
  );
  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(storage, null);
  });

  testWidgets('Home banner animation stops when reduced motion is enabled', (
    tester,
  ) async {
    final api = DonatixApi()..userId = 2;
    api.dio.httpClientAdapter = OfflineAdapter((r) {
      if (r.path.endsWith('/timezone')) {
        return jsonReply({'ok': true, 'choice': 'auto', 'zones': []});
      }
      if (r.path.endsWith('/keys')) {
        return jsonReply({'ok': true, 'items': [], 'active': false});
      }
      return jsonReply({
        'ok': true,
        'login': 'partner',
        'email': 'partner@example.invalid',
        'status': 'active',
        'tier': 'partner',
        'balance': '100',
        'markup': '5',
        'popular': [],
        'summary': {'totalSpent': '0', 'totalOrders': 0},
        'orders_all': 0,
        'dcoin_enabled': false,
        'referral_percent': 5,
        'createdAt': '2026-10-07',
        'project': '',
      });
    });
    Widget screen(bool reduce) => MaterialApp(
      theme: brandTheme(false),
      home: MediaQuery(
        data: MediaQueryData(
          size: const Size(360, 800),
          disableAnimations: reduce,
        ),
        child: Scaffold(
          body: HomeScreen(api: api, navigate: (_) {}),
        ),
      ),
    );
    await tester.pumpWidget(screen(false));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 1500));
    expect(find.text('Дӯстонро даъват кунед — бонус гиред'), findsOneWidget);
    for (final elapsed in [500, 2400, 6000]) {
      await tester.pump(Duration(milliseconds: elapsed));
      expect(tester.takeException(), isNull);
    }
    await tester.pumpWidget(screen(true));
    await tester.pumpAndSettle();
    expect(tester.binding.hasScheduledFrame, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  test(
    'Missing mobile API is distinct from a missing catalogue product',
    () async {
      final api = DonatixApi();
      api.dio.httpClientAdapter = OfflineAdapter(
        (r) => jsonReply({
          'ok': false,
          'error': 'Not Found',
          'code': 'not_found',
        }, 404),
      );
      await expectLater(
        api.get('/api/v1/mobile/config'),
        throwsA(
          isA<ApiFailure>().having(
            (e) => e.message,
            'message',
            contains('Мобильный API'),
          ),
        ),
      );
      api.dio.httpClientAdapter = OfflineAdapter(
        (r) => jsonReply({
          'ok': false,
          'error': 'Товар не найден.',
          'code': 'product_not_found',
        }, 404),
      );
      await expectLater(
        api.get('/api/v1/products/missing'),
        throwsA(
          isA<ApiFailure>().having(
            (e) => e.message,
            'message',
            'Товар не найден.',
          ),
        ),
      );
    },
  );

  testWidgets(
    'Google button appears after retrying a previously missing server module',
    (tester) async {
      var configured = false;
      final api = DonatixApi();
      api.dio.httpClientAdapter = OfflineAdapter(
        (r) => configured
            ? jsonReply({
                'ok': true,
                'google_enabled': true,
                'registration_open': true,
              })
            : jsonReply({'ok': false, 'error': 'Not Found'}, 404),
      );
      await tester.pumpWidget(
        MaterialApp(
          home: AuthScreen(api: api, onDone: () {}),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Войти через Google'), findsNothing);
      expect(find.text('Повторить подключение'), findsOneWidget);
      configured = true;
      await tester.tap(find.text('Повторить подключение'));
      await tester.pumpAndSettle();
      expect(find.text('Войти через Google'), findsOneWidget);
      expect(find.text('Повторить подключение'), findsNothing);
    },
  );

  testWidgets(
    'Username login follows the original form and completes mobile bootstrap',
    (tester) async {
      final api = DonatixApi();
      var loggedIn = false, submitted = 0;
      api.dio.httpClientAdapter = OfflineAdapter((r) {
        if (r.path == '/api/v1/mobile/config') return jsonReply({'ok': true});
        if (r.path == '/api/v1/mobile-session') {
          return jsonReply({
            'ok': true,
            'csrf': 'test',
            'login': 'partner',
            'user_id': 2,
            'tjs_rate': '10',
          });
        }
        if (r.method == 'POST') {
          submitted++;
          expect(r.data['email'], 'partner');
          return ResponseBody.fromString(
            '',
            303,
            headers: {
              'location': ['/panel'],
            },
          );
        }
        return pageReply('<form><input name="csrf" value="test"></form>');
      });
      await tester.pumpWidget(
        MaterialApp(
          home: AuthScreen(api: api, onDone: () => loggedIn = true),
        ),
      );
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextFormField).at(0), 'partner');
      await tester.enterText(find.byType(TextFormField).at(1), 'password123');
      await tester.ensureVisible(find.text('Войти').first);
      await tester.tap(find.text('Войти').first);
      await tester.pumpAndSettle();
      expect(loggedIn, isTrue);
      expect(submitted, 1);
    },
  );

  test('A released stale order lease cannot unlock another purchase', () {
    final api = DonatixApi()
      ..userId = 2
      ..session = 'fixture-session';
    final first = api.beginOrder()!;
    expect(api.beginOrder(), isNull);
    api.endOrder(first);
    final second = api.beginOrder()!;
    api.endOrder(first);
    expect(api.beginOrder(), isNull);
    api.userId = 3;
    expect(() => api.requireAccount(2), throwsA(isA<ApiFailure>()));
    api.endOrder(second);
    expect(api.beginOrder(), isNotNull);
  });

  testWidgets(
    'Repeated admin submission opens one confirmation and makes one POST',
    (tester) async {
      final api = DonatixApi()..csrf = 'fixture-csrf';
      var posts = 0;
      api.dio.httpClientAdapter = OfflineAdapter((r) {
        posts++;
        return pageReply('<main>Сохранено</main>');
      });
      final form = html
          .parse(
            '<form method="post" action="/admin/settings"><button class="primary">Сохранить</button></form>',
          )
          .querySelector('form')!;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: NativeForm(
              api: api,
              form: form,
              sourcePath: '/admin/settings',
              render: (n) => Text(n.text ?? ''),
              completed: ([response]) async {},
            ),
          ),
        ),
      );
      final action = tester
          .widget<FilledButton>(find.byType(FilledButton))
          .onPressed!;
      action();
      action();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.byType(AlertDialog), findsOneWidget);
      await tester.tap(find.text('Подтвердить'));
      await tester.pumpAndSettle();
      expect(posts, 1);
    },
  );

  final fixtures =
      jsonDecode(File('test/fixtures/admin/index.json').readAsStringSync())
          as List;
  for (final entry in fixtures.where((e) => e['route'] != '/admin/pricelist')) {
    final sizes = [
      (const Size(360, 800), false, 1.0),
      (const Size(1024, 900), false, 1.0),
      if ([
        '/admin',
        '/admin/traffic',
        '/admin/pay-settings',
        '/admin/catalog-sync',
        '/admin/settings',
        '/admin/users/2',
      ].contains(entry['route']))
        (const Size(360, 800), true, 1.4),
    ];
    for (final variant in sizes) {
      final size = variant.$1;
      testWidgets(
        'Original admin ${entry['route']} fits ${size.width.toInt()}px dark=${variant.$2} scale=${variant.$3}',
        (tester) async {
          tester.view.physicalSize = size;
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          final api = DonatixApi()
            ..userId = 1
            ..role = 'admin';
          final page = File(
            'test/fixtures/admin/${entry['file']}',
          ).readAsStringSync();
          api.dio.httpClientAdapter = OfflineAdapter(
            (r) => r.path.endsWith('/status')
                ? jsonReply({'ok': true, 'running': false, 'images_local': 0})
                : pageReply(page),
          );
          await tester.pumpWidget(
            MaterialApp(
              theme: brandTheme(variant.$2),
              home: MediaQuery(
                data: MediaQueryData(
                  size: size,
                  disableAnimations: true,
                  textScaler: TextScaler.linear(variant.$3),
                ),
                child: AdminScreen(api: api, path: entry['route']),
              ),
            ),
          );
          await tester.pumpAndSettle();
          expect(find.byType(Scaffold), findsOneWidget);
          expect(find.text('Страница недоступна.'), findsNothing);
          // Exercise off-screen forms and tables as well as the initial viewport.
          final scrollables = tester
              .stateList<ScrollableState>(find.byType(Scrollable))
              .where((s) => s.position.axis == Axis.vertical)
              .toList();
          for (final scrollable in scrollables) {
            var steps = 0;
            while (scrollable.mounted &&
                scrollable.position.pixels <
                    scrollable.position.maxScrollExtent &&
                steps++ < 80) {
              scrollable.position.jumpTo(
                (scrollable.position.pixels + 450).clamp(
                  0,
                  scrollable.position.maxScrollExtent,
                ),
              );
              await tester.pumpAndSettle();
              expect(tester.takeException(), isNull);
            }
          }
          expect(tester.takeException(), isNull);
          await tester.pumpWidget(const SizedBox.shrink());
        },
      );
    }
  }
}

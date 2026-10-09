import 'dart:async';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:donatix/core/api.dart';
import 'package:donatix/screens/deletion.dart';
import 'existing_api_test.dart'
    show SiteAdapter, jsonResponse, htmlResponse, cookie;

DonatixApi account({bool addon = true}) => DonatixApi()
  ..usesExistingApi = true
  ..androidExtension = addon
  ..userId = 2
  ..session = cookie(2)
  ..csrf = 'csrf';

const card = '''<a class="order-card"><span class="title">DX1</span>
  <span class="amount">\$1.2500</span><div class="oc-line">Game package</div>
  <div class="meta"><span class="status completed"></span><span class="pill">2026-10-08</span></div></a>''';
const deletionInfo = <String, dynamic>{
  'ok': true,
  'balance_usd': '0.0000',
  'blockers': <String>[],
  'erased': 'Удаляются личные данные.',
  'retained': 'Сохраняются обезличенные расчётные записи.',
  'support_contact': '@support',
};

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final saved = <String, String>{};
  final nativeCalls = <String>[];
  setUp(() {
    saved.clear();
    nativeCalls.clear();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
          const MethodChannel('plugins.it_nomads.com/flutter_secure_storage'),
          (call) async {
            final args = Map<String, dynamic>.from(
              call.arguments as Map? ?? {},
            );
            switch (call.method) {
              case 'read':
                return saved[args['key']];
              case 'write':
                saved[args['key']] = args['value'] as String;
                break;
              case 'delete':
                saved.remove(args['key']);
                break;
              case 'deleteAll':
                saved.clear();
                break;
            }
            return null;
          },
        );
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
          const MethodChannel('tj.donatix.app/native'),
          (call) async {
            nativeCalls.add(call.method);
            return null;
          },
        );
  });

  test(
    'History reads session-authenticated JSON with original filters and without purchases',
    () async {
      final api = account();
      api.dio.httpClientAdapter = SiteAdapter((o) {
        expect(o.method, 'GET');
        expect(o.uri.path, '/api/v1/android/orders');
        expect(o.headers['Cookie'], contains('dx_session='));
        expect(o.queryParameters, {
          'page': 2,
          'status': 'completed',
          'q': 'player',
          'period': 'all',
        });
        return jsonResponse({
          'ok': true,
          'items': [
            {'order_id': 'DX1'},
          ],
          'total': 31,
          'page': 2,
          'limit': 30,
        });
      });
      final d = await api.get('/api/v1/orders', {
        'page': 2,
        'limit': 20,
        'status': 'completed',
        'q': 'player',
        'period': 'all',
      });
      expect(d['items'][0]['order_id'], 'DX1');
      expect(d['limit'], 30);
    },
  );

  test(
    'Previous addon falls back to valid cards without depending on summary wording',
    () async {
      final api = account();
      final routes = <String>[];
      api.dio.httpClientAdapter = SiteAdapter((o) {
        routes.add(o.uri.path);
        if (o.uri.path == '/api/v1/android/orders') {
          return jsonResponse({'ok': false}, 404);
        }
        return htmlResponse(
          '<div id="main"><section class="page-hero"><p>История покупок</p></section>$card</div><nav class="pager"><a href="/panel/orders?page=2">Далее</a></nav>',
        );
      });
      final d = await api.get('/api/v1/orders', {'page': 1});
      expect(routes, ['/api/v1/android/orders', '/panel/orders']);
      expect(d['items'][0]['order_id'], 'DX1');
      expect(d['total'], isNull);
      expect(d['totals'], isNull);
      expect(d['has_next'], isTrue);
    },
  );

  test(
    'A genuine empty HTML history is accepted without invented statistics',
    () async {
      final api = account(addon: false);
      api.dio.httpClientAdapter = SiteAdapter(
        (o) => htmlResponse(
          '<div id="main"><section class="list"><div class="card empty">Заказов нет.</div></section></div>',
        ),
      );
      final d = await api.get('/api/v1/orders');
      expect(d['items'], isEmpty);
      expect(d['total'], isNull);
      expect(d['has_next'], isFalse);
    },
  );

  test(
    'Unrelated HTML is still an error rather than a false empty history',
    () async {
      final api = account(addon: false);
      api.dio.httpClientAdapter = SiteAdapter(
        (o) => htmlResponse('<div id="main">Привяжите Telegram</div>'),
      );
      await expectLater(api.get('/api/v1/orders'), throwsA(isA<ApiFailure>()));
    },
  );

  for (final status in [401, 403, 500]) {
    test(
      'History does not hide HTTP $status by fetching an empty HTML page',
      () async {
        final api = account();
        var requests = 0;
        api.dio.httpClientAdapter = SiteAdapter((o) {
          requests++;
          return jsonResponse({'ok': false, 'error': 'Failure'}, status);
        });
        await expectLater(
          api.get('/api/v1/orders'),
          throwsA(anyOf(isA<ApiFailure>(), isA<DioException>())),
        );
        expect(requests, 1);
      },
    );
  }

  test(
    'Deletion maps to the Android addon with cookie, CSRF and unchanged payload',
    () async {
      final api = account();
      api.dio.httpClientAdapter = SiteAdapter((o) {
        expect(o.uri.path, '/api/v1/android/account/deletion');
        expect(o.headers['X-CSRF-Token'], 'csrf');
        expect(o.headers['Cookie'], contains('dx_session='));
        if (o.method == 'GET') return jsonResponse(deletionInfo);
        expect(o.data, {
          'confirmation': 'УДАЛИТЬ',
          'password': 'current-password',
        });
        return jsonResponse({
          'ok': true,
          'state': 'completed',
          'reasons': [],
          'message': 'Данные удалены.',
        });
      });
      expect(
        (await api.get('/api/v1/mobile/account/deletion'))['retained'],
        deletionInfo['retained'],
      );
      expect(
        (await api.post('/api/v1/mobile/account/deletion', {
          'confirmation': 'УДАЛИТЬ',
          'password': 'current-password',
        }))['state'],
        'completed',
      );
    },
  );

  test(
    'Old server reports a missing deletion update without pretending to erase anything',
    () async {
      final api = account();
      api.dio.httpClientAdapter = SiteAdapter(
        (o) => jsonResponse({'ok': false}, 404),
      );
      await expectLater(
        api.post('/api/v1/mobile/account/deletion', {
          'confirmation': 'УДАЛИТЬ',
        }),
        throwsA(isA<ApiFailure>().having((e) => e.status, 'status', 501)),
      );
      expect(api.userId, 2);
    },
  );

  test('Late history cannot cross an account switch', () async {
    final api = account();
    final response = Completer<ResponseBody>();
    final entered = Completer<void>();
    api.dio.httpClientAdapter = SiteAdapter((o) {
      entered.complete();
      return response.future;
    });
    final pending = api.get('/api/v1/orders');
    final assertion = expectLater(pending, throwsA(isA<ApiFailure>()));
    await entered.future;
    api.userId = 3;
    response.complete(
      jsonResponse({
        'ok': true,
        'items': [
          {'order_id': 'PRIVATE-OLD'},
        ],
      }),
    );
    await assertion;
  });

  testWidgets(
    'Successful deletion explains retention and clears local credentials/files',
    (tester) async {
      final api = account();
      saved['private-receipt'] = 'private';
      var expired = false, deletes = 0;
      api.onSessionExpired = () => expired = true;
      api.dio.httpClientAdapter = SiteAdapter((o) {
        if (o.method == 'GET') return jsonResponse(deletionInfo);
        deletes++;
        return jsonResponse({
          'ok': true,
          'state': 'completed',
          'reasons': [],
          'message': 'Данные удалены.',
        });
      });
      await tester.pumpWidget(MaterialApp(home: DeletionScreen(api: api)));
      await tester.pumpAndSettle();
      expect(find.text(deletionInfo['retained'] as String), findsOneWidget);
      await tester.enterText(
        find.widgetWithText(TextField, 'Введите УДАЛИТЬ'),
        'УДАЛИТЬ',
      );
      await tester.pump();
      await tester.ensureVisible(
        find.widgetWithText(FilledButton, 'Удалить аккаунт и данные'),
      );
      await tester.tap(
        find.widgetWithText(FilledButton, 'Удалить аккаунт и данные'),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('Подтвердить'));
      // The request's spinner intentionally stays active while its result
      // dialog awaits acknowledgement; settling all animations would time out.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      await tester.tap(find.text('Понятно'));
      await tester.pumpAndSettle();
      expect(deletes, 1);
      expect(api.userId, 0);
      expect(api.session, isNull);
      expect(saved, isEmpty);
      expect(nativeCalls, contains('clearPrivateFiles'));
      expect(expired, isTrue);
    },
  );

  testWidgets(
    'Account change during deletion confirmation cannot delete the new account',
    (tester) async {
      final api = account();
      var deletes = 0;
      api.dio.httpClientAdapter = SiteAdapter((o) {
        if (o.method == 'POST') deletes++;
        return jsonResponse(deletionInfo);
      });
      await tester.pumpWidget(MaterialApp(home: DeletionScreen(api: api)));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.widgetWithText(TextField, 'Введите УДАЛИТЬ'),
        'УДАЛИТЬ',
      );
      await tester.pump();
      await tester.ensureVisible(
        find.widgetWithText(FilledButton, 'Удалить аккаунт и данные'),
      );
      await tester.tap(
        find.widgetWithText(FilledButton, 'Удалить аккаунт и данные'),
      );
      await tester.pumpAndSettle();
      api.userId = 3;
      api.session = cookie(3);
      await tester.tap(find.text('Подтвердить'));
      await tester.pumpAndSettle();
      expect(deletes, 0);
      expect(api.userId, 3);
      expect(nativeCalls, isEmpty);
    },
  );
}

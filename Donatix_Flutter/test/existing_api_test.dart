import 'dart:async';
import 'dart:convert';
import 'package:dio/dio.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:donatix/core/api.dart';

class SiteAdapter implements HttpClientAdapter {
  final FutureOr<ResponseBody> Function(RequestOptions) reply;
  SiteAdapter(this.reply);
  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? stream,
      Future<void>? cancel) async => reply(options);
  @override
  void close({bool force = false}) {}
}

ResponseBody jsonResponse(Map<String, dynamic> body, [int status = 200]) =>
    ResponseBody.fromString(jsonEncode(body), status,
      headers: {Headers.contentTypeHeader: ['application/json']});
ResponseBody htmlResponse(String body) => ResponseBody.fromString(body, 200,
    headers: {Headers.contentTypeHeader: ['text/html']});
String cookie(int id) => '${base64Url.encode(utf8.encode(jsonEncode({
  'user_id': id, 'sid': 'fixture-$id'})))}.timestamp.signature';
const profile = '''<div id="main">
<form action="/logout"><input name="csrf" value="fixture-csrf"></form>
<div class="who"><b>fixture-user</b><span class="muted">fixture@example.com</span></div>
<div class="tier-card ok"><b class="tier-name">Bronze</b></div>
<div class="bal-amount">\$100.0000</div>
<div class="bal-tile"><b class="t-value">\$20.0000</b></div>
<div class="bal-tile"><b class="t-value">3</b><span class="t-note">из 4 всего</span></div>
<a class="bell"><span class="dot-count">2</span></a></div>''';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final saved = <String, String>{};
  const storage = MethodChannel('plugins.it_nomads.com/flutter_secure_storage');
  setUp(() {
    saved.clear();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(storage, (call) async {
      final args = Map<String, dynamic>.from(call.arguments as Map);
      if (call.method == 'read') return saved[args['key']];
      if (call.method == 'write') saved[args['key']] = args['value'];
      if (call.method == 'delete') saved.remove(args['key']);
      return null;
    });
  });

  test('Missing mobile config uses existing forms without replacing the session', () async {
    final api = DonatixApi()..session = cookie(2);
    final original = api.session;
    api.dio.httpClientAdapter = SiteAdapter((o) {
      if (o.uri.path == '/api/v1/mobile/config') {
        return jsonResponse({'ok': false, 'error': 'Not Found'}, 404);
      }
      expect(o.headers['Cookie'], 'dx_cur=USD');
      final route = o.uri.path;
      return ResponseBody.fromString(
        '<form action="$route"><input name="csrf" value="guest-csrf"></form>', 200,
        headers: {Headers.contentTypeHeader: ['text/html'],
          'set-cookie': ['dx_session=guest-session; Path=/; Secure']});
    });
    final config = await api.get('/api/v1/mobile/config');
    expect(api.usesExistingApi, isTrue);
    expect(config['registration_open'], isTrue);
    expect(config['google_enabled'], isFalse);
    expect(api.session, original);
    expect(saved['donatix_session'], isNull);
  });

  test('Active login obtains a dedicated account key through the existing form', () async {
    final api = DonatixApi()..usesExistingApi = true..session = cookie(2);
    var made = false;
    final paths = <String>[];
    api.dio.httpClientAdapter = SiteAdapter((o) {
      paths.add('${o.method} ${o.uri.path}');
      switch (o.uri.path) {
        case '/panel': return htmlResponse(profile);
        case '/panel/data/rate': return jsonResponse({'tjs_rate': '11.2'});
        case '/panel/api': return htmlResponse(made
            ? '<input name="csrf" value="fixture-csrf"><code id="new-key">own-key</code>'
            : '<input name="csrf" value="fixture-csrf"><table></table>');
        case '/panel/api/keys':
          expect((o.data as Map)['name'], 'Donatix Android');
          made = true;
          return ResponseBody.fromString('', 303, headers: {'location': ['/panel/api']});
        case '/api/v1/me':
          expect(o.headers['X-API-Key'], 'own-key');
          return jsonResponse({'ok': true, 'login': 'fixture-user'});
        default: fail('Unexpected route ${o.uri.path}');
      }
    });
    await api.bootstrap();
    expect(api.userId, 2);
    expect(api.personalApiKey, 'own-key');
    expect(saved['donatix_rest_key_2'], 'own-key');
    expect(paths.where((p) => p.startsWith('POST')), ['POST /panel/api/keys']);
  });

  test('Explicit key tests are not overwritten by the current personal key', () async {
    final api = DonatixApi()..usesExistingApi = true..userId = 2
      ..session = cookie(2)..personalApiKey = 'own-key';
    api.dio.httpClientAdapter = SiteAdapter((o) {
      expect(o.headers['X-API-Key'], 'tested-key');
      return jsonResponse({'login': 'fixture-user'});
    });
    await api.existingSite.json('GET', '/api/v1/me', keyOverride: 'tested-key');
  });

  test('Background bell preview does not open the auto-read inbox page', () async {
    final api = DonatixApi()..usesExistingApi = true..userId = 2..session = cookie(2);
    api.dio.httpClientAdapter = SiteAdapter((o) {
      expect(o.uri.path, '/panel');
      return htmlResponse(profile);
    });
    final inbox = await api.get('/api/v1/mobile/notifications', {'preview': true});
    expect(inbox['unread'], 2);
    expect(inbox['items'], isEmpty);
  });

  test('Order filters use existing website parameters and its 30-item pages', () async {
    final api = DonatixApi()..usesExistingApi = true..userId = 2..session = cookie(2);
    api.dio.httpClientAdapter = SiteAdapter((o) {
      expect(o.uri.path, '/panel/orders');
      expect(o.queryParameters, {'page': 2, 'q': 'player', 'period': '7d', 'status': 'completed'});
      return htmlResponse('''<div id="main"><section class="page-hero"><p>7 дней: 31 · выполнено 30 на \$12.0001 · возвращено 1</p></section>
        <a class="order-card"><span class="title">DX31</span><span class="amount">\$1.0001</span><div class="oc-line">Пакет</div>
        <div class="meta"><span class="status completed"></span><span class="pill">2026-10-07</span></div></a></div>''');
    });
    final d = await api.get('/api/v1/orders', {'page': 2, 'limit': 20,
      'q': 'player', 'period': '7d', 'status': 'completed'});
    expect(d['total'], 31);
    expect(d['limit'], 30);
    expect(d['totals'], {'done': 30, 'spent': '12.0001', 'failed': 1});
    expect(d['items'][0]['order_id'], 'DX31');
  });

  test('Changed product price rejects a purchase before an order POST', () async {
    final api = DonatixApi()..usesExistingApi = true..userId = 2
      ..session = cookie(2)..personalApiKey = 'own-key'..status = 'active';
    var charges = 0;
    api.dio.httpClientAdapter = SiteAdapter((o) {
      if (o.uri.path == '/panel') return htmlResponse(profile);
      if (o.uri.path == '/api/v1/products/pack') { return jsonResponse({'product': {
        'price_usd': '2.0001', 'kind': 'topup', 'min_quantity': 1,
        'max_quantity': 1, 'fields': []}}); }
      if (o.uri.path == '/api/v1/balance') return jsonResponse({'balance': '100'});
      charges++;
      fail('Unexpected charge route ${o.uri.path}');
    });
    await expectLater(api.post('/api/v1/orders', {'product_id': 'pack', 'quantity': 1,
      'fields': <String, String>{}, 'expected_total_usd': '2.0000'},
      idempotency: 'fixture-operation-1234'), throwsA(isA<ApiFailure>().having((e) => e.status, 'status', 422)));
    expect(charges, 0);
  });

  test('Money quotes use decimal arithmetic and supplier-compatible ceiling', () {
    expect(ExistingSiteApi.totalPrice('0.000001', '3'), '0.0001');
    expect(ExistingSiteApi.totalPrice('0.98', '1234', divisor: '100'), '12.0932');
    expect(ExistingSiteApi.totalPrice('1e-8', '10000000'), '0.1000');
    expect(ExistingSiteApi.totalPrice('0.333333', '3'), '1.0000');
  });

  test('A late response cannot restore the logged-out account cookie', () async {
    final api = DonatixApi()..usesExistingApi = true..userId = 2
      ..session = cookie(2)..personalApiKey = 'own-key';
    final entered = Completer<void>(), reply = Completer<ResponseBody>();
    api.dio.httpClientAdapter = SiteAdapter((o) { entered.complete(); return reply.future; });
    final request = api.get('/api/v1/balance');
    await entered.future;
    await api.clear();
    api.userId = 3; api.session = cookie(3);
    reply.complete(ResponseBody.fromString('{"balance":"10","ok":true}', 200,
      headers: {Headers.contentTypeHeader: ['application/json'],
        'set-cookie': ['dx_session=${cookie(2)}; Path=/; Secure']}));
    await expectLater(request, throwsA(isA<ApiFailure>()));
    expect(api.userId, 3);
    expect(api.session, cookie(3));
    expect(saved['donatix_session'], isNull);
  });
}

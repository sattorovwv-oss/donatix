import 'dart:convert';
import 'dart:io';
import 'package:dio/dio.dart';
import 'package:flutter/services.dart';
import 'package:decimal/decimal.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:html/parser.dart' as html;

class ApiFailure implements Exception {
  final String message;
  final int? status;
  const ApiFailure(this.message, [this.status]);
  @override
  String toString() => message;
}

/// Only the signed session cookie is persisted. Passwords and supplier keys
/// never enter storage. No automatic retries for financial POST requests.
class DonatixApi {
  static const origin = String.fromEnvironment(
    'DONATIX_URL',
    defaultValue: 'https://donatix.tj',
  );
  final storage = const FlutterSecureStorage();
  late final Dio dio;
  String? session;
  String csrf = '';
  String login = '';
  int userId = 0;
  String role = 'client';
  String status = '';
  String? nextLink;
  Future<void> Function()? onSessionChanged;
  String displayCurrency = 'USD';
  String tjsRate = '1';
  String displayPrice(dynamic usd) {
    final raw = '$usd';
    if (displayCurrency == 'USD') return '\$$raw';
    try {
      return '${(Decimal.parse(raw) * Decimal.parse(tjsRate)).toStringAsFixed(4)} с.';
    } catch (_) {
      return raw;
    }
  }

  void Function()? onSessionExpired;
  String get pendingOrderKey => 'pending_order_$userId';
  DonatixApi() {
    final uri = Uri.parse(origin);
    if (uri.scheme != 'https' ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.path.isNotEmpty && uri.path != '/')) {
      throw const ApiFailure('Адрес сервера должен использовать HTTPS.');
    }
    dio = Dio(
      BaseOptions(
        baseUrl: origin,
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(seconds: 45),
        sendTimeout: const Duration(seconds: 45),
        followRedirects: false,
        validateStatus: (s) => s != null && s < 500,
        headers: {
          'Accept': 'application/json',
          'User-Agent': 'DonatixNative/1.0',
        },
      ),
    );
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (o, h) {
          if (session != null) {
            o.headers['Cookie'] =
                'dx_session=$session; dx_cur=$displayCurrency';
          }
          if (csrf.isNotEmpty) o.headers['X-CSRF-Token'] = csrf;
          h.next(o);
        },
        onResponse: (r, h) async {
          for (final raw in r.headers['set-cookie'] ?? <String>[]) {
            final cookie = Cookie.fromSetCookieValue(raw);
            if (cookie.name != 'dx_session') continue;
            session = cookie.value.isEmpty || cookie.maxAge == 0
                ? null
                : cookie.value;
            if (session == null) {
              await storage.delete(key: 'donatix_session');
            } else {
              await storage.write(key: 'donatix_session', value: session);
            }
          }
          await onSessionChanged?.call();
          h.next(r);
        },
      ),
    );
  }
  Future<bool> restore() async {
    session = await storage.read(key: 'donatix_session');
    if (session == null) return false;
    try {
      await bootstrap();
      return true;
    } on ApiFailure catch (e) {
      if (e.status == 401 || e.status == 403) {
        await clear();
        return false;
      }
      rethrow;
    }
  }

  Future<Map<String, dynamic>> bootstrap() async {
    final d = await get('/api/v1/mobile-session');
    if (d['csrf'] is! String ||
        d['login'] is! String ||
        d['user_id'] is! int ||
        d['tjs_rate'] is! String) {
      throw const ApiFailure('Обновите мобильное расширение API на сервере.');
    }
    csrf = d['csrf'] as String;
    login = d['login'] as String;
    userId = d['user_id'] as int;
    role = '${d['role'] ?? 'client'}';
    status = '${d['status'] ?? ''}';
    tjsRate = d['tjs_rate'] as String;
    return d;
  }

  Future<void> clear() async {
    session = null;
    userId = 0;
    role = 'client';
    status = '';
    csrf = '';
    await storage.delete(key: 'donatix_session');
    try {
      await const MethodChannel(
        'tj.donatix.app/native',
      ).invokeMethod<void>('clearAppleCredential');
    } on MissingPluginException {
      /* Android uses its existing login providers. */
    } on PlatformException {
      /* Server logout still revokes the session. */
    }
    await onSessionChanged?.call();
  }

  Future<void> logout() async {
    try {
      await form('/logout', {'csrf': csrf});
    } finally {
      await clear();
    }
  }

  Future<Map<String, dynamic>> get(
    String path, [
    Map<String, dynamic>? query,
  ]) => request('GET', path, query: query);
  Future<Map<String, dynamic>> post(
    String path,
    dynamic data, {
    String? idempotency,
  }) => request('POST', path, data: data, idempotency: idempotency);
  Future<Map<String, dynamic>> request(
    String method,
    String path, {
    dynamic data,
    Map<String, dynamic>? query,
    String? idempotency,
  }) async {
    try {
      if (userId == 0 && path == '/api/v1/accounts/check') {
        path = '/api/v1/mobile/public/accounts/check';
      }
      if (userId == 0 && path.startsWith('/api/v1/mobile/gamekeys/')) {
        path = path.replaceFirst('/api/v1/mobile/', '/api/v1/mobile/public/');
      }
      if (method == 'GET' && userId == 0) {
        if (path == '/api/v1/mobile/categories') {
          path = '/api/v1/mobile/public/categories';
        } else if (path.startsWith('/api/v1/products') ||
            path.startsWith('/api/v1/steam-gifts/games')) {
          path = path.replaceFirst('/api/v1/', '/api/v1/mobile/public/');
        }
      }
      final r = await dio.request<dynamic>(
        path,
        data: data,
        queryParameters: query,
        options: Options(
          method: method,
          headers: {if (idempotency != null) 'Idempotency-Key': idempotency},
        ),
      );
      dynamic d = r.data;
      if (d is String) {
        try {
          d = jsonDecode(d);
        } catch (_) {
          d = null;
        }
      }
      if (r.statusCode == 401 && session != null) {
        await clear();
        onSessionExpired?.call();
      }
      if (d is! Map || r.statusCode! >= 300 || d['ok'] == false) {
        throw ApiFailure(
          d is Map
              ? '${d['error'] ?? d['detail'] ?? 'Запрос отклонён'}'
              : r.statusCode == 404
              ? 'На сервере не установлен мобильный модуль.'
              : 'Сервер вернул неожиданный ответ.',
          r.statusCode,
        );
      }
      return Map<String, dynamic>.from(d);
    } on DioException {
      throw const ApiFailure(
        'Нет связи с сервером. Проверьте интернет. Если вы оформляли заказ, сначала проверьте его в истории.',
      );
    }
  }

  Future<String> decodeHtml(List<int> bytes) async =>
      utf8.decode(bytes, allowMalformed: true);

  Future<Response<dynamic>> form(String path, Map<String, String> data) =>
      dio.post<dynamic>(
        path,
        data: data,
        options: Options(contentType: Headers.formUrlEncodedContentType),
      );
  Future<bool> signIn(
    Map<String, String> fields, {
    bool register = false,
  }) async {
    final route = register ? '/register' : '/login';
    final page = await dio.get<String>(
      route,
      queryParameters: register && (fields['ref'] ?? '').isNotEmpty
          ? {'ref': fields['ref']}
          : null,
      options: Options(responseType: ResponseType.plain),
    );
    final token = html
        .parse(page.data)
        .querySelector('input[name="csrf"]')
        ?.attributes['value'];
    if (token == null) {
      throw const ApiFailure('Не удалось открыть форму входа.');
    }
    final r = await form(route, {...fields, 'csrf': token});
    if (r.statusCode == 303 || r.statusCode == 302) {
      if ((r.headers.value('location') ?? '').contains('/login/code')) {
        final page = await dio.get<String>(
          '/login/code',
          options: Options(responseType: ResponseType.plain),
        );
        csrf =
            html
                .parse(page.data)
                .querySelector('input[name="csrf"]')
                ?.attributes['value'] ??
            '';
        return false;
      }
      await bootstrap();
      return true;
    }
    final doc = html.parse('${r.data}');
    throw ApiFailure(
      doc
              .querySelector('.flash.bad, .flash.error, .error, [role="alert"]')
              ?.text
              .trim() ??
          (r.statusCode == 429
              ? 'Слишком много попыток. Подождите 15 минут.'
              : 'Проверьте введённые данные.'),
      r.statusCode,
    );
  }

  Future<void> verifyCode(String code) async {
    final r = await form('/login/code', {'csrf': csrf, 'code': code});
    if (r.statusCode != 303 && r.statusCode != 302) {
      throw const ApiFailure('Код не принят или истёк.');
    }
    await bootstrap();
  }

  Future<Map<String, dynamic>> receipt(int id, String path) async {
    final file = File(path);
    if (await file.length() > 10 * 1024 * 1024) {
      throw const ApiFailure('Чек должен быть меньше 10 МБ.');
    }
    return post(
      '/api/v1/payments/$id/receipt',
      FormData.fromMap({'file': await MultipartFile.fromFile(path)}),
    );
  }
}

// ignore_for_file: prefer_interpolation_to_compose_strings

part of 'api.dart';

/// Compatibility with the unmodified Donatix website. Only its existing HTTP
/// API and authenticated forms are used. No database access or migrations.
class ExistingSiteApi {
  final DonatixApi api;
  ExistingSiteApi(this.api);
  Map<String, dynamic>? _configuration;
  void reset() => _configuration = null;

  static String text(dom.Element? element) =>
      (element?.text ?? '').replaceAll(RegExp(r'\s+'), ' ').trim();
  static String number(String value) =>
      RegExp(r'[+-]?\d+(?:[.,]\d+)?')
          .firstMatch(value.replaceAll(RegExp(r'(?<=\d)[\s\u00a0]+(?=\d)'), ''))
          ?.group(0)
          ?.replaceAll(',', '.') ??
      '';
  static int integer(String value) => int.tryParse(number(value)) ?? 0;
  static dom.Element? ancestor(dom.Element element, String name) {
    dom.Element? current = element.parent;
    while (current != null) {
      if (current.localName == name) return current;
      current = current.parent;
    }
    return null;
  }

  static String usd(dom.Element? element) {
    if (element == null) throw const ApiFailure('Сервер не передал цену.');
    final titled =
        element.attributes['title'] ??
        element.querySelector(r'[title^="$"]')?.attributes['title'];
    final source = titled ?? text(element);
    final found = RegExp(r'\$([+-]?\d+(?:[.,]\d+)?)').firstMatch(source);
    if (found == null) {
      throw const ApiFailure('Не удалось прочитать цену сайта.');
    }
    return found.group(1)!.replaceAll(',', '.');
  }

  Future<dom.Document> page(
    String path, {
    Map<String, dynamic>? query,
    bool authenticated = true,
    bool withSession = true,
  }) async {
    final owner = api.userId, epoch = api._sessionEpoch;
    for (var redirects = 0; redirects < 5; redirects++) {
      final uri = Uri.parse(DonatixApi.origin).resolve(path);
      if (uri.origin != Uri.parse(DonatixApi.origin).origin) {
        throw const ApiFailure('Адрес страницы не принадлежит Donatix.');
      }
      final response = await api.dio.get<String>(
        uri.toString(),
        queryParameters: query,
        options: Options(
          responseType: ResponseType.plain,
          extra: {'donatix_ignore_cookie': !withSession},
          headers: {
            'Accept': 'text/html',
            'Cookie':
                (!withSession || api.session == null
                    ? ''
                    : 'dx_session=' + api.session! + '; ') +
                'dx_cur=USD',
          },
        ),
      );
      if (epoch != api._sessionEpoch || owner != api.userId) {
        throw const ApiFailure(
          'Аккаунт изменился. Откройте страницу заново.',
          401,
        );
      }
      final status = response.statusCode ?? 0;
      if ([301, 302, 303, 307, 308].contains(status)) {
        final target = response.headers.value('location');
        if (target == null) {
          throw const ApiFailure('Сервер не передал адрес перехода.');
        }
        final next = uri.resolve(target);
        if (authenticated && ['/login', '/register'].contains(next.path)) {
          await api.clear();
          api.onSessionExpired?.call();
          throw const ApiFailure('Сессия истекла. Войдите снова.', 401);
        }
        path = next.toString();
        query = null;
        continue;
      }
      if (status < 200 || status >= 300) {
        final doc = html.parse(response.data);
        throw ApiFailure(
          text(
                doc.querySelector('.flash.error, .flash.bad, [role="alert"]'),
              ).isEmpty
              ? 'Не удалось открыть страницу сайта.'
              : text(
                  doc.querySelector('.flash.error, .flash.bad, [role="alert"]'),
                ),
          status,
        );
      }
      final doc = html.parse(response.data);
      final csrf = doc.querySelector('input[name="csrf"]')?.attributes['value'];
      if (csrf != null && csrf.isNotEmpty && authenticated) api.csrf = csrf;
      return doc;
    }
    throw const ApiFailure('Слишком много переходов на сайте.');
  }

  Future<Map<String, dynamic>> json(
    String method,
    String path, {
    dynamic data,
    Map<String, dynamic>? query,
    String? idempotency,
    String? keyOverride,
  }) async {
    final owner = api.userId, epoch = api._sessionEpoch;
    final r = await api.dio.request<dynamic>(
      path,
      data: data,
      queryParameters: query,
      options: Options(
        method: method,
        headers: {
          if (idempotency != null) 'Idempotency-Key': idempotency,
          if (keyOverride != null) 'X-API-Key': keyOverride,
        },
      ),
    );
    if (epoch != api._sessionEpoch || owner != api.userId) {
      throw const ApiFailure(
        'Аккаунт изменился. Проверьте историю перед повтором.',
        401,
      );
    }
    dynamic body = r.data;
    if (body is String) {
      try {
        body = jsonDecode(body);
      } catch (_) {
        body = null;
      }
    }
    if (body is! Map || (r.statusCode ?? 0) >= 300 || body['ok'] == false) {
      throw ApiFailure(
        body is Map
            ? (body['error'] ?? body['detail'] ?? 'Запрос отклонён').toString()
            : 'Сервер вернул неожиданный ответ.',
        r.statusCode,
      );
    }
    return {'ok': true, ...Map<String, dynamic>.from(body)};
  }

  Future<dom.Document> form(
    String path,
    Map<String, String> values, {
    required String back,
  }) async {
    final owner = api.userId, epoch = api._sessionEpoch;
    if (owner <= 0) throw const ApiFailure('Войдите в аккаунт.', 401);
    await page(back);
    api.requireAccount(owner);
    final r = await api.form(path, {...values, 'csrf': api.csrf});
    if (epoch != api._sessionEpoch) {
      throw const ApiFailure(
        'Аккаунт изменился. Проверьте результат операции.',
        401,
      );
    }
    api.requireAccount(owner);
    if (![200, 302, 303].contains(r.statusCode)) {
      final doc = html.parse(r.data.toString());
      final message = text(
        doc.querySelector('.flash.error, .flash.bad, [role="alert"]'),
      );
      throw ApiFailure(
        message.isEmpty ? 'Операция отклонена сайтом.' : message,
        r.statusCode,
      );
    }
    final doc = [302, 303].contains(r.statusCode)
        ? await page(r.headers.value('location') ?? back)
        : html.parse(r.data.toString());
    final failure = text(doc.querySelector('.flash.error, .flash.bad'));
    if (failure.isNotEmpty) throw ApiFailure(failure, 422);
    return doc;
  }

  Future<Map<String, dynamic>> configuration() async {
    if (_configuration != null) {
      api.usesExistingApi = true;
      return _configuration!;
    }
    final login = await page(
      '/login',
      authenticated: false,
      withSession: false,
    );
    if (login.querySelector('form[action="/login"] input[name="csrf"]') ==
        null) {
      throw const ApiFailure('Сайт не передал форму входа.');
    }
    final registration = await page(
      '/register',
      authenticated: false,
      withSession: false,
    );
    api.usesExistingApi = true;
    api.androidExtension = false;
    api.androidPushEnabled = false;
    var googleEnabled = false;
    // Optional isolated module; a missing module never disables password login.
    if (!Platform.isIOS && defaultTargetPlatform == TargetPlatform.android) {
      try {
        final response = await api.dio.get<dynamic>(
          '/api/v1/android/config',
          options: Options(
            extra: {'donatix_ignore_cookie': true},
            headers: {'Cookie': '', 'Accept': 'application/json'},
          ),
        );
        dynamic extension = response.data;
        if (extension is String) extension = jsonDecode(extension);
        if (response.statusCode == 200 &&
            extension is Map &&
            extension['ok'] == true &&
            extension['android_extension_version'] == 1) {
          api.androidExtension = true;
          api.androidPushEnabled = extension['fcm_enabled'] == true;
          googleEnabled = extension['google_enabled'] == true;
        }
      } on DioException {
        /* Existing site continues to work. */
      } on FormatException {
        /* Unknown response is not a supported module. */
      }
    }
    _configuration = {
      'ok': true,
      'site_name': text(login.querySelector('.brand')).isEmpty
          ? 'Donatix'
          : text(login.querySelector('.brand')),
      'registration_open':
          registration.querySelector('form[action="/register"]') != null,
      'google_enabled': googleEnabled,
      'site_google_enabled':
          login.querySelector('a[href="/auth/google"]') != null,
      'apple_enabled': false,
      'features': ['existing_rest_api', 'existing_site_forms'],
      'support_contact':
          login.querySelector('a[href^="https://t.me/"]')?.attributes['href'] ??
          '',
      'tg_channel': login.querySelector('.side-tg')?.attributes['href'] ?? '',
    };
    return _configuration!;
  }

  static Map<String, dynamic> sessionPayload(String cookie) {
    try {
      return Map<String, dynamic>.from(
        jsonDecode(
              utf8.decode(
                base64Url.decode(base64Url.normalize(cookie.split('.').first)),
              ),
            )
            as Map,
      );
    } catch (_) {
      throw const ApiFailure('Сайт передал некорректную сессию.', 401);
    }
  }

  Future<Map<String, dynamic>> bootstrap() async {
    final epoch = api._sessionEpoch;
    api.personalKeyVerified = false;
    final doc = await page('/panel');
    if (api.session == null ||
        doc.querySelector('form[action="/logout"]') == null) {
      throw const ApiFailure('Войдите в аккаунт.', 401);
    }
    // Cookie contents are used only after the website has authenticated this
    // very cookie. They do not grant access; all operations are checked there.
    final payload = sessionPayload(api.session!);
    final id = payload['user_id'];
    if (id is! int || id <= 0 || api.csrf.isEmpty) {
      throw const ApiFailure('Не удалось подтвердить аккаунт на сайте.', 401);
    }
    api.userId = id;
    api.login = text(doc.querySelector('.who b, .app-hello b'));
    api.role = doc.querySelector('.side a[href="/admin"]') != null
        ? 'admin'
        : 'client';
    api.status = doc.querySelector('.tier-card.ok') != null
        ? 'active'
        : 'pending';
    final rate = await json('GET', '/panel/data/rate');
    final rateText = (rate['rate'] ?? rate['tjs_rate'] ?? '').toString();
    if (rateText.isEmpty ||
        Decimal.tryParse(rateText) == null ||
        Decimal.parse(rateText) <= Decimal.zero) {
      throw const ApiFailure('Сайт не передал действующий курс сомони.');
    }
    api.tjsRate = rateText;
    void sameAccount() {
      api.requireAccount(id);
      if (epoch != api._sessionEpoch) {
        throw const ApiFailure('Аккаунт изменился.', 401);
      }
    }

    final storedKey = await api.storage.read(key: 'donatix_rest_key_$id');
    sameAccount();
    api.personalApiKey = storedKey;
    if (api.status == 'active') {
      if (api.personalApiKey != null) {
        try {
          final me = await json('GET', '/api/v1/me');
          if (me['login'] != api.login) api.personalApiKey = null;
        } on ApiFailure catch (e) {
          sameAccount();
          if (e.status != 401 && e.status != 403) rethrow;
          api.personalApiKey = null;
        }
      }
      if (api.personalApiKey == null) {
        final key = await _personalKey();
        sameAccount();
        api.personalApiKey = key;
      }
      final me = await json('GET', '/api/v1/me');
      if (me['login'] != api.login) {
        api.personalApiKey = null;
        throw const ApiFailure('API-ключ принадлежит другому аккаунту.', 403);
      }
      await api.storage.write(
        key: 'donatix_rest_key_$id',
        value: api.personalApiKey!,
      );
      sameAccount();
      api.personalKeyVerified = true;
    } else {
      api.personalApiKey = null;
    }
    await api.onSessionChanged?.call();
    return {
      'ok': true,
      'csrf': api.csrf,
      'login': api.login,
      'user_id': api.userId,
      'tjs_rate': api.tjsRate,
      'role': api.role,
      'status': api.status,
    };
  }

  Future<String> _personalKey() async {
    final owner = api.userId;
    final doc = await page('/panel/api');
    final buttons = doc
        .querySelectorAll('button[data-show]')
        .where(
          (b) =>
              text(ancestor(b, 'tr')?.querySelector('td')) == 'Donatix Android',
        )
        .toList();
    buttons.sort((a, b) {
      final aName = text(ancestor(a, 'tr')?.querySelector('td'));
      final bName = text(ancestor(b, 'tr')?.querySelector('td'));
      return (aName == 'Donatix Android' ? 0 : 1).compareTo(
        bName == 'Donatix Android' ? 0 : 1,
      );
    });
    for (final button in buttons.take(2)) {
      final id = button.attributes['data-show'];
      if (id == null || int.tryParse(id) == null) continue;
      try {
        final key = await _reveal(id);
        api.requireAccount(owner);
        return key;
      } on ApiFailure catch (e) {
        if (e.status != 404) rethrow;
      }
    }
    // Normal account action through the already-existing site form. Never use
    // the owner's/admin's key as an application-wide credential.
    final created = await form('/panel/api/keys', {
      'name': 'Donatix Android',
    }, back: '/panel/api');
    api.requireAccount(owner);
    final key = text(created.querySelector('#new-key'));
    if (key.isEmpty) {
      throw const ApiFailure('Не удалось получить личный API-ключ аккаунта.');
    }
    return key;
  }

  Future<String> _reveal(String id) async {
    final owner = api.userId, epoch = api._sessionEpoch;
    final r = await api.form('/panel/api/keys/' + id + '/reveal', {
      'csrf': api.csrf,
    });
    if (epoch != api._sessionEpoch || api.userId != owner) {
      throw const ApiFailure('Аккаунт изменился.', 401);
    }
    dynamic d = r.data;
    if (d is String) {
      try {
        d = jsonDecode(d);
      } catch (_) {
        d = null;
      }
    }
    if (d is! Map || d['ok'] != true || d['key'] is! String) {
      throw ApiFailure(
        d is Map
            ? (d['error'] ?? 'Ключ недоступен.').toString()
            : 'Не удалось получить ключ.',
        r.statusCode,
      );
    }
    return d['key'] as String;
  }

  Future<Map<String, dynamic>?> request(
    String method,
    String path, {
    dynamic data,
    Map<String, dynamic>? query,
    String? idempotency,
  }) async {
    final body = data is Map
        ? Map<String, dynamic>.from(data)
        : <String, dynamic>{};
    final isGet = method == 'GET';
    if (path == '/api/v1/mobile/config') return configuration();
    if (path == '/api/v1/mobile-session') return bootstrap();
    if (api.androidExtension && path.startsWith('/api/v1/android/')) {
      return json(method, path, data: data, query: query);
    }
    if (path == '/api/v1/mobile/categories') return categories(query ?? {});
    if (path.startsWith('/api/v1/mobile/public/')) {
      path = path.replaceFirst('/api/v1/mobile/public/', '/api/v1/');
    }
    if (isGet &&
        api.personalApiKey == null &&
        path.startsWith('/api/v1/products')) {
      if (path == '/api/v1/products') return guestProducts(query ?? {});
      return {
        'ok': true,
        'product': await guestProduct(
          Uri.decodeComponent(path.split('/').last),
        ),
      };
    }
    if (isGet &&
        path.startsWith('/api/v1/steam-gifts/games') &&
        api.personalApiKey == null) {
      return json(
        'GET',
        path.replaceFirst('/api/v1/', '/panel/data/'),
        query: query,
      );
    }
    if (path == '/api/v1/accounts/check' && api.personalApiKey == null) {
      final fields = body['fields'] as Map? ?? {};
      return json(
        'GET',
        '/panel/data/check-account/' +
            Uri.encodeComponent(body['product_id'].toString()),
        query: {
          for (final e in fields.entries) 'field_' + e.key.toString(): e.value,
        },
      );
    }
    if (path.contains('/gamekeys/') && path.endsWith('/regions')) {
      final id = path.split('/')[5];
      return json('GET', '/panel/data/gamekey-regions/' + id);
    }
    if (path == '/api/v1/mobile/orders/quote') return quote(body);
    if (path == '/api/v1/mobile/cart/quote') return cartQuote(body);
    if (path == '/api/v1/mobile/cart') return cart(body, idempotency);
    if (isGet && path == '/api/v1/orders') return orders(query ?? {});
    if (isGet && path == '/api/v1/transactions') {
      return transactions(query ?? {});
    }
    if (path == '/api/v1/orders' && !isGet) {
      await guard();
      final expected = body['expected_total_usd']?.toString();
      if (expected != null) {
        final current = await quote(body);
        if (Decimal.parse(expected) !=
            Decimal.parse(current['total_usd'].toString())) {
          throw const ApiFailure(
            'Цена изменилась. Проверьте стоимость и подтвердите покупку заново.',
            422,
          );
        }
      }
      return json(
        'POST',
        path,
        data: {
          'product_id': body['product_id'],
          'quantity': body['quantity'] ?? 1,
          'fields': body['fields'] ?? <String, String>{},
        },
        idempotency: idempotency,
      );
    }
    if (path == '/api/v1/me' && api.personalApiKey == null) {
      final doc = await page('/panel');
      if (doc.querySelector('.tier-card.ok') != null) {
        await bootstrap();
        return json('GET', '/api/v1/me');
      }
      return profileMe(doc);
    }
    if (isGet && path == '/api/v1/balance' && api.personalApiKey == null) {
      final me = profileMe(await page('/panel'));
      return {'ok': true, 'balance': me['balance'], 'currency': 'USD'};
    }
    if (path == '/api/v1/mobile/home') return home();
    if (path == '/api/v1/mobile/timezone') return timezone(isGet, body);
    if (path == '/api/v1/mobile/keys') return keys(isGet, body);
    if (path.startsWith('/api/v1/mobile/keys/')) return keyAction(path, body);
    if (path == '/api/v1/mobile/webhook') {
      await form('/panel/api/webhook', {
        'webhook_url': (body['url'] ?? '').toString(),
        if (body['rotate'] == true) 'rotate': '1',
      }, back: '/panel/api');
      return {'ok': true};
    }
    if (path == '/api/v1/mobile/notifications') {
      return notifications(preview: query?['preview'] == true);
    }
    if (path == '/api/v1/mobile/notifications/read') {
      await page('/panel/notifications');
      return {'ok': true};
    }
    if (path == '/api/v1/mobile/logins') return logins();
    if (path == '/api/v1/mobile/referrals') return referrals();
    if (path == '/api/v1/mobile/dcoin/chart') {
      return json('GET', '/panel/data/dcoin', query: query);
    }
    if (path == '/api/v1/mobile/dcoin') return dcoin();
    if (path == '/api/v1/mobile/dcoin/exchange') return exchange(body);
    if (path == '/api/v1/mobile/stats') return stats(query ?? {});
    if (path == '/api/v1/mobile/bots') return bots(isGet, body);
    if (path.startsWith('/api/v1/mobile/bots/')) {
      final suffix = path.substring('/api/v1/mobile/bots/'.length);
      if (!RegExp(
        r'^\d+/(start|stop|restart|admins|delete)$',
      ).hasMatch(suffix)) {
        throw const ApiFailure('Неизвестное действие.');
      }
      await form('/panel/bots/' + suffix, {
        'admin_ids': (body['admin_ids'] ?? '').toString(),
      }, back: '/panel/bots');
      return {'ok': true};
    }
    if (path == '/api/v1/mobile/support/link') return support();
    if (path == '/api/v1/mobile/support/code') {
      await form('/panel/support/code', {}, back: '/panel/support');
      return {'ok': true};
    }
    if (path.startsWith('/api/v1/mobile/payments/')) {
      final suffix = path.substring('/api/v1/mobile/payments/'.length);
      if (!RegExp(r'^\d+/(boost|cancel)$').hasMatch(suffix)) {
        throw const ApiFailure('Неизвестное действие.');
      }
      await form('/panel/balance/' + suffix, {}, back: '/panel/balance');
      return {'ok': true};
    }
    if (path == '/api/v1/mobile/admin/pricelist') return pricelist();
    if (path.startsWith('/api/v1/mobile/push/')) {
      if (path.endsWith('/unregister')) {
        return {'ok': true, 'configured': false};
      }
      throw const ApiFailure(
        'Мгновенные push требуют серверной поддержки.',
        501,
      );
    }
    if (path.startsWith('/api/v1/mobile/account/')) {
      throw const ApiFailure(
        'На сервере пока нет удаления аккаунта. Обратитесь в поддержку.',
        501,
      );
    }
    if (path.startsWith('/api/v1/mobile/')) {
      throw const ApiFailure(
        'Эта функция требует отдельной серверной интеграции.',
        501,
      );
    }
    if (api.personalApiKey == null && path.startsWith('/api/v1/')) {
      await guard();
    }
    if (!isGet && path.startsWith('/api/v1/')) await guard();
    return null;
  }

  Future<void> guard() async {
    final owner = api.userId;
    if (owner <= 0) throw const ApiFailure('Войдите в аккаунт.', 401);
    final doc = await page('/panel');
    api.requireAccount(owner);
    if (api.session == null ||
        sessionPayload(api.session!)['user_id'] != owner ||
        doc.querySelector('form[action="/logout"]') == null) {
      throw const ApiFailure('Сессия истекла. Войдите снова.', 401);
    }
    api.status = doc.querySelector('.tier-card.ok') != null
        ? 'active'
        : 'pending';
    if (api.status != 'active') {
      throw const ApiFailure('Аккаунт ждёт активации администратором.', 403);
    }
    if (api.personalApiKey == null) {
      await bootstrap();
    }
  }

  static String totalPrice(
    String price,
    String quantity, {
    String divisor = '1',
  }) {
    ({BigInt n, BigInt d}) fraction(String s) {
      final m = RegExp(r'^(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$').firstMatch(s);
      if (m == null) {
        throw const ApiFailure('Сервер передал некорректную стоимость.');
      }
      final decimals = m.group(2) ?? '';
      final exponent = int.tryParse(m.group(3) ?? '0') ?? 0;
      if (exponent.abs() > 30 || decimals.length > 30) {
        throw const ApiFailure('Сервер передал некорректную стоимость.');
      }
      var n = BigInt.parse(m.group(1)! + decimals), d = BigInt.one;
      final scale = decimals.length - exponent;
      if (scale >= 0) {
        d = BigInt.from(10).pow(scale);
      } else {
        n *= BigInt.from(10).pow(-scale);
      }
      return (n: n, d: d);
    }

    final p = fraction(price), q = fraction(quantity), div = fraction(divisor);
    final numerator = p.n * q.n * div.d * BigInt.from(10000);
    final denominator = p.d * q.d * div.n;
    if (denominator <= BigInt.zero) {
      throw const ApiFailure('Некорректный курс.');
    }
    final micro = (numerator + denominator - BigInt.one) ~/ denominator;
    return (micro ~/ BigInt.from(10000)).toString() +
        '.' +
        (micro % BigInt.from(10000)).toString().padLeft(4, '0');
  }

  Future<Map<String, dynamic>> product(String id) async =>
      api.personalApiKey == null
      ? guestProduct(id)
      : Map<String, dynamic>.from(
          (await json(
                'GET',
                '/api/v1/products/' + Uri.encodeComponent(id),
              ))['product']
              as Map,
        );

  Future<Map<String, dynamic>> quote(Map<String, dynamic> body) async {
    if (api.personalApiKey == null) {
      await guard();
    }
    final owner = api.userId;
    final p = await product(body['product_id'].toString());
    api.requireAccount(owner);
    final fields = Map<String, dynamic>.from(body['fields'] as Map? ?? {});
    final quantity = int.tryParse((body['quantity'] ?? 1).toString());
    final min = integer(p['min_quantity'].toString());
    final max = integer(p['max_quantity'].toString());
    if (quantity == null || quantity < min || quantity > max) {
      throw const ApiFailure('Проверьте количество.', 422);
    }
    for (final f in p['fields'] as List? ?? []) {
      if ((fields[f['key']] ?? '').toString().trim().isEmpty) {
        throw ApiFailure(
          'Заполните поле «' + f['label'].toString() + '».',
          422,
        );
      }
    }
    var amount = quantity.toString(),
        divisor = '1',
        price = p['price_usd'].toString();
    if (p['kind'] == 'steam_topup') {
      amount = (fields['amount'] ?? '').toString().replaceAll(',', '.');
      divisor = (p['rates']?[fields['currency']] ?? '').toString();
      if (divisor.isEmpty) {
        throw const ApiFailure('Выберите валюту Steam.', 422);
      }
    } else if (p['kind'] == 'steam_gift') {
      final game = await json(
        'GET',
        '/api/v1/steam-gifts/games/' +
            Uri.encodeComponent(fields['app_id'].toString()),
      );
      String? resolved;
      for (final edition in game['offers'] as List? ?? []) {
        if (edition['sub_id'].toString() != fields['sub_id'].toString()) {
          continue;
        }
        for (final region in edition['regions'] as List? ?? []) {
          if (region['region'].toString() == fields['region'].toString()) {
            resolved = region['price_usd'].toString();
          }
        }
      }
      if (resolved == null) {
        throw const ApiFailure('Предложение Steam недоступно.', 422);
      }
      price = resolved;
    }
    final balance = await json('GET', '/api/v1/balance');
    api.requireAccount(owner);
    return {
      'ok': true,
      'total_usd': totalPrice(price, amount, divisor: divisor),
      'balance_usd': balance['balance'],
      'price_locked': false,
    };
  }

  Future<Map<String, dynamic>> cartQuote(Map<String, dynamic> body) async {
    final input = body['items'] as List? ?? [];
    final totalCount = input.fold<int>(
      0,
      (sum, i) => sum + integer(i['count'].toString()),
    );
    if (input.isEmpty ||
        totalCount > 20 ||
        totalCount < 1 ||
        input.map((i) => i['product_id']).toSet().length != input.length) {
      throw const ApiFailure(
        'За один раз — не больше 20 пакетов одной игры.',
        422,
      );
    }
    final result = <Map<String, dynamic>>[];
    Decimal total = Decimal.zero;
    String? signature;
    for (final item in input) {
      final p = await product(item['product_id'].toString());
      if (p['kind'] != 'topup' || integer(p['max_quantity'].toString()) != 1) {
        throw const ApiFailure('Пакет недоступен для корзины.', 422);
      }
      final current =
          p['category_id'].toString() + '|' + (p['region'] ?? '').toString();
      if (signature != null && current != signature) {
        throw const ApiFailure('Выберите пакеты одной игры и региона.', 422);
      }
      signature = current;
      final q = await quote({
        'product_id': item['product_id'],
        'quantity': 1,
        'fields': body['fields'],
      });
      final count = integer(item['count'].toString());
      if (count < 1) {
        throw const ApiFailure('Проверьте количество пакетов.', 422);
      }
      result.add({
        'product_id': item['product_id'],
        'count': count,
        'expected_total_usd': q['total_usd'],
      });
      total +=
          Decimal.parse(q['total_usd'].toString()) * Decimal.fromInt(count);
    }
    final balance = await json('GET', '/api/v1/balance');
    return {
      'ok': true,
      'items': result,
      'total_usd': total.toStringAsFixed(4),
      'balance_usd': balance['balance'],
    };
  }

  Future<Map<String, dynamic>> cart(
    Map<String, dynamic> body,
    String? key,
  ) async {
    if (key == null || key.length < 16) {
      throw const ApiFailure('Не задан ключ операции.', 422);
    }
    final owner = api.userId;
    final input = body['items'] as List? ?? [];
    final requested = input.fold<int>(
      0,
      (sum, i) => sum + integer(i['count'].toString()),
    );
    if (requested < 1 || requested > 20) {
      throw const ApiFailure('Проверьте корзину.', 422);
    }
    await guard();
    final made = <Map<String, dynamic>>[];
    var index = 0;
    for (final item in input) {
      for (var n = 0; n < integer(item['count'].toString()); n++) {
        api.requireAccount(owner);
        try {
          final quoted = await quote({
            'product_id': item['product_id'],
            'quantity': 1,
            'fields': body['fields'] ?? {},
          });
          if (Decimal.tryParse(item['expected_total_usd'].toString()) !=
              Decimal.parse(quoted['total_usd'].toString())) {
            throw const ApiFailure(
              'Цена пакета изменилась. Проверьте историю и новую стоимость.',
              422,
            );
          }
          final result = await json(
            'POST',
            '/api/v1/orders',
            data: {
              'product_id': item['product_id'],
              'quantity': 1,
              'fields': body['fields'] ?? {},
            },
            idempotency: 'mcart-' + key + '-' + index.toString(),
          );
          made.add(Map<String, dynamic>.from(result['order'] as Map));
          index++;
        } on ApiFailure catch (e) {
          if (e.status == null ||
              e.status! >= 500 ||
              [401, 408, 409, 429].contains(e.status)) {
            rethrow;
          }
          return {
            'ok': true,
            'items': made,
            'requested': requested,
            'partial': true,
            'error': e.message,
          };
        }
      }
    }
    api.requireAccount(owner);
    return {
      'ok': true,
      'items': made,
      'requested': requested,
      'partial': false,
    };
  }

  Future<Map<String, dynamic>> categories(Map<String, dynamic> query) async {
    final kind = (query['kind'] ?? '').toString();
    // The site's unfiltered page is a capped list of packages, not games.
    // Read the complete category pages instead so later sections aren't lost
    // behind the first 500 game keys.
    final sections = kind.isEmpty
        ? [
            'topup',
            'telegram',
            'steam_topup',
            'steam_gift',
            'gift_card',
            'game_key',
          ]
        : [kind.startsWith('telegram_') ? 'telegram' : kind];
    final lists = await Future.wait(
      sections.map((section) async {
        final doc = await page(
          '/panel/catalog',
          query: {...query, 'kind': section, 'q': ''},
          authenticated: false,
        );
        if (section == 'telegram') {
          return [
            for (final service in ['telegram_stars', 'telegram_premium'])
              if (kind.isEmpty || kind == service || kind == 'telegram')
                if (telegramProducts(doc, service).isNotEmpty)
                  {
                    'category_id': service,
                    'category_name': service == 'telegram_stars'
                        ? 'Telegram Stars — звёзды'
                        : 'Telegram Premium',
                    'kind': service,
                    'image_url': null,
                    'from_price': null,
                    'price_note': service == 'telegram_stars'
                        ? 'Выберите количество звёзд'
                        : 'Выберите срок подписки',
                    'regions': <String>[],
                  },
          ];
        }
        if (section == 'steam_topup' || section == 'steam_gift') {
          return [
            for (final card in doc.querySelectorAll('.pack-card'))
              {
                'category_id': Uri.parse(
                  card.attributes['href'] ?? '',
                ).pathSegments.last,
                'category_name': text(card.querySelector('.pack-name')),
                'kind': section,
                'image_url': card.querySelector('img')?.attributes['src'],
                'from_price': null,
                'price_note': 'Рассчитать стоимость',
                'regions': <String>[],
              },
          ];
        }
        return categoryCards(doc);
      }),
    );
    final search = (query['q'] ?? '').toString().trim().toLowerCase();
    final seen = <String>{};
    return {
      'ok': true,
      'items': [
        for (final item in lists.expand((items) => items))
          if ((search.isEmpty ||
                  '${item['category_name']} ${item['category_source_name'] ?? ''} ${item['kind']}'
                      .toLowerCase()
                      .contains(search)) &&
              seen.add('${item['kind']}|${item['category_id']}'))
            item,
      ],
    };
  }

  List<Map<String, dynamic>> categoryCards(dom.Document doc) => [
    for (final card in doc.querySelectorAll('.game-card'))
      {
        'category_id':
            Uri.parse(
              card.attributes['href'] ?? '',
            ).queryParameters['category'] ??
            '',
        'category_name': catalogDisplayName(
          text(card.querySelector('.game-name')),
        ),
        'category_source_name': text(card.querySelector('.game-name')),
        'kind':
            Uri.parse(card.attributes['href'] ?? '').queryParameters['kind'] ??
            '',
        'image_url': card.querySelector('img')?.attributes['src'],
        'from_price': usd(card.querySelector('.game-meta')),
        'region_label': card.querySelectorAll('.game-meta span').length > 1
            ? text(card.querySelectorAll('.game-meta span').last)
            : '',
        'regions': <String>[],
      },
  ];

  String popularTitle(dom.Element link) {
    // media() contains a nested span with initials such as FF/PM. The title
    // is the last direct span child of the link, not span:last-child below it.
    final title = text(
      link.children.where((e) => e.localName == 'span').lastOrNull,
    );
    return title.isNotEmpty
        ? title
        : link.querySelector('img')?.attributes['alt'] ?? '';
  }

  Future<Map<String, dynamic>> guestProducts(Map<String, dynamic> query) async {
    final doc = await page(
      '/panel/catalog',
      query: {
        ...query,
        if (query['category_id'] != null) 'category': query['category_id'],
      },
      authenticated: false,
    );
    final kind = (query['kind'] ?? '').toString();
    if (kind.startsWith('telegram_')) {
      return {'ok': true, 'items': telegramProducts(doc, kind)};
    }
    return {
      'ok': true,
      'items': [
        for (final card in doc.querySelectorAll('.pack-card'))
          {
            'product_id': Uri.parse(
              card.attributes['href'] ?? '',
            ).pathSegments.last,
            'kind': kind,
            'name': text(card.querySelector('.pack-name')),
            'title': text(card.querySelector('.pack-name')),
            'price_usd': usd(card.querySelector('.pack-price')),
            'category_id': query['category_id'] ?? '',
            'category_name': text(doc.querySelector('.game-head h1')),
            'region_title': text(card.querySelector('.badge')),
            'region': '',
            'fields': <Map<String, dynamic>>[],
            'min_quantity': 1,
            'max_quantity': 1,
            'unit': text(card.querySelector('.pack-price')).contains('звезда')
                ? 'star'
                : 'item',
            'image_url': doc.querySelector('.game-head img')?.attributes['src'],
          },
      ],
    };
  }

  List<Map<String, dynamic>> telegramProducts(dom.Document doc, String kind) {
    final fields = [
      {
        'key': 'telegram_username',
        'label': 'Telegram username',
        'options': <String>[],
      },
    ];
    if (kind == 'telegram_premium') {
      return [
        for (final plan in doc.querySelectorAll('.tg-plan'))
          {
            'product_id': plan
                .querySelector('[name="product_id"]')
                ?.attributes['value'],
            'kind': kind,
            'name': text(plan.querySelector('.tp-name')),
            'title': text(plan.querySelector('.tp-name')),
            'unit': 'item',
            'price_usd': plan
                .querySelector('[data-price]')
                ?.attributes['data-price'],
            'min_quantity': 1,
            'max_quantity': 1,
            'fields': fields,
          },
      ];
    }
    final stars = doc.querySelector('.tab-pane[data-pane="stars"]');
    final id = stars?.querySelector('[name="product_id"]')?.attributes['value'];
    if (id == null) return [];
    final source = doc.querySelectorAll('script').map((s) => s.text).join('\n');
    final price = RegExp(
      r'const starPrice\s*=\s*"([^"\n]+)";',
    ).firstMatch(source)?.group(1);
    if (price == null || Decimal.tryParse(price) == null) {
      throw const ApiFailure('Сайт не передал точную цену звёзд.');
    }
    final quantity = stars?.querySelector('[name="quantity"]');
    return [
      {
        'product_id': id,
        'kind': 'telegram_stars',
        'name': 'Telegram Stars',
        'title': 'Telegram Stars',
        'unit': 'star',
        'price_usd': price,
        'min_quantity': integer(quantity?.attributes['min'] ?? ''),
        'max_quantity': integer(quantity?.attributes['max'] ?? ''),
        'fields': fields,
      },
    ];
  }

  Future<Map<String, dynamic>> guestProduct(String id) async {
    final doc = await page(
      '/panel/buy/' + Uri.encodeComponent(id),
      authenticated: false,
    );
    final fields = <Map<String, dynamic>>[];
    final form = doc.querySelector('form#buy');
    if (form == null) throw const ApiFailure('Товар недоступен.', 404);
    for (final field in form.querySelectorAll('[name^="field_"]')) {
      final key = field.attributes['name']!.substring(6);
      if (fields.any((f) => f['key'] == key)) continue;
      final label = doc.querySelector('label[for="' + (field.id) + '"]');
      fields.add({
        'key': key,
        'label': text(label).isEmpty ? key : text(label),
        'options': field.localName == 'select'
            ? field
                  .querySelectorAll('option')
                  .where((o) => (o.attributes['value'] ?? o.text).isNotEmpty)
                  .map((o) => o.attributes['value'] ?? o.text)
                  .toList()
            : <String>[],
      });
    }
    final source = doc.querySelectorAll('script').map((s) => s.text).join('\n');
    final price = RegExp(
      r'(?:const|var)\s+(?:unit|perUsd)\s*=\s*parseFloat\("([^"]+)"\)',
    ).firstMatch(source)?.group(1);
    if (price == null) {
      throw const ApiFailure('Не удалось получить точную цену товара.');
    }
    final quantity = form.querySelector('[name="quantity"]');
    final kindLink = doc
        .querySelector('a.back[href*="kind="]')
        ?.attributes['href'];
    final kind = id == 'steam-topup'
        ? 'steam_topup'
        : id == 'steam-gift'
        ? 'steam_gift'
        : Uri.parse(kindLink ?? '').queryParameters['kind'] ?? 'topup';
    final title = text(
      doc.querySelector('#s-item, .buy-head h1, .page-hero h1'),
    );
    final categoryLink = doc.querySelector('.pp.on')?.attributes['href'];
    Map<String, dynamic> rates = {};
    final rateMatch = RegExp(
      r'const rates\s*=\s*(\{[^;]+\});',
    ).firstMatch(source);
    if (rateMatch != null) {
      rates = Map<String, dynamic>.from(jsonDecode(rateMatch.group(1)!) as Map);
    }
    return {
      'product_id': id,
      'kind': kind,
      'name': title,
      'title': title,
      'category_name': text(doc.querySelector('.buy-head h1')),
      'category_id':
          Uri.parse(categoryLink ?? '').queryParameters['category'] ?? '',
      'price_usd': price,
      'unit': (doc.body?.text ?? '').contains('Количество звёзд')
          ? 'star'
          : 'item',
      'min_quantity': integer(
        quantity?.attributes['min'] ?? quantity?.attributes['value'] ?? '1',
      ),
      'max_quantity': integer(quantity?.attributes['max'] ?? '1'),
      'fields': fields,
      'image_url': doc.querySelector('.buy-head img')?.attributes['src'],
      'region': '',
      'region_title': text(doc.querySelector('.bh-tags .badge')),
      'account_check': doc.querySelector('#acc-btn') != null,
      'stock': null,
      'rates': rates,
      'min_usd': RegExp(
        r'minUsd\s*=\s*parseFloat\("([^"]+)"\)',
      ).firstMatch(source)?.group(1),
      'max_usd': RegExp(
        r'maxUsd\s*=\s*parseFloat\("([^"]+)"\)',
      ).firstMatch(source)?.group(1),
      'discount_percent': number(text(doc.querySelector('.summary .ok-text'))),
    };
  }

  static String value(dom.Document doc, String label) {
    for (final row in doc.querySelectorAll('.kv')) {
      if (text(row.querySelector('.k')) == label) {
        return text(row.querySelector('.v'));
      }
    }
    return '';
  }

  Map<String, dynamic> profileMe(dom.Document doc) {
    final tiles = doc.querySelectorAll('.bal-tile');
    if (tiles.length < 2 || doc.querySelector('.who b') == null) {
      throw const ApiFailure('Не удалось получить данные профиля.');
    }
    return {
      'ok': true,
      'login': text(doc.querySelector('.who b')),
      'email': text(doc.querySelector('.who .muted')),
      'status': api.status,
      'tier': text(doc.querySelector('.tier-name')).toLowerCase(),
      'balance': usd(doc.querySelector('.bal-amount')),
      'currency': 'USD',
      'summary': {
        'totalSpent': usd(tiles.first.querySelector('.t-value')),
        'totalOrders': integer(text(tiles[1].querySelector('.t-value'))),
      },
      'createdAt': value(doc, 'Регистрация'),
      'project': value(doc, 'Проект'),
    };
  }

  Future<Map<String, dynamic>> home() async {
    final doc = await page('/panel');
    final markup = RegExp(
      r'Наценка[^:]*:\s*([\d.,]+)%',
    ).firstMatch(doc.body?.text ?? '');
    final all = doc.querySelectorAll('.bal-tile .t-note').lastOrNull;
    final coin = doc.querySelector('.dc-card');
    Map<String, dynamic>? dc;
    if (coin != null) {
      final small = coin.querySelector('small');
      final prices = RegExp(r'\$([0-9.]+)').allMatches(text(small)).toList();
      if (prices.length >= 2) {
        dc = {
          'balance_text': number(text(coin.querySelector('.grow b'))),
          'worth_usd': prices[0].group(1)!,
          'price': double.parse(prices[1].group(1)!),
          'change':
              double.tryParse(number(text(coin.querySelector('.dc-chg')))) ?? 0,
        };
      }
    }
    return {
      'ok': true,
      'orders_all': integer(text(all)),
      'low_usd': '0',
      'low_balance': doc.querySelector('.low-bal') != null,
      'markup': markup?.group(1)?.replaceAll(',', '.') ?? '',
      'referral_enabled': doc.querySelector('.ref-promo') != null,
      'referral_percent': null,
      'dcoin': dc,
      'dcoin_enabled': dc != null,
      'support_contact':
          doc
              .querySelector('.quick a[href^="https://t.me/"]')
              ?.attributes['href'] ??
          '',
      'tg_channel': doc.querySelector('.side-tg')?.attributes['href'] ?? '',
      'popular': [
        for (final link in doc.querySelectorAll('.pop-quick .pop-tile'))
          {
            'title': popularTitle(link),
            'href': link.attributes['href'] ?? '',
            'image_url': link.querySelector('img')?.attributes['src'],
            'kind':
                Uri.parse(
                  link.attributes['href'] ?? '',
                ).queryParameters['kind'] ??
                '',
          },
      ],
    };
  }

  Future<Map<String, dynamic>> timezone(
    bool isGet,
    Map<String, dynamic> body,
  ) async {
    if (!isGet) {
      await form('/panel/timezone', {
        'tz': (body['timezone'] ?? 'auto').toString(),
      }, back: '/panel');
      return {'ok': true};
    }
    final doc = await page('/panel');
    final select = doc.querySelector('select[name="tz"]');
    if (select == null) throw const ApiFailure('Часовые пояса недоступны.');
    final options = select.querySelectorAll('option');
    final chosen = options
        .where((o) => o.attributes.containsKey('selected'))
        .firstOrNull;
    return {
      'ok': true,
      'choice': chosen?.attributes['value'] ?? 'auto',
      'zones': [
        for (final option in options)
          if (option.attributes['value'] != 'auto')
            [option.attributes['value'], text(option)],
      ],
    };
  }

  Future<Map<String, dynamic>> keys(
    bool isGet,
    Map<String, dynamic> body,
  ) async {
    dom.Document doc;
    if (!isGet) {
      doc = await form('/panel/api/keys', {
        'name': (body['name'] ?? '').toString(),
      }, back: '/panel/api');
      final key = text(doc.querySelector('#new-key'));
      if (key.isEmpty) throw const ApiFailure('Сайт не передал новый ключ.');
      return {'ok': true, 'key': key};
    }
    doc = await page('/panel/api');
    final items = <Map<String, dynamic>>[];
    for (final row in doc.querySelectorAll('table tr')) {
      final cells = row.querySelectorAll('td');
      if (cells.length < 5) continue;
      final val = row.querySelector('.key-val');
      if (val == null) continue;
      final id = int.tryParse(val.id.replaceFirst('kv-', ''));
      if (id == null) continue;
      items.add({
        'id': id,
        'name': text(cells[0]),
        'prefix': text(val).replaceAll('…', ''),
        'created_at': text(cells[2]),
        'last_used_at': text(cells[3]) == 'ни разу' ? null : text(cells[3]),
        'revoked_at': row.querySelector('[data-show]') == null
            ? 'отозван'
            : null,
        'can_show': row.querySelector('[data-show]') != null,
      });
    }
    final hook = doc.querySelector('form[action="/panel/api/webhook"]');
    return {
      'ok': true,
      'items': items,
      'webhook_url':
          hook?.querySelector('[name="webhook_url"]')?.attributes['value'] ??
          '',
      'webhook_secret': text(hook?.querySelector('pre')),
      'base_url': DonatixApi.origin,
      'active': api.status == 'active',
    };
  }

  Future<Map<String, dynamic>> keyAction(
    String path,
    Map<String, dynamic> body,
  ) async {
    final parts = path.split('/'), id = parts[5], action = parts[6];
    if (int.tryParse(id) == null) throw const ApiFailure('Неизвестный ключ.');
    await page('/panel/api');
    if (action == 'revoke') {
      final result = await form(
        '/panel/api/keys/' + id + '/revoke',
        {},
        back: '/panel/api',
      );
      if (result.querySelector('[data-show="' + id + '"]') != null) {
        throw const ApiFailure('Сайт не подтвердил отзыв ключа.');
      }
      return {'ok': true};
    }
    final key = await _reveal(id);
    if (action == 'test') {
      final tested = await json('GET', '/api/v1/me', keyOverride: key);
      return {'ok': true, 'valid': tested['login'] == api.login};
    }
    if (action != 'reveal') throw const ApiFailure('Неизвестное действие.');
    return {'ok': true, 'key': key};
  }

  Future<Map<String, dynamic>> notifications({bool preview = false}) async {
    final doc = await page(preview ? '/panel' : '/panel/notifications');
    if (preview) {
      final bell = doc.querySelector('.bell');
      final badge = bell?.attributes['aria-label']?.contains(':') == true
          ? bell!.attributes['aria-label']!.split(':').last
          : text(bell?.querySelector('.dot-count'));
      return {
        'ok': true,
        'unread': integer(badge),
        'items': <Map<String, dynamic>>[],
        'total': 0,
      };
    }
    final items = <Map<String, dynamic>>[];
    for (final row in doc.querySelectorAll('#main > .list .item')) {
      final content = text(row.querySelector('.title'));
      final code = text(row.querySelector('.big-code'));
      final details = row.querySelectorAll('.sub');
      final date = details.isEmpty
          ? ''
          : text(details.last).replaceAll(' · новое', '');
      final message = code.isEmpty
          ? content
          : content +
                '\n' +
                code +
                '\n' +
                (details.isEmpty ? '' : text(details.first));
      items.add({
        'id': base64Url.encode(utf8.encode(message + date)),
        'title': '',
        'text': message,
        'created_at': date,
        'read_at': row.querySelector('.tile.g1') == null ? date : null,
        'link': row.attributes['href'] == '#'
            ? ''
            : row.attributes['href'] ?? '',
      });
    }
    return {
      'ok': true,
      'items': items,
      'total': items.length,
      'unread': items.where((n) => n['read_at'] == null).length,
    };
  }

  Future<Map<String, dynamic>> orders(Map<String, dynamic> query) async {
    final doc = await page(
      '/panel/orders',
      query: {
        for (final key in [
          'page',
          'status',
          'q',
          'period',
          'date_from',
          'date_to',
        ])
          if (query[key] != null) key: query[key],
      },
    );
    final heading = text(doc.querySelector('.page-hero p'));
    final match = RegExp(
      r'^(.*):\s*(\d+)\s*·\s*выполнено\s+(\d+)\s+на',
    ).firstMatch(heading);
    if (match == null) {
      throw const ApiFailure('Не удалось прочитать историю заказов.');
    }
    final failed = RegExp(r'возвращено\s+(\d+)').firstMatch(heading);
    final items = <Map<String, dynamic>>[];
    for (final row in doc.querySelectorAll('#main .order-card')) {
      final status = row.querySelector('.status');
      final state = status?.classes.where((c) => c != 'status').firstOrNull;
      if (state == null) {
        throw const ApiFailure('Сайт не передал статус заказа.');
      }
      items.add({
        'order_id': text(row.querySelector('.title')),
        'product_name': text(row.querySelector('.oc-line')),
        'status': state,
        'total_usd': usd(row.querySelector('.amount')),
        'created_at': text(row.querySelector('.meta .pill')),
        'image_url': row.querySelector('img')?.attributes['src'],
      });
    }
    return {
      'ok': true,
      'items': items,
      'total': int.parse(match.group(2)!),
      'page': query['page'] ?? 1,
      'limit': 30,
      'period': match.group(1),
      'totals': {
        'done': int.parse(match.group(3)!),
        'spent': usd(doc.querySelector('.page-hero p')),
        'failed': failed == null ? 0 : int.parse(failed.group(1)!),
      },
    };
  }

  Future<Map<String, dynamic>> transactions(Map<String, dynamic> query) async {
    final doc = await page(
      '/panel/transactions',
      query: {'page': query['page'] ?? 1, 'type': query['type'] ?? ''},
    );
    final items = <Map<String, dynamic>>[];
    for (final row in doc.querySelectorAll('#main .list .item')) {
      final balances = row.querySelectorAll('.tx-bal b');
      if (balances.length != 2) {
        throw const ApiFailure('Не удалось прочитать баланс операции.');
      }
      final amount = row.querySelector('.amount');
      final date = row.querySelectorAll('.meta .pill').lastOrNull;
      final order = row.querySelector('.sub a')?.attributes['href'];
      items.add({
        'transactionId': text(row.querySelector('.title')),
        'amount':
            (amount?.classes.contains('minus') == true ? '-' : '') +
            usd(amount),
        'balanceBefore': usd(balances[0]),
        'balanceAfter': usd(balances[1]),
        'note': text(row.querySelector('.sub')),
        'createdAt': text(date),
        'type': amount?.classes.contains('plus') == true ? 'credit' : 'debit',
        'orderId': order == null ? null : Uri.parse(order).pathSegments.last,
      });
    }
    final pager = text(doc.querySelector('.pager span')).split('/');
    final pages = pager.length == 2 ? integer(pager.last) : 1;
    final current = integer((query['page'] ?? 1).toString());
    return {
      'ok': true,
      'items': items,
      'limit': 40,
      'has_next': current < pages,
      'total': null,
    };
  }

  Future<Map<String, dynamic>> logins() async {
    final doc = await page('/panel/logins');
    final rows = [
      for (final item in doc.querySelectorAll('#main .list .item'))
        {
          'ip': text(item.querySelector('.title')),
          'user_agent': text(item.querySelector('.sub')),
          'created_at': text(item.querySelector('.pill')),
          'session_status_available': false,
          'ended_at': null,
        },
    ];
    return {'ok': true, 'items': rows, 'total': rows.length};
  }

  Future<Map<String, dynamic>> referrals() async {
    final doc = await page('/panel/referrals');
    final link = text(doc.querySelector('#ref-link'));
    final kpis = doc.querySelectorAll('.ref-kpis .kpi');
    final values = kpis.map((k) => k.querySelector('.value')).toList();
    final rewards = <Map<String, dynamic>>[],
        friends = <Map<String, dynamic>>[];
    final lists = doc.querySelectorAll('#main > .list');
    if (lists.isNotEmpty) {
      for (final item in lists.first.querySelectorAll('.item')) {
        final title = text(item.querySelector('.title'));
        final sub = text(item.querySelector('.sub')).split(' · ');
        rewards.add({
          'earned': usd(item.querySelector('.title')),
          'login': title.split(' · ').last,
          'order_id': sub.first.replaceFirst('заказ ', ''),
          'created_at': sub.length > 1 ? sub.last : '',
        });
      }
    }
    if (lists.length > 1) {
      for (final item in lists[1].querySelectorAll('.item')) {
        friends.add({
          'login': text(item.querySelector('.title')),
          'created_at': text(item.querySelector('.sub')).replaceFirst('с ', ''),
        });
      }
    }
    return {
      'ok': true,
      'enabled': link.isNotEmpty,
      'percent': null,
      'link': link,
      'stats': {
        'invited': values.isEmpty ? 0 : integer(text(values[0])),
        'active': kpis.isEmpty
            ? 0
            : integer(text(kpis[0].querySelector('.note'))),
        'earned': values.length < 2 ? '0' : usd(values[1]),
      },
      'invited': values.isEmpty ? 0 : integer(text(values[0])),
      'active': kpis.isEmpty
          ? 0
          : integer(text(kpis[0].querySelector('.note'))),
      'earned': values.length < 2 ? '0' : usd(values[1]),
      'rewards': rewards,
      'friends': friends,
    };
  }

  Future<Map<String, dynamic>> support() async {
    final doc = await page('/panel/support');
    final link = doc.querySelector('.sc-open')?.attributes['href'] ?? '';
    final uri = Uri.tryParse(link);
    return {
      'ok': true,
      'code': text(doc.querySelector('#sc-code')),
      'url': link,
      'bot': uri?.pathSegments.lastOrNull ?? '',
      'minutes': integer(text(doc.querySelector('.sc-step .muted.small'))),
    };
  }

  Future<Map<String, dynamic>> dcoin() async {
    final doc = await page('/panel/dcoin');
    final exchangeForm = doc.querySelector(
      'form[action="/panel/dcoin/exchange"]',
    );
    final balance = number(text(doc.querySelector('#dc-bal')));
    final fee = RegExp(
      r'комиссия\s+([\d.]+)%',
    ).firstMatch(doc.body?.text ?? '')?.group(1);
    final perUsd = RegExp(
      r'\$1\s*=\s*(\d+)',
    ).firstMatch(doc.body?.text ?? '')?.group(1);
    final history = <Map<String, dynamic>>[],
        top = <Map<String, dynamic>>[],
        days = <Map<String, dynamic>>[];
    final lists = doc.querySelectorAll('.dc-lists > .card');
    if (lists.isNotEmpty) {
      for (final row in lists[0].querySelectorAll('.dc-row')) {
        final amount = text(row.querySelector('b'));
        history.add({
          'reason': text(
            row.querySelector('.grow'),
          ).replaceAll(text(row.querySelector('small')), '').trim(),
          'created_at': text(row.querySelector('small')),
          'amount': number(amount),
          'plus': amount.startsWith('+'),
          'wait': row.querySelector('b.wait') != null,
        });
      }
    }
    if (lists.length > 1) {
      for (final row in lists[1].querySelectorAll('.dc-row')) {
        top.add({
          'login': text(row.querySelector('.grow')),
          'coins': number(text(row.querySelector('b'))),
        });
      }
    }
    for (final day in doc.querySelectorAll('.dc-day')) {
      final ranges = RegExp(
        r'\$([\d.]+)',
      ).allMatches(text(day.querySelector('.dc-day-range'))).toList();
      days.add({
        'day': text(day.querySelector('.dc-day-date')),
        'change':
            double.tryParse(number(text(day.querySelector('.dc-chg')))) ?? 0,
        'high': ranges.isEmpty ? 0 : double.parse(ranges[0].group(1)!),
        'low': ranges.length < 2 ? 0 : double.parse(ranges[1].group(1)!),
      });
    }
    final all = exchangeForm
        ?.querySelector('[data-all]')
        ?.attributes['data-all'];
    return {
      'ok': true,
      'summary': {
        'balance_text': balance,
        'waiting_text':
            RegExp(
              r'([\d.]+)\s*D ждут',
            ).firstMatch(text(doc.querySelector('#dc-worth')))?.group(1) ??
            '',
        'worth_usd': usd(doc.querySelector('#dc-worth')),
        'free_text': all ?? balance,
        'free': all == null
            ? null
            : (Decimal.parse(all) * Decimal.fromInt(100)).toBigInt().toInt(),
        'price': double.parse(number(text(doc.querySelector('#dc-price')))),
        'change': double.parse(number(text(doc.querySelector('#dc-chg')))),
        'per_usd': perUsd == null ? null : int.parse(perUsd),
        'fee_pct': fee == null ? null : double.parse(fee),
        'open': exchangeForm != null,
        'enabled': true,
        'opens': text(doc.querySelector('.dc-lock b')),
      },
      'history': history,
      'top': top,
      'days': days,
      'timeframes': [
        for (final button in doc.querySelectorAll('.dc-tfs [data-tf]'))
          button.attributes['data-tf'],
      ],
    };
  }

  Future<Map<String, dynamic>> exchange(Map<String, dynamic> body) async {
    final doc = await form('/panel/dcoin/exchange', {
      'amount': (body['amount'] ?? '').toString(),
    }, back: '/panel/dcoin');
    final message = text(doc.querySelector('.flash:not(.warn):not(.error)'));
    final match = RegExp(
      r'Обменяли\s+([\d.,]+)\s*D.*?\$([\d.,]+)',
    ).firstMatch(message);
    if (match == null) {
      throw const ApiFailure(
        'Обновите баланс и историю перед повторным обменом.',
      );
    }
    return {
      'ok': true,
      'coins': match.group(1),
      'credited_usd': match.group(2),
    };
  }

  Future<Map<String, dynamic>> bots(
    bool isGet,
    Map<String, dynamic> body,
  ) async {
    if (!isGet) {
      await form('/panel/bots', {
        'token': (body['token'] ?? '').toString(),
        'admin_ids': (body['admin_ids'] ?? '').toString(),
      }, back: '/panel/bots');
      return {'ok': true};
    }
    final doc = await page('/panel/bots');
    final lock = doc.querySelector('.bot-lock');
    final progress = doc.querySelector('.lock-progress');
    final items = <Map<String, dynamic>>[];
    for (final card in doc.querySelectorAll('.bot-card')) {
      final action =
          card
              .querySelector(r'form[action$="/delete"]')
              ?.attributes['action'] ??
          '';
      final id = int.tryParse(action.split('/').reversed.skip(1).first);
      if (id == null) continue;
      final warn = text(card.querySelector('.flash.warn'));
      items.add({
        'id': id,
        'username': text(
          card.querySelector('.bot-head .grow b'),
        ).replaceFirst('@', ''),
        'admin_ids':
            card.querySelector('[name="admin_ids"]')?.attributes['value'] ?? '',
        'enabled':
            card.querySelector('.status-dot.on, .status-dot.wait') != null,
        'running': card.querySelector('.status-dot.on') != null,
        'warn_count': integer(warn),
        'disabled_reason': warn.contains('отключён автоматически')
            ? 'inactive'
            : '',
        'conflict': card.querySelector('.flash.error') != null,
      });
    }
    final limit = text(doc.querySelector('.section-title .muted'));
    final max = limit.split('из').last;
    return {
      'ok': true,
      'items': items,
      'max_bots': integer(max),
      'eligibility': {
        'ok': lock == null,
        'reason': text(lock?.querySelector('p')),
        'done': integer(progress?.attributes['aria-valuenow'] ?? '0'),
        'need': integer(progress?.attributes['aria-valuemax'] ?? '0'),
      },
      'ready': !doc
          .querySelectorAll('#main > .flash.warn')
          .any((f) => text(f).contains('Конструктор сейчас недоступен')),
    };
  }

  Future<Map<String, dynamic>> stats(Map<String, dynamic> query) async {
    final doc = await page(
      '/panel/stats',
      query: {'period': query['period'] ?? '30d'},
    );
    final cards = doc.querySelectorAll('.scard .value');
    if (cards.length < 5) throw const ApiFailure('Сайт не передал статистику.');
    final chart = doc.querySelector('.chart-card');
    List<List<double>> points(String selector) {
      final source = chart?.querySelector(selector)?.attributes['d'] ?? '';
      final values = RegExp(
        r'-?\d+(?:\.\d+)?',
      ).allMatches(source).map((m) => double.parse(m.group(0)!)).toList();
      return [
        for (var i = 0; i + 1 < values.length; i += 2)
          [values[i], values[i + 1]],
      ];
    }

    final created = points('.ln.c-created'),
        done = points('.ln.c-done'),
        refunded = points('.ln.c-ref');
    final grid = chart?.querySelectorAll('.ax.y') ?? <dom.Element>[];
    final lines = chart?.querySelectorAll('.gridline') ?? <dom.Element>[];
    final ticks = chart?.querySelectorAll('.ax.x') ?? <dom.Element>[];
    final max = grid.isEmpty
        ? 0
        : grid.map((e) => integer(text(e))).reduce((a, b) => a > b ? a : b);
    final ys =
        lines
            .map((e) => double.tryParse(e.attributes['y1'] ?? ''))
            .whereType<double>()
            .toList()
          ..sort();
    int count(double y) => ys.length < 2 || ys.last == ys.first
        ? 0
        : (((ys.last - y) / (ys.last - ys.first)) * max)
              .round()
              .clamp(0, max)
              .toInt();
    final series = [
      for (var i = 0; i < created.length; i++)
        {
          'day': ticks.isEmpty
              ? ''
              : text(
                  ticks[(i *
                          (ticks.length - 1) /
                          (created.length <= 1 ? 1 : created.length - 1))
                      .round()],
                ),
          'created': count(created[i][1]),
          'done': i < done.length ? count(done[i][1]) : 0,
          'refunded': i < refunded.length ? count(refunded[i][1]) : 0,
        },
    ];
    final kinds = <Map<String, dynamic>>[];
    for (final row in doc.querySelectorAll('.kind-row')) {
      final nums = row.querySelectorAll('.kr-top span');
      if (nums.length < 3) continue;
      kinds.add({
        'title': text(row.querySelector('.kr-top b')),
        'created': integer(text(nums[0])),
        'done': integer(text(nums[1])),
        'refunded': integer(text(nums[2])),
        'turnover': usd(row.querySelector('.kr-sum b')),
      });
    }
    return {
      'ok': true,
      'analytics': {
        'periods': {
          for (final a in doc.querySelectorAll('.periods a'))
            Uri.parse(a.attributes['href'] ?? '').queryParameters['period'] ??
                '': text(
              a,
            ),
        },
        'created': integer(text(cards[0])),
        'done': integer(text(cards[1])),
        'refunded': integer(text(cards[2])),
        'turnover': usd(cards[3]),
        'topped': usd(cards[4]),
        'tz_hours': integer(text(doc.querySelector('.tz'))),
        'series': series,
        'by_kind': kinds,
      },
    };
  }

  Future<Map<String, dynamic>> pricelist() async {
    final doc = await page('/admin/pricelist');
    final data = doc.querySelector('#pl-data')?.text;
    if (data == null) {
      throw const ApiFailure('В каталоге пока нет пакетов для прайс-листа.');
    }
    final parsed = Map<String, dynamic>.from(jsonDecode(data) as Map);
    return {
      'ok': true,
      ...parsed,
      'name': text(doc.querySelector('.brand')).isEmpty
          ? 'Donatix'
          : text(doc.querySelector('.brand')),
      'site': DonatixApi.origin,
    };
  }
}

import 'package:flutter/material.dart';
import 'api.dart';
import '../screens/account.dart';
import '../screens/balance.dart';
import '../screens/catalog.dart';
import '../screens/dcoin.dart';
import '../screens/document.dart';
import '../screens/pricelist.dart';
import '../screens/management.dart';
import '../screens/orders.dart';
import '../screens/purchase.dart';
import '../screens/stats.dart';
import '../screens/steam.dart';
import '../screens/telegram.dart';

Widget purchasePage(DonatixApi api, String productId) => switch (productId) {
  'steam-topup' => SteamScreen(api: api),
  'steam-gift' => SteamGiftScreen(api: api),
  _ => PurchaseScreen(api: api, productId: productId),
};

Future<void> openDonatixLink(
  BuildContext context,
  DonatixApi api,
  String value,
) async {
  final root = Uri.parse(DonatixApi.origin),
      uri = Uri.parse(DonatixApi.origin).resolve(value);
  if (uri.scheme != root.scheme ||
      uri.host != root.host ||
      uri.port != root.port) {
    await externalLink(context, value);
    return;
  }
  if (uri.path == '/login' ||
      uri.path == '/register' ||
      uri.path.startsWith('/auth/')) {
    api.onSessionExpired?.call();
    return;
  }
  final query = uri.queryParameters;
  final path = uri.path;
  if (path == '/api/swagger' || path == '/api/redoc') {
    await externalLink(context, uri.toString());
    return;
  }
  Widget screen;
  if (path.startsWith('/panel/buy/')) {
    screen = purchasePage(
      api,
      Uri.decodeComponent(path.substring('/panel/buy/'.length)),
    );
  } else if (path == '/panel/catalog') {
    if (query['kind'] == 'telegram' ||
        query['kind'] == 'telegram_stars' ||
        query['kind'] == 'telegram_premium') {
      screen = TelegramScreen(
        api: api,
        premium:
            query['tab'] == 'premium' || query['kind'] == 'telegram_premium',
      );
    } else if ((query['category'] ?? '').isNotEmpty) {
      screen = PacksScreen(
        api: api,
        category: query['category']!,
        kind: query['kind'] ?? '',
        title: 'Каталог',
        region: query['region'],
      );
    } else {
      screen = Scaffold(
        appBar: AppBar(title: const Text('Каталог')),
        body: CatalogScreen(api: api, initialKind: query['kind'] ?? ''),
      );
    }
  } else if (path == '/admin/pricelist') {
    screen = PricelistScreen(api: api);
  } else if (path == '/panel/dcoin') {
    screen = DcoinScreen(api: api);
  } else if (path == '/panel/stats') {
    screen = StatsScreen(api: api);
  } else if (path == '/panel/bots') {
    screen = BotsScreen(api: api);
  } else if (path == '/panel/api') {
    screen = ApiSettingsScreen(
      api: api,
      docs: () => openDonatixLink(context, api, '/docs'),
    );
  } else if (path == '/panel/support') {
    screen = SupportScreen(api: api);
  } else if (path == '/panel/timezone') {
    screen = TimezoneScreen(api: api);
  } else if ([
    '/panel/referrals',
    '/panel/logins',
    '/panel/transactions',
    '/panel/notifications',
  ].contains(path)) {
    screen = AccountScreen(api: api, section: path.split('/').last);
  } else if (path == '/panel/orders') {
    screen = Scaffold(
      appBar: AppBar(title: const Text('Заказы')),
      body: OrdersScreen(api: api),
    );
  } else if (path.startsWith('/panel/orders/')) {
    screen = OrderScreen(
      api: api,
      orderId: Uri.decodeComponent(path.split('/').last),
    );
  } else if (path == '/panel/balance') {
    screen = Scaffold(
      appBar: AppBar(title: const Text('Пополнить баланс')),
      body: BalanceScreen(api: api),
    );
  } else if (RegExp(r'^/panel/balance/\d+/pay$').hasMatch(path)) {
    screen = PaymentScreen(api: api, paymentId: int.parse(path.split('/')[3]));
  } else {
    screen = DocumentScreen(
      api: api,
      path: uri.toString(),
      title: path.startsWith('/admin')
          ? 'Администрация Donatix'
          : path == '/terms'
          ? 'Условия сервиса'
          : path == '/privacy'
          ? 'Конфиденциальность'
          : path == '/docs'
          ? 'Документация'
          : 'Donatix',
    );
  }
  if (api.userId == 0 &&
      (path.startsWith('/admin') ||
          (path.startsWith('/panel') &&
              !path.startsWith('/panel/catalog') &&
              !path.startsWith('/panel/buy')))) {
    api.nextLink = uri.toString();
    api.onSessionExpired?.call();
    return;
  }
  if (context.mounted) {
    await Navigator.push(
      context,
      MaterialPageRoute<void>(builder: (_) => screen),
    );
  }
}

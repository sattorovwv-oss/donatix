import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/api.dart';
import 'widgets/ui.dart';
import 'screens/auth.dart';
import 'screens/home.dart';
import 'screens/catalog.dart';
import 'screens/orders.dart';
import 'screens/balance.dart';
import 'core/navigation.dart';
import 'core/notifications.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  LicenseRegistry.addLicense(() async* {
    yield LicenseEntryWithLineBreaks([
      'Onest',
    ], await rootBundle.loadString('assets/Onest-LICENSE.txt'));
  });
  runApp(const DonatixApp());
}

ThemeData brandTheme(bool dark) {
  final scheme = ColorScheme.fromSeed(
    seedColor: accent,
    brightness: dark ? Brightness.dark : Brightness.light,
    surface: dark ? const Color(0xff161922) : Colors.white,
  );
  return ThemeData(
    useMaterial3: true,
    fontFamily: 'Onest',
    colorScheme: scheme.copyWith(
      primary: dark ? const Color(0xff8b8cf8) : accent,
      onPrimary: dark ? const Color(0xff11122a) : Colors.white,
    ),
    scaffoldBackgroundColor: dark
        ? const Color(0xff0e1016)
        : const Color(0xfff5f6fa),
    dividerColor: dark ? const Color(0xff262b37) : const Color(0xffe7e9f0),
    appBarTheme: AppBarTheme(
      backgroundColor: dark ? const Color(0xff0e1016) : const Color(0xfff5f6fa),
      centerTitle: false,
      surfaceTintColor: Colors.transparent,
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: dark ? const Color(0xff12151d) : const Color(0xfff7f8fb),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: BorderSide(
          color: dark ? const Color(0xff262b37) : const Color(0xffe7e9f0),
        ),
      ),
      contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 16),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: const Size(40, 48),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      ),
    ),
    navigationBarTheme: const NavigationBarThemeData(height: 72),
  );
}

class DonatixApp extends StatefulWidget {
  const DonatixApp({super.key});
  @override
  State<DonatixApp> createState() => _DonatixAppState();
}

class _DonatixAppState extends State<DonatixApp> with WidgetsBindingObserver {
  final api = DonatixApi();
  final navigatorKey = GlobalKey<NavigatorState>();
  bool? signedIn;
  Object? error;
  ThemeMode mode = ThemeMode.system;
  int index = 0;
  late final notifications = NativeNotifications(api);
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    api.onSessionExpired = () {
      if (!mounted) return;
      navigatorKey.currentState?.popUntil((route) => route.isFirst);
      setState(() {
        signedIn = false;
        index = 0;
      });
    };
    api.onSessionChanged = notifications.synchronize;
    restore();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) openNotification();
  }

  Future<void> openNotification() async {
    final link = await notifications.takeLink();
    if (link == null || link.isEmpty) return;
    if (signedIn != true) {
      api.nextLink = link;
      return;
    }
    final context = navigatorKey.currentContext;
    if (context != null && context.mounted) {
      await openDonatixLink(context, api, link);
    }
  }

  Future<void> restore() async {
    setState(() {
      signedIn = null;
      error = null;
    });
    try {
      final prefs = await SharedPreferences.getInstance();
      final name = prefs.getString('theme');
      api.displayCurrency = prefs.getString('currency') == 'TJS'
          ? 'TJS'
          : 'USD';
      final restored = await api.restore();
      if (restored) await notifications.synchronize();
      WidgetsBinding.instance.addPostFrameCallback((_) => openNotification());
      if (mounted) {
        setState(() {
          signedIn = restored;
          mode = name == 'dark'
              ? ThemeMode.dark
              : name == 'light'
              ? ThemeMode.light
              : ThemeMode.system;
        });
      }
    } catch (e) {
      if (mounted) setState(() => error = e);
    }
  }

  void loggedIn() {
    setState(() => signedIn = true);
    notifications.synchronize();
    final link = api.nextLink;
    api.nextLink = null;
    if (link != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        final context = navigatorKey.currentContext;
        if (context != null) openDonatixLink(context, api, link);
      });
    }
  }

  Future<void> currency() async {
    setState(
      () => api.displayCurrency = api.displayCurrency == 'USD' ? 'TJS' : 'USD',
    );
    await (await SharedPreferences.getInstance()).setString(
      'currency',
      api.displayCurrency,
    );
  }

  Future<void> theme() async {
    setState(
      () => mode = mode == ThemeMode.dark ? ThemeMode.light : ThemeMode.dark,
    );
    await (await SharedPreferences.getInstance()).setString('theme', mode.name);
  }

  Future<void> logout(BuildContext c) async {
    final yes = await showDialog<bool>(
      context: c,
      builder: (d) => AlertDialog(
        title: const Text('Выйти из аккаунта?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(d, false),
            child: const Text('Отмена'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(d, true),
            child: const Text('Выйти'),
          ),
        ],
      ),
    );
    if (yes != true) return;
    try {
      await api.logout();
    } catch (_) {
      /* Local session is always removed even when offline. */
    }
    await notifications.stop();
    if (mounted) {
      setState(() {
        signedIn = false;
        index = 0;
      });
    }
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
    navigatorKey: navigatorKey,
    title: 'Donatix',
    debugShowCheckedModeBanner: false,
    theme: brandTheme(false),
    darkTheme: brandTheme(true),
    themeMode: mode,
    home: signedIn == null
        ? Scaffold(
            body: StateView(error: error, retry: restore),
          )
        : signedIn == false
        ? AuthScreen(api: api, onDone: loggedIn)
        : Builder(
            builder: (context) => Scaffold(
              appBar: AppBar(
                title: Row(
                  children: [
                    ClipRRect(
                      borderRadius: BorderRadius.circular(8),
                      child: Image.asset(
                        'assets/logo.png',
                        width: 30,
                        height: 30,
                      ),
                    ),
                    const SizedBox(width: 10),
                    const Text(
                      'Donatix',
                      style: TextStyle(fontWeight: FontWeight.w800),
                    ),
                  ],
                ),
                actions: [
                  NotificationBell(
                    api: api,
                    open: () =>
                        openDonatixLink(context, api, '/panel/notifications'),
                  ),
                  TextButton(
                    onPressed: currency,
                    child: Text(api.displayCurrency),
                  ),
                  IconButton(
                    onPressed: theme,
                    tooltip: 'Сменить тему',
                    icon: const Icon(Icons.brightness_6_outlined),
                  ),
                ],
              ),
              body: SafeArea(
                top: false,
                child: AnimatedSwitcher(
                  duration: MediaQuery.disableAnimationsOf(context)
                      ? Duration.zero
                      : const Duration(milliseconds: 240),
                  child: KeyedSubtree(
                    key: ValueKey(index),
                    child: switch (index) {
                      0 => HomeScreen(
                        api: api,
                        navigate: (i) => setState(() => index = i),
                      ),
                      1 => CatalogScreen(api: api),
                      2 => BalanceScreen(api: api),
                      3 => OrdersScreen(api: api),
                      _ => more(context),
                    },
                  ),
                ),
              ),
              bottomNavigationBar: NavigationBar(
                selectedIndex: index,
                onDestinationSelected: (i) => setState(() => index = i),
                destinations: const [
                  NavigationDestination(
                    icon: Icon(Icons.home_outlined),
                    selectedIcon: Icon(Icons.home),
                    label: 'Главная',
                  ),
                  NavigationDestination(
                    icon: Icon(Icons.grid_view_outlined),
                    label: 'Каталог',
                  ),
                  NavigationDestination(
                    icon: Icon(Icons.account_balance_wallet_outlined),
                    label: 'Пополнить',
                  ),
                  NavigationDestination(
                    icon: Icon(Icons.shopping_bag_outlined),
                    label: 'Заказы',
                  ),
                  NavigationDestination(icon: Icon(Icons.menu), label: 'Ещё'),
                ],
              ),
            ),
          ),
  );
  Widget more(BuildContext context) => ListView(
    padding: const EdgeInsets.all(18),
    children: [
      const Heading('Ещё'),
      for (final e in [
        ('/panel/dcoin', 'D-коин', Icons.currency_exchange),
        ('/panel/referrals', 'Пригласить друзей', Icons.group_add_outlined),
        ('/panel/notifications', 'Уведомления', Icons.notifications_outlined),
        ('/panel/support', 'Поддержка', Icons.support_agent),
        ('/panel/bots', 'Мой Telegram-бот', Icons.smart_toy_outlined),
        ('/panel/api', 'API и webhook', Icons.key_outlined),
        ('/panel/stats', 'Аналитика', Icons.bar_chart),
        ('/panel/logins', 'История входов', Icons.security),
        ('/panel/timezone', 'Часовой пояс', Icons.schedule),
        ('/docs', 'Документация', Icons.book_outlined),
        if (api.role == 'admin')
          ('/admin', 'Админка', Icons.admin_panel_settings_outlined),
        ('/privacy', 'Конфиденциальность', Icons.privacy_tip_outlined),
        ('/terms', 'Условия сервиса', Icons.description_outlined),
      ])
        Surface(
          padding: EdgeInsets.zero,
          child: ListTile(
            leading: Icon(e.$3),
            title: Text(e.$2),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => openDonatixLink(context, api, e.$1),
          ),
        ),
      Surface(child: NotificationPreferences(service: notifications)),
      Surface(
        padding: EdgeInsets.zero,
        child: ListTile(
          leading: const Icon(Icons.logout),
          title: const Text('Выйти'),
          onTap: () => logout(context),
        ),
      ),
      const Center(
        child: Text('Donatix · 1.0.0 (2)', style: TextStyle(fontSize: 12)),
      ),
    ],
  );
}

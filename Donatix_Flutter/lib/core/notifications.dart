import 'dart:async';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'api.dart';
import '../widgets/ui.dart';

class NativeNotifications {
  final DonatixApi api;
  static const channel = MethodChannel('tj.donatix.app/native');
  bool enabled = false;
  String? configured;
  DateTime? lastConfiguration;
  bool synchronizing = false;
  bool synchronizeAgain = false;
  Map<String, dynamic> push = {};
  NativeNotifications(this.api);
  Future<void> refreshStatus() async {
    try {
      final d = await channel.invokeMapMethod<String, dynamic>('pushStatus');
      push = d ?? {};
    } on MissingPluginException {
      push = {};
    } on PlatformException {
      push = {};
    }
  }

  Future<void> synchronize({bool force = false}) async {
    if (synchronizing) {
      synchronizeAgain = true;
      return;
    }
    synchronizing = true;
    try {
      enabled =
          (await SharedPreferences.getInstance()).getBool(
            'background_notifications',
          ) ??
          false;
      final signature = '$enabled|${api.userId}|${api.session}|${api.usesExistingApi}|${api.personalKeyIdentity}';
      if (signature == configured &&
          (!force ||
              (lastConfiguration != null &&
                  DateTime.now().difference(lastConfiguration!) <
                      const Duration(minutes: 1)))) {
        return;
      }
      if (enabled && api.userId != 0 && api.session != null &&
          (!api.usesExistingApi || api.personalApiKey != null)) {
        await channel.invokeMethod<void>('configureNotifications', {
          'origin': DonatixApi.origin,
          'cookie': api.session,
          'userId': api.userId,
          'existingApi': api.usesExistingApi,
          'apiKey': api.personalApiKey,
        });
      } else {
        await channel.invokeMethod<void>('stopNotifications');
      }
      configured = signature;
      lastConfiguration = DateTime.now();
      await refreshStatus();
    } on MissingPluginException {
      /* Other targets keep the in-app inbox. */
    } on PlatformException {
      /* A background-worker failure must not fail a financial API response. */
    } finally {
      synchronizing = false;
      if (synchronizeAgain) {
        synchronizeAgain = false;
        await synchronize(force: true);
      }
    }
  }

  Future<bool> setEnabled(bool value) async {
    if (!value && api.userId != 0) {
      await refreshStatus();
      try {
        if (push['deviceId'] != null) {
          await api.post('/api/v1/mobile/push/unregister', {
            'device_id': push['deviceId'],
          });
        }
      } catch (_) {
        /* Token is also invalidated by the native bridge. */
      }
    }
    if (value) {
      try {
        value =
            await channel.invokeMethod<bool>('requestNotificationPermission') ??
            false;
      } on MissingPluginException {
        value = false;
      } on PlatformException {
        value = false;
      }
    }
    enabled = value;
    await (await SharedPreferences.getInstance()).setBool(
      'background_notifications',
      value,
    );
    await synchronize();
    return value;
  }

  Future<void> stop() async {
    configured = null;
    lastConfiguration = null;
    try {
      await channel.invokeMethod<void>('stopNotifications');
    } on MissingPluginException {
      /* The target does not expose native notifications. */
    } on PlatformException {
      /* The session is also revoked on the server during logout. */
    }
  }

  Future<String?> takeLink() async {
    try {
      return await channel.invokeMethod<String>('takeNotificationLink');
    } on MissingPluginException {
      return null;
    } on PlatformException {
      return null;
    }
  }
}

class NotificationPreferences extends StatefulWidget {
  final NativeNotifications service;
  const NotificationPreferences({super.key, required this.service});
  @override
  State<NotificationPreferences> createState() =>
      _NotificationPreferencesState();
}

class _NotificationPreferencesState extends State<NotificationPreferences> {
  bool busy = false;
  Timer? statusTimer;
  @override
  void initState() {
    super.initState();
    statusTimer = Timer.periodic(const Duration(seconds: 3), (_) async {
      await widget.service.refreshStatus();
      if (widget.service.enabled &&
          (widget.service.push['registered'] != true ||
              widget.service.push['server'] != true)) {
        await widget.service.synchronize(force: true);
      }
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    statusTimer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => SwitchListTile(
    contentPadding: EdgeInsets.zero,
    title: const Text('Уведомления о заказах и балансе'),
    subtitle: Text(
      !widget.service.enabled
          ? 'Получать уведомления о заказах и изменениях баланса.'
          : widget.service.push['permission'] == false && !Platform.isIOS
          ? 'Уведомления выключены в настройках Android.'
          : widget.service.api.usesExistingApi
          ? 'Заказы и баланс проверяются периодически. Доставка может задерживаться. Мгновенные push на сервере не подключены.'
          : widget.service.push['server'] == true &&
                widget.service.push['registered'] == true
          ? Platform.isIOS
                ? 'Push через APNs/FCM подключён. Подробности доступны в приложении.'
                : 'Устройство зарегистрировано для push. Включена резервная фоновая проверка.'
          : widget.service.push['registered'] == true &&
                widget.service.push['server'] != true &&
                !Platform.isIOS
          ? 'На сервере ещё не включена отправка push. Уведомления проверяются периодически.'
          : widget.service.push['firebase'] == true
          ? Platform.isIOS
                ? 'Подключение APNs/FCM. Без push список обновляется при открытии приложения.'
                : 'Регистрация FCM выполняется в фоне. До подключения доступна периодическая проверка.'
          : Platform.isIOS
          ? 'Список обновляется при открытии. Для push владелец должен подключить Firebase и APNs.'
          : 'Доступна периодическая проверка. Для FCM владелец должен подключить свой Firebase-проект.',
    ),
    value: widget.service.enabled,
    onChanged: busy
        ? null
        : (value) async {
            setState(() => busy = true);
            try {
              final on = await widget.service.setEnabled(value);
              if (context.mounted && value && !on) {
                message(
                  context,
                  'Разрешите уведомления в настройках ${Platform.isIOS ? 'iOS' : 'Android'}.',
                );
              }
            } catch (e) {
              if (context.mounted) message(context, e);
            } finally {
              if (mounted) setState(() => busy = false);
            }
          },
  );
}

class NotificationBell extends StatefulWidget {
  final DonatixApi api;
  final VoidCallback open;
  const NotificationBell({super.key, required this.api, required this.open});
  @override
  State<NotificationBell> createState() => _NotificationBellState();
}

class _NotificationBellState extends State<NotificationBell>
    with WidgetsBindingObserver {
  Timer? timer;
  bool loading = false;
  int count = 0;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    refresh();
  }

  @override
  void dispose() {
    timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    timer?.cancel();
    if (state == AppLifecycleState.resumed) refresh();
  }

  Future<void> refresh() async {
    if (loading) return;
    loading = true;
    timer?.cancel();
    try {
      final d = await widget.api.get('/api/v1/mobile/notifications',
          widget.api.usesExistingApi ? {'preview': true} : null);
      if (mounted) setState(() => count = d['unread'] as int? ?? 0);
    } catch (_) {
      /* Keep the last known badge; the inbox reports request errors. */
    } finally {
      loading = false;
      if (mounted &&
          WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed) {
        timer = Timer(const Duration(seconds: 20), refresh);
      }
    }
  }

  @override
  Widget build(BuildContext context) => IconButton(
    tooltip: 'Уведомления',
    onPressed: () {
      widget.open();
      refresh();
    },
    icon: Badge(
      isLabelVisible: count > 0,
      label: Text(count > 99 ? '99+' : '$count'),
      child: const Icon(Icons.notifications_none),
    ),
  );
}

import 'dart:async';
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
  NativeNotifications(this.api);
  Future<void> synchronize() async {
    enabled =
        (await SharedPreferences.getInstance()).getBool(
          'background_notifications',
        ) ??
        false;
    final signature = '$enabled|${api.userId}|${api.session}';
    if (signature == configured) return;
    try {
      if (enabled && api.userId != 0 && api.session != null) {
        await channel.invokeMethod<void>('configureNotifications', {
          'origin': DonatixApi.origin,
          'cookie': api.session,
          'userId': api.userId,
        });
      } else {
        await channel.invokeMethod<void>('stopNotifications');
      }
      configured = signature;
    } on MissingPluginException {
      /* Other targets keep the in-app inbox. */
    } on PlatformException {
      /* A background-worker failure must not fail a financial API response. */
    }
  }

  Future<bool> setEnabled(bool value) async {
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
    try {
      await channel.invokeMethod<void>('stopNotifications');
    } on MissingPluginException {
      /* No Android worker. */
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
  @override
  Widget build(BuildContext context) => SwitchListTile(
    contentPadding: EdgeInsets.zero,
    title: const Text('Фоновые уведомления'),
    subtitle: const Text(
      'Android периодически проверяет заказы и пополнения. Проверка может задерживаться из-за энергосбережения.',
    ),
    value: widget.service.enabled,
    onChanged: busy
        ? null
        : (value) async {
            setState(() => busy = true);
            try {
              final on = await widget.service.setEnabled(value);
              if (context.mounted && value && !on) {
                message(context, 'Разрешите уведомления в настройках Android.');
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
      final d = await widget.api.get('/api/v1/mobile/notifications');
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

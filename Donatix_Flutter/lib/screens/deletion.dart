import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../core/api.dart';
import '../core/checkout.dart';
import '../widgets/ui.dart';

class DeletionScreen extends StatefulWidget {
  final DonatixApi api;
  const DeletionScreen({super.key, required this.api});
  @override
  State<DeletionScreen> createState() => _DeletionScreenState();
}

class _DeletionScreenState extends State<DeletionScreen> {
  final confirmation = TextEditingController(),
      password = TextEditingController();
  Map<String, dynamic>? info;
  int? loadedUserId;
  Object? error;
  bool busy = false;
  @override
  void initState() {
    super.initState();
    load();
  }

  @override
  void dispose() {
    confirmation.dispose();
    password.dispose();
    super.dispose();
  }

  Future<void> load() async {
    final owner = widget.api.userId;
    try {
      final d = await widget.api.get('/api/v1/mobile/account/deletion');
      widget.api.requireAccount(owner);
      if (mounted) {
        setState(() {
          info = d;
          loadedUserId = owner;
          error = null;
        });
      }
    } catch (e) {
      if (mounted) setState(() => error = e);
    }
  }

  Future<void> submit() async {
    if (busy || confirmation.text != 'УДАЛИТЬ') return;
    final deletingUserId = loadedUserId ?? 0;
    if (!await confirmAction(
      context,
      'Удалить аккаунт и данные?',
      'Вход, API и боты отключатся. Незавершённые расчёты будут завершены перед удалением. D-коины будут утрачены.',
    )) {
      return;
    }
    if (!mounted) return;
    setState(() => busy = true);
    try {
      widget.api.requireAccount(deletingUserId);
      final d = await widget.api.post('/api/v1/mobile/account/deletion', {
        'confirmation': confirmation.text,
        'password': password.text,
      });
      password.clear();
      if (!mounted) return;
      await showDialog<void>(
        context: context,
        builder: (c) => AlertDialog(
          title: Text(
            d['state'] == 'completed' ? 'Данные удалены' : 'Заявка принята',
          ),
          content: Text(
            '${d['message']}\n${(d['reasons'] as List? ?? []).join('\n')}',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(c),
              child: const Text('Понятно'),
            ),
          ],
        ),
      );
      if (widget.api.userId != deletingUserId) return;
      // Invalidate in-flight responses before deleting local credentials.
      Object? cleanupError;
      try {
        await widget.api.clear();
        await widget.api.storage.deleteAll();
      } catch (e) {
        cleanupError = e;
      }
      PaintingBinding.instance.imageCache.clear();
      PaintingBinding.instance.imageCache.clearLiveImages();
      try {
        await const MethodChannel(
          'tj.donatix.app/native',
        ).invokeMethod<void>('clearPrivateFiles');
      } on PlatformException catch (e) {
        cleanupError = e;
      } on MissingPluginException {
        /* Unsupported targets still clear the Flutter session. */
      }
      if (cleanupError != null && mounted) {
        await showDialog<void>(
          context: context,
          builder: (c) => AlertDialog(
            title: const Text('Очистка устройства'),
            content: const Text(
              'Сервер принял удаление, но очистить все локальные файлы не удалось. Очистите данные Donatix в настройках Android.',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(c),
                child: const Text('Понятно'),
              ),
            ],
          ),
        );
      }
      widget.api.onSessionExpired?.call();
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Удаление аккаунта')),
    body: info == null
        ? StateView(error: error, retry: load)
        : ListView(
            padding: const EdgeInsets.all(18),
            children: [
              const Heading('Удаление аккаунта и данных'),
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(text(info!['erased'])),
                    if (info!['retained'] != null) ...[
                      const SizedBox(height: 12),
                      Text(text(info!['retained'])),
                    ],
                    if (info!['processing'] != null) ...[
                      const SizedBox(height: 12),
                      Text(text(info!['processing'])),
                    ],
                    const SizedBox(height: 12),
                    const Text(
                      'Действие необратимо. D-коины будут утрачены. Заказы и возврат остатка баланса должны завершиться перед удалением.',
                    ),
                    InfoRow(
                      'Баланс',
                      widget.api.displayPrice(info!['balance_usd']),
                    ),
                    for (final reason in info!['blockers'] as List? ?? [])
                      Padding(
                        padding: const EdgeInsets.only(bottom: 10),
                        child: Text(
                          '$reason',
                          style: TextStyle(
                            color: Theme.of(context).colorScheme.error,
                          ),
                        ),
                      ),
                    TextFormField(
                      controller: password,
                      obscureText: true,
                      autocorrect: false,
                      decoration: const InputDecoration(
                        labelText: 'Текущий пароль',
                        helperText:
                            'Или выйдите и войдите заново, затем подтвердите удаление в течение 5 минут',
                      ),
                    ),
                    const SizedBox(height: 14),
                    TextField(
                      controller: confirmation,
                      onChanged: (_) => setState(() {}),
                      decoration: const InputDecoration(
                        labelText: 'Введите УДАЛИТЬ',
                      ),
                    ),
                    const SizedBox(height: 18),
                    BusyButton(
                      'Удалить аккаунт и данные',
                      busy: busy,
                      onPressed: confirmation.text == 'УДАЛИТЬ' ? submit : null,
                    ),
                  ],
                ),
              ),
              Text('Поддержка: ${info!['support_contact']}'),
            ],
          ),
  );
}

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import '../core/api.dart';
import '../core/checkout.dart';
import '../widgets/ui.dart';
import 'account.dart';

Future<void> externalLink(BuildContext context, String value) async {
  final uri = Uri.tryParse(value);
  if (uri == null ||
      !['https', 'tg', 'mailto', 'tel'].contains(uri.scheme) ||
      uri.userInfo.isNotEmpty) {
    if (context.mounted) message(context, 'Ссылка недоступна.');
    return;
  }
  try {
    if (!await launchUrl(uri, mode: LaunchMode.externalApplication) &&
        context.mounted) {
      message(context, 'Не удалось открыть ссылку.');
    }
  } catch (_) {
    if (context.mounted) message(context, 'Не удалось открыть ссылку.');
  }
}

Future<void> copyValue(BuildContext context, String value) async {
  await Clipboard.setData(ClipboardData(text: value));
  if (context.mounted) message(context, 'Скопировано');
}

class BotsScreen extends StatefulWidget {
  final DonatixApi api;
  const BotsScreen({super.key, required this.api});
  @override
  State<BotsScreen> createState() => _BotsScreenState();
}

class _BotsScreenState extends State<BotsScreen> {
  final token = TextEditingController(), admins = TextEditingController();
  final form = GlobalKey<FormState>();
  int revision = 0;
  bool busy = false, hidden = true;
  @override
  void dispose() {
    token.dispose();
    admins.dispose();
    super.dispose();
  }

  Future<void> create() async {
    if (!(form.currentState?.validate() ?? false)) return;
    setState(() => busy = true);
    try {
      await widget.api.post('/api/v1/mobile/bots', {
        'token': token.text.trim(),
        'admin_ids': admins.text.trim(),
      });
      token.clear();
      admins.clear();
      if (mounted) {
        message(
          context,
          'Бот подключён — запустится в течение минуты. Управление в боте: /panel.',
        );
        setState(() => revision++);
      }
    } catch (e) {
      if (mounted) {
        message(context, e);
        setState(() => revision++);
      }
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> action(Map b, String action, [String ids = '']) async {
    if (busy) return;
    if (action == 'delete' &&
        !await confirmAction(
          context,
          'Удалить бота @${b['username']}?',
          'Бот перестанет работать, его API-ключ будет отозван.',
          action: 'Удалить',
        )) {
      return;
    }
    setState(() => busy = true);
    try {
      await widget.api.post('/api/v1/mobile/bots/${b['id']}/$action', {
        'admin_ids': ids,
      });
      if (mounted) {
        message(context, 'Изменения сохранены.');
        setState(() => revision++);
      }
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> editAdmins(Map b) async {
    final input = TextEditingController(text: text(b['admin_ids']));
    final saved = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Сменить админов'),
        content: TextField(
          controller: input,
          keyboardType: TextInputType.number,
          decoration: const InputDecoration(
            labelText: 'Telegram ID через запятую',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(c),
            child: const Text('Отмена'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(c, true),
            child: const Text('Сохранить'),
          ),
        ],
      ),
    );
    final value = input.text;
    input.dispose();
    if (saved == true) await action(b, 'admins', value);
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Мой Telegram-бот')),
    body: AsyncPage(
      key: ValueKey(revision),
      load: () => widget.api.get('/api/v1/mobile/bots'),
      builder: (context, d) {
        final elig = d['eligibility'] as Map;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Heading(
              'Мой Telegram-бот',
              subtitle:
                  'Свой бот-магазин за минуту: игры, звёзды и цены — уже внутри. Заказы оплачиваются с вашего баланса Donatix, прибыль — ваша наценка.',
            ),
            if (d['ready'] != true)
              const Surface(
                child: Text(
                  'Конструктор сейчас недоступен. Напишите в поддержку.',
                ),
              ),
            if (elig['ok'] != true)
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Heading('Свой бот пока закрыт'),
                    Text(text(elig['reason'])),
                    const SizedBox(height: 12),
                    LinearProgressIndicator(
                      value:
                          ((elig['done'] as num) /
                                  ((elig['need'] as num) == 0
                                      ? 1
                                      : (elig['need'] as num)))
                              .clamp(0, 1)
                              .toDouble(),
                    ),
                    Text(
                      'Выполнено заказов: ${elig['done']} из ${elig['need']}',
                    ),
                  ],
                ),
              )
            else
              Surface(
                child: Form(
                  key: form,
                  child: Column(
                    children: [
                      const Heading('Подключить бота'),
                      TextButton(
                        onPressed: () =>
                            externalLink(context, 'https://t.me/BotFather'),
                        child: const Text(
                          '1 · @BotFather → /newbot → получите токен',
                        ),
                      ),
                      TextButton(
                        onPressed: () =>
                            externalLink(context, 'https://t.me/userinfobot'),
                        child: const Text('2 · Узнайте свой Telegram ID'),
                      ),
                      TextFormField(
                        controller: token,
                        enabled: !busy,
                        obscureText: hidden,
                        autocorrect: false,
                        decoration: InputDecoration(
                          labelText: 'Токен бота',
                          hintText: '123456789:AAH…',
                          suffixIcon: IconButton(
                            onPressed: () => setState(() => hidden = !hidden),
                            icon: Icon(
                              hidden
                                  ? Icons.visibility_outlined
                                  : Icons.visibility_off_outlined,
                            ),
                          ),
                        ),
                        validator: (v) => (v ?? '').contains(':')
                            ? null
                            : 'Введите токен бота',
                      ),
                      const SizedBox(height: 12),
                      TextFormField(
                        controller: admins,
                        enabled: !busy,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(
                          labelText: 'Telegram ID админов через запятую',
                        ),
                        validator: (v) =>
                            RegExp(
                              r'^\s*\d+(\s*,\s*\d+)*\s*$',
                            ).hasMatch(v ?? '')
                            ? null
                            : 'Введите Telegram ID',
                      ),
                      const SizedBox(height: 16),
                      BusyButton(
                        'Подключить и запустить',
                        busy: busy,
                        onPressed: d['ready'] == true ? create : null,
                      ),
                      const SizedBox(height: 10),
                      const Text(
                        'Токен хранится на сервере зашифрованным. Для бота создастся отдельный API-ключ — удалите бота, и ключ отзовётся.',
                        style: TextStyle(fontSize: 12),
                      ),
                    ],
                  ),
                ),
              ),
            Heading(
              'Мои боты',
              subtitle: '${(d['items'] as List).length} из ${d['max_bots']}',
            ),
            if ((d['items'] as List).isEmpty)
              const Surface(
                child: Text('Ботов пока нет — подключите первый выше.'),
              ),
            for (final b in d['items'] as List)
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Heading(
                      '@${b['username']}',
                      subtitle: b['running'] == true
                          ? 'Работает'
                          : b['enabled'] == 1 || b['enabled'] == true
                          ? 'Запускается…'
                          : 'Остановлен',
                    ),
                    Text('Админы: ${b['admin_ids']}'),
                    if ((b['warn_count'] as num? ?? 0) > 0)
                      Text(
                        'Предупреждение ${b['warn_count']}: в боте давно нет продаж.',
                      ),
                    if (text(b['disabled_reason']).isNotEmpty)
                      Text('Причина отключения: ${b['disabled_reason']}'),
                    if (b['conflict'] == true)
                      const Text(
                        'Этот токен запущен ещё где-то. Остановите другую копию или создайте нового бота.',
                      ),
                    Wrap(
                      spacing: 8,
                      children: [
                        TextButton(
                          onPressed: () => externalLink(
                            context,
                            'https://t.me/${b['username']}',
                          ),
                          child: const Text('Открыть в Telegram'),
                        ),
                        if (b['enabled'] == 1 || b['enabled'] == true) ...[
                          TextButton(
                            onPressed: busy
                                ? null
                                : () => action(b as Map, 'restart'),
                            child: const Text('Перезапустить'),
                          ),
                          TextButton(
                            onPressed: busy
                                ? null
                                : () => action(b as Map, 'stop'),
                            child: const Text('Остановить'),
                          ),
                        ] else if (![
                          'inactive',
                          'admin',
                        ].contains(b['disabled_reason']))
                          TextButton(
                            onPressed: busy
                                ? null
                                : () => action(b as Map, 'start'),
                            child: const Text('Запустить'),
                          ),
                        TextButton(
                          onPressed: busy ? null : () => editAdmins(b as Map),
                          child: const Text('Сменить админов'),
                        ),
                        TextButton(
                          onPressed: busy
                              ? null
                              : () => action(b as Map, 'delete'),
                          child: const Text(
                            'Удалить',
                            style: TextStyle(color: Colors.red),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            const Surface(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Heading('Что умеет бот'),
                  Text(
                    '🕹 Игры по регионам — добавляйте игры, меняйте цены и названия пакетов\n💳 Ваши реквизиты: банки и USDT TRC20/BEP20\n💵 Цены в сомони по свежему курсу, ваша наценка\n🏦 Пополнение счёта Donatix прямо из бота с чеком\n\nУправление — в самом боте: /panel',
                  ),
                ],
              ),
            ),
          ],
        );
      },
    ),
  );
}

class ApiSettingsScreen extends StatefulWidget {
  final DonatixApi api;
  final VoidCallback? docs;
  const ApiSettingsScreen({super.key, required this.api, this.docs});
  @override
  State<ApiSettingsScreen> createState() => _ApiSettingsScreenState();
}

class _ApiSettingsScreenState extends State<ApiSettingsScreen> {
  final name = TextEditingController(), url = TextEditingController();
  final revealed = <int, String>{};
  String? newKey;
  bool busy = false, rotate = false, initialized = false;
  int revision = 0;
  @override
  void dispose() {
    name.dispose();
    url.dispose();
    revealed.clear();
    newKey = null;
    super.dispose();
  }

  Future<void> run(Future<void> Function() callback) async {
    if (busy) return;
    setState(() => busy = true);
    try {
      await callback();
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> create() => run(() async {
    final d = await widget.api.post('/api/v1/mobile/keys', {
      'name': name.text.trim(),
    });
    name.clear();
    if (mounted) {
      setState(() {
        newKey = text(d['key']);
        revision++;
      });
    }
  });
  Future<void> keyAction(Map k, String action) async {
    if (action == 'revoke' &&
        !await confirmAction(
          context,
          'Отозвать ключ?',
          'Всё, что использует этот ключ, перестанет работать.',
          action: 'Отозвать',
        )) {
      return;
    }
    await run(() async {
      final d = await widget.api.post(
        '/api/v1/mobile/keys/${k['id']}/${action == 'copy' ? 'reveal' : action}',
        {},
      );
      if (!mounted) return;
      if (action == 'test') {
        message(
          context,
          d['valid'] == true ? 'Ключ работает.' : 'Ключ недействителен.',
        );
      } else if (action == 'copy') {
        await copyValue(context, text(d['key']));
      } else if (action == 'reveal') {
        setState(() => revealed[k['id'] as int] = text(d['key']));
      } else {
        setState(() {
          revealed.remove(k['id']);
          revision++;
        });
      }
    });
  }

  Future<void> save() => run(() async {
    if (rotate &&
        !await confirmAction(
          context,
          'Выпустить новый секрет?',
          'Обновите секрет в системе, принимающей webhook. Старый секрет перестанет подходить.',
        )) {
      return;
    }
    await widget.api.post('/api/v1/mobile/webhook', {
      'url': url.text.trim(),
      'rotate': rotate,
    });
    if (mounted) {
      message(context, 'Настройки webhook сохранены.');
      setState(() {
        rotate = false;
        revision++;
      });
    }
  });
  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('API и webhook')),
    body: AsyncPage(
      key: ValueKey(revision),
      load: () => widget.api.get('/api/v1/mobile/keys'),
      builder: (context, d) {
        if (!initialized) {
          url.text = text(d['webhook_url']);
          initialized = true;
        }
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Heading(
              'API и webhook',
              subtitle: 'Подключите свой сайт или бота.',
            ),
            TextButton(
              onPressed: widget.docs,
              child: const Text('Документация'),
            ),
            if (newKey != null)
              Surface(
                child: Column(
                  children: [
                    const Heading('Новый ключ создан'),
                    SelectableText(newKey!),
                    TextButton(
                      onPressed: () => copyValue(context, newKey!),
                      child: const Text('Скопировать'),
                    ),
                  ],
                ),
              ),
            Surface(
              child: Column(
                children: [
                  const Heading('API-ключи'),
                  if (d['active'] == true) ...[
                    TextField(
                      controller: name,
                      enabled: !busy,
                      maxLength: 64,
                      decoration: const InputDecoration(
                        labelText: 'Название',
                        hintText: 'Мой бот',
                      ),
                    ),
                    BusyButton('Создать ключ', busy: busy, onPressed: create),
                  ] else
                    const Text('Ключи можно создать после активации аккаунта.'),
                ],
              ),
            ),
            if ((d['items'] as List).isEmpty)
              const Surface(child: Text('Ключей нет.')),
            for (final k in d['items'] as List)
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Heading(text(k['name'])),
                    SelectableText(revealed[k['id']] ?? '${k['prefix']}…'),
                    InfoRow('Создан', text(k['created_at'])),
                    InfoRow(
                      'Использован',
                      text(k['last_used_at'] ?? 'ни разу'),
                    ),
                    if (k['revoked_at'] != null)
                      const Text('Отозван')
                    else
                      Wrap(
                        children: [
                          for (final a in {
                            'reveal': 'Показать',
                            'copy': 'Скопировать',
                            'test': 'Проверить ключ',
                            'revoke': 'Отозвать',
                          }.entries)
                            TextButton(
                              onPressed: busy
                                  ? null
                                  : () => keyAction(k as Map, a.key),
                              child: Text(a.value),
                            ),
                        ],
                      ),
                  ],
                ),
              ),
            const Surface(
              child: Text(
                'Передавайте полный ключ в заголовке X-API-Key. Начало ключа с многоточием не подходит для подключения.',
              ),
            ),
            Surface(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Heading('Webhook'),
                  const Text(
                    'Куда присылать уведомления, когда заказ выполнен или отменён. Необязательно.',
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: url,
                    enabled: !busy,
                    keyboardType: TextInputType.url,
                    decoration: const InputDecoration(
                      labelText: 'Адрес',
                      hintText: 'https://your-site.com/donatix-hook',
                    ),
                  ),
                  const SizedBox(height: 12),
                  const Text('Секрет для проверки подписи'),
                  SelectableText(text(d['webhook_secret'])),
                  TextButton(
                    onPressed: () =>
                        copyValue(context, text(d['webhook_secret'])),
                    child: const Text('Копировать секрет'),
                  ),
                  CheckboxListTile(
                    contentPadding: EdgeInsets.zero,
                    value: rotate,
                    onChanged: busy
                        ? null
                        : (v) => setState(() => rotate = v ?? false),
                    title: const Text('Выпустить новый секрет'),
                  ),
                  BusyButton('Сохранить', busy: busy, onPressed: save),
                ],
              ),
            ),
          ],
        );
      },
    ),
  );
}

class SupportScreen extends StatefulWidget {
  final DonatixApi api;
  const SupportScreen({super.key, required this.api});
  @override
  State<SupportScreen> createState() => _SupportScreenState();
}

class _SupportScreenState extends State<SupportScreen> {
  int revision = 0;
  bool busy = false;
  Future<void> code() async {
    setState(() => busy = true);
    try {
      await widget.api.existingSite.form(
        '/panel/support/code',
        {},
        back: '/panel/support',
      );
      if (mounted) {
        await Navigator.push(
          context,
          MaterialPageRoute<void>(
            builder: (_) =>
                AccountScreen(api: widget.api, section: 'notifications'),
          ),
        );
      }
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      // A new notification code revokes the code in the previously shown link.
      if (mounted) {
        setState(() {
          busy = false;
          revision++;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('Поддержка'),
      actions: [
        IconButton(
          tooltip: 'Получить новый код',
          onPressed: busy ? null : () => setState(() => revision++),
          icon: const Icon(Icons.refresh),
        ),
      ],
    ),
    body: AsyncPage(
      key: ValueKey(revision),
      // The live website supports this page even without the mobile module.
      load: () => widget.api.existingSite.support(),
      builder: (context, d) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Heading(
            'Поддержка в Telegram',
            subtitle:
                'Помощник отвечает сразу, по-таджикски и по-русски: заказы, пополнения, баланс, свой бот.',
          ),
          if (text(d['code']).isEmpty)
            const Surface(child: Text('Бот поддержки ещё не подключён.'))
          else
            Surface(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Heading('1 · Нажмите кнопку'),
                  const Text('Откроется бот, и он сразу узнает ваш аккаунт.'),
                  if (text(d['bot']).isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Text(
                      '@${d['bot']}',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ],
                  const SizedBox(height: 12),
                  BusyButton(
                    'Открыть Telegram',
                    onPressed: text(d['url']).isEmpty
                        ? null
                        : () => externalLink(context, text(d['url'])),
                  ),
                  const SizedBox(height: 20),
                  const Heading('2 · Или отправьте код'),
                  SelectableText(
                    text(d['code']),
                    style: const TextStyle(
                      fontSize: 28,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  TextButton(
                    onPressed: () => copyValue(context, text(d['code'])),
                    child: const Text('Копировать'),
                  ),
                  Text(
                    'Код одноразовый, действует ${d['minutes']} минут. Нужен новый — обновите страницу.',
                  ),
                  const SizedBox(height: 12),
                  BusyButton(
                    'Получить код в уведомления',
                    busy: busy,
                    onPressed: code,
                  ),
                  const SizedBox(height: 12),
                  const Text(
                    'Никому не пересылайте код: с ним видны ваши заказы и баланс.',
                    style: TextStyle(fontSize: 12),
                  ),
                ],
              ),
            ),
        ],
      ),
    ),
  );
}

class TimezoneScreen extends StatefulWidget {
  final DonatixApi api;
  const TimezoneScreen({super.key, required this.api});
  @override
  State<TimezoneScreen> createState() => _TimezoneScreenState();
}

class _TimezoneScreenState extends State<TimezoneScreen> {
  String? choice;
  bool busy = false;
  Future<void> save() async {
    if (choice == null) return;
    setState(() => busy = true);
    try {
      await widget.api.post('/api/v1/mobile/timezone', {'timezone': choice});
      if (mounted) message(context, 'Часовой пояс сохранён.');
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Часовой пояс')),
    body: AsyncPage(
      load: () => widget.api.get('/api/v1/mobile/timezone'),
      builder: (context, d) {
        choice ??= text(d['choice']);
        return Column(
          children: [
            DropdownButtonFormField<String>(
              initialValue: choice,
              isExpanded: true,
              decoration: const InputDecoration(labelText: 'Часовой пояс'),
              items: [
                const DropdownMenuItem(
                  value: 'auto',
                  child: Text('Автоматически — по устройству'),
                ),
                for (final z in d['zones'] as List)
                  DropdownMenuItem(
                    value: text(z[0]),
                    child: Text(text(z[1]), overflow: TextOverflow.ellipsis),
                  ),
              ],
              onChanged: busy ? null : (v) => setState(() => choice = v),
            ),
            const SizedBox(height: 16),
            BusyButton('Сохранить', busy: busy, onPressed: save),
          ],
        );
      },
    ),
  );
}

import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:file_picker/file_picker.dart';
import 'package:image_picker/image_picker.dart';
import 'package:url_launcher/url_launcher.dart';
import '../core/api.dart';
import '../core/checkout.dart';
import '../widgets/ui.dart';

class BalanceScreen extends StatefulWidget {
  final DonatixApi api;
  const BalanceScreen({super.key, required this.api});
  @override
  State<BalanceScreen> createState() => _BalanceScreenState();
}

class _BalanceScreenState extends State<BalanceScreen> {
  final amount = TextEditingController();
  final form = GlobalKey<FormState>();
  String? method;
  String currency = 'USD';
  bool busy = false;
  int revision = 0;
  @override
  void dispose() {
    amount.dispose();
    super.dispose();
  }

  Future<void> create() async {
    if (!(form.currentState?.validate() ?? false) || method == null) return;
    setState(() => busy = true);
    try {
      final d = await widget.api.post('/api/v1/payments', {
        'method': method,
        currency == 'USD' ? 'amount_usd' : 'amount_tjs': amount.text
            .trim()
            .replaceAll(',', '.'),
      });
      if (mounted) {
        await Navigator.push(
          context,
          MaterialPageRoute<void>(
            builder: (_) => PaymentScreen(
              api: widget.api,
              paymentId: d['payment']['id'] as int,
            ),
          ),
        );
        if (mounted) setState(() => revision++);
      }
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => AsyncPage(
    key: ValueKey(revision),
    load: () async {
      final methods = await widget.api.get('/api/v1/payments/methods');
      final payments = await widget.api.get('/api/v1/payments');
      final balance = await widget.api.get('/api/v1/balance');
      return {
        ...methods,
        'payments': payments['items'],
        'balance': balance['balance'],
      };
    },
    builder: (context, d) => Form(
      key: form,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Heading('Пополнить баланс'),
          Surface(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  widget.api.displayPrice(d['balance']),
                  style: const TextStyle(
                    fontSize: 30,
                    fontWeight: FontWeight.w800,
                  ),
                ),
                const Text('Доступно'),
                const SizedBox(height: 20),
                DropdownButtonFormField<String>(
                  initialValue: method,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Способ оплаты'),
                  items: (d['methods'] as List)
                      .map(
                        (m) => DropdownMenuItem(
                          value: text(m['code']),
                          child: Text(
                            text(m['title']),
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      )
                      .toList(),
                  onChanged: busy ? null : (v) => setState(() => method = v),
                  validator: (v) => v == null ? 'Выберите способ' : null,
                ),
                const SizedBox(height: 14),
                SegmentedButton<String>(
                  segments: const [
                    ButtonSegment(value: 'USD', label: Text('USD')),
                    ButtonSegment(value: 'TJS', label: Text('Сомони')),
                  ],
                  selected: {currency},
                  onSelectionChanged: busy
                      ? null
                      : (v) => setState(() => currency = v.first),
                ),
                const SizedBox(height: 14),
                TextFormField(
                  controller: amount,
                  keyboardType: const TextInputType.numberWithOptions(
                    decimal: true,
                  ),
                  decoration: InputDecoration(labelText: 'Сумма в $currency'),
                  validator: (v) {
                    final n = num.tryParse((v ?? '').replaceAll(',', '.'));
                    return n == null || !n.isFinite || n <= 0
                        ? 'Введите положительную сумму'
                        : null;
                  },
                ),
                const SizedBox(height: 8),
                Text(
                  'Минимум: ${d['min_usd']} USD / ${d['min_tjs']} TJS\nКурс: ${d['tjs_rate']} TJS за 1 USD',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
                const SizedBox(height: 16),
                BusyButton('Продолжить', busy: busy, onPressed: create),
              ],
            ),
          ),
          const Heading('Заявки на пополнение'),
          if ((d['payments'] as List).isEmpty)
            const Surface(child: Text('Заявок пока нет.')),
          ...(d['payments'] as List).map(
            (p) => Surface(
              padding: EdgeInsets.zero,
              child: ListTile(
                title: Text(text(p['method_title'])),
                subtitle: Text(statusTitle(p['status'])),
                trailing: Text(widget.api.displayPrice(p['amount_usd'])),
                onTap: () => Navigator.push(
                  context,
                  MaterialPageRoute<void>(
                    builder: (_) => PaymentScreen(
                      api: widget.api,
                      paymentId: p['id'] as int,
                    ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    ),
  );
}

class PaymentScreen extends StatefulWidget {
  final DonatixApi api;
  final int paymentId;
  const PaymentScreen({super.key, required this.api, required this.paymentId});
  @override
  State<PaymentScreen> createState() => _PaymentScreenState();
}

class _PaymentScreenState extends State<PaymentScreen>
    with WidgetsBindingObserver {
  Map<String, dynamic>? payment;
  Object? error;
  Timer? timer;
  bool busy = false, loading = false;
  Future<void> action(String action) async {
    if (busy) return;
    if (action == 'cancel' &&
        !await confirmAction(
          context,
          'Отменить заявку?',
          'Отменить можно только заявку без отправленного чека.',
          action: 'Отменить заявку',
        )) {
      return;
    }
    setState(() => busy = true);
    try {
      await widget.api.post(
        '/api/v1/mobile/payments/${widget.paymentId}/$action',
        {},
      );
      if (mounted) {
        message(
          context,
          action == 'boost'
              ? 'Напомнили администратору — заявка снова наверху списка.'
              : 'Заявка отменена.',
        );
      }
      await refresh();
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

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
      final d = await widget.api.get('/api/v1/payments/${widget.paymentId}');
      if (mounted) {
        setState(() {
          payment = Map<String, dynamic>.from(d['payment'] as Map);
          error = null;
        });
      }
    } catch (e) {
      if (mounted) setState(() => error = e);
    } finally {
      loading = false;
      if (mounted &&
          (payment == null || payment!['status'] == 'pending') &&
          WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed) {
        timer = Timer(const Duration(seconds: 25), refresh);
      }
    }
  }

  Future<void> attach(bool camera) async {
    if (busy) return;
    setState(() => busy = true);
    try {
      String? path;
      if (camera) {
        path = (await ImagePicker().pickImage(
          source: ImageSource.camera,
          imageQuality: 90,
        ))?.path;
      } else {
        final r = await FilePicker.platform.pickFiles(
          type: FileType.custom,
          allowedExtensions: ['jpg', 'jpeg', 'png', 'webp', 'pdf'],
        );
        path = r?.files.single.path;
      }
      if (path == null) return;
      final d = await widget.api.receipt(widget.paymentId, path);
      if (mounted) {
        setState(
          () => payment = Map<String, dynamic>.from(d['payment'] as Map),
        );
        message(context, 'Чек отправлен. Статус обновится после проверки.');
      }
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: Text('Пополнение №${widget.paymentId}'),
      actions: [
        IconButton(onPressed: refresh, icon: const Icon(Icons.refresh)),
      ],
    ),
    body: payment == null
        ? StateView(error: error, retry: refresh)
        : ListView(
            padding: const EdgeInsets.all(18),
            children: [
              Heading(
                statusTitle(payment!['status']),
                subtitle: text(payment!['method_title']),
              ),
              if (error != null) Surface(child: Text('$error')),
              Surface(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    InfoRow(
                      'К переводу',
                      '${payment!['pay_amount']} ${payment!['pay_currency']}',
                    ),
                    InfoRow('Будет зачислено', '\$${payment!['amount_usd']}'),
                    if (text(payment!['network']).isNotEmpty)
                      InfoRow('Сеть', text(payment!['network'])),
                    for (final key in [
                      'details',
                      'address',
                      'network_note',
                      'auto_note',
                      'note',
                    ])
                      if (text(payment![key]).isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.only(top: 12),
                          child: SelectableText(text(payment![key])),
                        ),
                    if (payment!['status'] == 'pending') ...[
                      const SizedBox(height: 14),
                      OutlinedButton.icon(
                        icon: const Icon(Icons.copy),
                        label: const Text('Копировать реквизиты'),
                        onPressed: () async {
                          await Clipboard.setData(
                            ClipboardData(
                              text: text(payment!['address']).isEmpty
                                  ? text(payment!['details'])
                                  : text(payment!['address']),
                            ),
                          );
                          if (context.mounted) message(context, 'Скопировано');
                        },
                      ),
                      if (text(payment!['pay_url']).isNotEmpty)
                        BusyButton(
                          'Перейти к оплате',
                          onPressed: () async {
                            final uri = Uri.tryParse(text(payment!['pay_url']));
                            if (uri == null || uri.scheme != 'https') {
                              message(context, 'Ссылка оплаты недоступна.');
                              return;
                            }
                            try {
                              if (!await launchUrl(
                                    uri,
                                    mode: LaunchMode.externalApplication,
                                  ) &&
                                  context.mounted) {
                                message(context, 'Не удалось открыть оплату.');
                              }
                            } catch (e) {
                              if (context.mounted) {
                                message(context, 'Не удалось открыть оплату.');
                              }
                            }
                          },
                        ),
                      if (text(payment!['auto']).isEmpty) ...[
                        const SizedBox(height: 16),
                        Text(
                          payment!['receipt'] == true
                              ? 'Чек уже отправлен'
                              : 'Приложите чек после перевода',
                        ),
                        const SizedBox(height: 8),
                        BusyButton(
                          'Выбрать фото или PDF',
                          busy: busy,
                          onPressed: () => attach(false),
                        ),
                        OutlinedButton.icon(
                          onPressed: busy ? null : () => attach(true),
                          icon: const Icon(Icons.camera_alt_outlined),
                          label: const Text('Снять чек'),
                        ),
                        if (payment!['receipt'] == true)
                          OutlinedButton.icon(
                            onPressed: busy ? null : () => action('boost'),
                            icon: const Icon(Icons.bolt),
                            label: const Text('Ускорить проверку'),
                          ),
                        if (payment!['receipt'] != true)
                          TextButton(
                            onPressed: busy ? null : () => action('cancel'),
                            child: const Text('Отменить заявку'),
                          ),
                      ],
                    ],
                  ],
                ),
              ),
            ],
          ),
  );
}

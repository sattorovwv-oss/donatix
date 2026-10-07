import 'package:flutter/material.dart';
import '../core/api.dart';
import 'site_design.dart';

const accent = Color(0xff4f46e5);
const kinds = <String, String>{
  '': 'Все',
  'topup': 'Игры и сервисы',
  'telegram_stars': 'Stars',
  'telegram_premium': 'Premium',
  'steam_topup': 'Steam',
  'steam_gift': 'Steam Гифты',
  'gift_card': 'Подарочные карты',
  'game_key': 'Ключи игр',
};
String text(dynamic value) => value == null ? '' : '$value';
String statusTitle(dynamic s) =>
    const {
      'processing': 'В обработке',
      'completed': 'Выполнен',
      'failed': 'Отменён · возврат',
      'pending': 'Ожидает',
      'approved': 'Зачислено',
      'paid': 'Зачислено',
      'attention': 'Требует внимания',
      'rejected': 'Отклонено',
      'cancelled': 'Отменено',
      'active': 'Активен',
      'blocked': 'Заблокирован',
    }[s] ??
    text(s);
String imageUrl(dynamic value) {
  final s = text(value);
  if (s.isEmpty) return '';
  final uri = Uri.parse(DonatixApi.origin).resolve(s);
  return uri.scheme == 'https' ? uri.toString() : '';
}

void message(BuildContext context, Object error) {
  ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
}

class Surface extends StatelessWidget {
  final Widget child;
  final EdgeInsets padding;
  const Surface({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(18),
  });
  @override
  Widget build(BuildContext context) => SiteReveal(
    child: Container(
      padding: padding,
      margin: const EdgeInsets.only(bottom: 14),
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.surface,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Theme.of(context).dividerColor),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: .04),
            blurRadius: 8,
            offset: const Offset(0, 2),
          ),
        ],
      ),
      child: child,
    ),
  );
}

class Heading extends StatelessWidget {
  final String title;
  final String? subtitle;
  const Heading(this.title, {super.key, this.subtitle});
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 20, top: 6),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          title,
          style: Theme.of(
            context,
          ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700),
        ),
        if (subtitle != null)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              subtitle!,
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ),
      ],
    ),
  );
}

class BusyButton extends StatelessWidget {
  final String label;
  final bool busy;
  final VoidCallback? onPressed;
  const BusyButton(
    this.label, {
    super.key,
    required this.onPressed,
    this.busy = false,
  });
  @override
  Widget build(BuildContext context) => SizedBox(
    width: double.infinity,
    child: FilledButton(
      onPressed: busy ? null : onPressed,
      child: busy
          ? const SizedBox(
              width: 20,
              height: 20,
              child: CircularProgressIndicator(strokeWidth: 2),
            )
          : Text(label),
    ),
  );
}

class ProductImage extends StatelessWidget {
  final dynamic url;
  final double size;
  const ProductImage(this.url, {super.key, this.size = 64});
  @override
  Widget build(BuildContext context) {
    final source = imageUrl(url);
    final fallback = Container(
      color: accent.withValues(alpha: .1),
      child: const Center(
        child: Icon(Icons.sports_esports_outlined, color: accent),
      ),
    );
    return ClipRRect(
      borderRadius: BorderRadius.circular(12),
      child: SizedBox(
        width: size,
        height: size,
        child: source.isEmpty
            ? fallback
            : Image.network(
                source,
                fit: BoxFit.cover,
                errorBuilder: (_, __, ___) => fallback,
              ),
      ),
    );
  }
}

class StateView extends StatelessWidget {
  final Object? error;
  final VoidCallback retry;
  const StateView({super.key, this.error, required this.retry});
  @override
  Widget build(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(28),
      child: error == null
          ? const CircularProgressIndicator()
          : Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.cloud_off_outlined, size: 44),
                const SizedBox(height: 16),
                Text('$error', textAlign: TextAlign.center),
                const SizedBox(height: 20),
                OutlinedButton(
                  onPressed: retry,
                  child: const Text('Повторить'),
                ),
              ],
            ),
    ),
  );
}

class AsyncPage extends StatefulWidget {
  final Future<Map<String, dynamic>> Function() load;
  final Widget Function(BuildContext, Map<String, dynamic>) builder;
  const AsyncPage({super.key, required this.load, required this.builder});
  @override
  State<AsyncPage> createState() => _AsyncPageState();
}

class _AsyncPageState extends State<AsyncPage> {
  Map<String, dynamic>? data;
  Object? error;
  int generation = 0;
  @override
  void initState() {
    super.initState();
    reload();
  }

  Future<void> reload() async {
    final id = ++generation;
    try {
      final result = await widget.load();
      if (mounted && generation == id) {
        setState(() {
          data = result;
          error = null;
        });
      }
    } catch (e) {
      if (mounted && generation == id) setState(() => error = e);
    }
  }

  @override
  Widget build(BuildContext context) => data == null
      ? StateView(error: error, retry: reload)
      : RefreshIndicator(
          onRefresh: reload,
          child: ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.all(18),
            children: [
              if (error != null) Surface(child: Text('$error')),
              widget.builder(context, data!),
            ],
          ),
        );
}

class InfoRow extends StatelessWidget {
  final String label, value;
  const InfoRow(this.label, this.value, {super.key});
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 9),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: Text(label, style: Theme.of(context).textTheme.bodySmall),
        ),
        const SizedBox(width: 12),
        Flexible(child: SelectableText(value, textAlign: TextAlign.end)),
      ],
    ),
  );
}

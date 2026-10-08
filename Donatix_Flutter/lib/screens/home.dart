import 'dart:math' as math;
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_svg/flutter_svg.dart';
import 'package:decimal/decimal.dart';
import '../core/api.dart';
import '../core/navigation.dart';
import '../widgets/ui.dart';
import '../widgets/site_design.dart';

class HomeScreen extends StatelessWidget {
  final DonatixApi api;
  final ValueChanged<int> navigate;
  const HomeScreen({super.key, required this.api, required this.navigate});

  @override
  Widget build(BuildContext context) => AsyncPage(
    load: () async {
      final me = await api.get('/api/v1/me');
      return {...me, ...await api.get('/api/v1/mobile/home')};
    },
    builder: (context, me) {
      final p = SiteColors(context);
      final login = text(me['login']);
      final summary = me['summary'] as Map;
      Widget tile(
        String label,
        String value,
        String note, {
        VoidCallback? tap,
      }) {
        final child = Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          decoration: BoxDecoration(
            color: p.surface,
            border: Border.all(color: p.line),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(label, style: TextStyle(fontSize: 14.4, color: p.muted)),
              const SizedBox(height: 2),
              Text(
                value,
                style: const TextStyle(
                  fontSize: 20.8,
                  fontWeight: FontWeight.w800,
                ),
              ),
              const SizedBox(height: 2),
              Text(note, style: TextStyle(fontSize: 12.8, color: p.muted)),
            ],
          ),
        );
        return tap == null ? child : SitePress(onTap: tap, child: child);
      }

      final active = me['status'] == 'active';
      final tier = text(me['tier']);
      const popularStyle = TextStyle(
        fontSize: 12.5,
        fontWeight: FontWeight.w600,
        height: 1.2,
      );
      var popularHeight = 132.0;
      for (final item in (me['popular'] as List).take(8)) {
        final label = TextPainter(
          text: TextSpan(
            text: text(item['title']),
            style: DefaultTextStyle.of(context).style.merge(popularStyle),
          ),
          textDirection: Directionality.of(context),
          textScaler: MediaQuery.textScalerOf(context),
        )..layout(maxWidth: 130);
        popularHeight = math.max(popularHeight, label.height + 86);
        label.dispose();
      }
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(2, 4, 2, 12),
            child: Text.rich(
              TextSpan(
                children: [
                  const TextSpan(text: 'Салом, '),
                  TextSpan(
                    text: login,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const TextSpan(text: ' 👋'),
                ],
              ),
              style: const TextStyle(fontSize: 18.4),
            ),
          ),
          if (me['low_balance'] == true ||
              Decimal.parse(text(me['balance'])) <
                  Decimal.parse(text(me['low_usd'] ?? '0')))
            Surface(
              child: Row(
                children: [
                  Icon(
                    Icons.account_balance_wallet_outlined,
                    color: p.semantic('warn'),
                  ),
                  const SizedBox(width: 12),
                  const Expanded(
                    child: Text(
                      'Баланс заканчивается. Пополните его перед покупкой.',
                    ),
                  ),
                  TextButton(
                    onPressed: () => navigate(2),
                    child: const Text('Пополнить'),
                  ),
                ],
              ),
            ),
          SiteReveal(
            child: Container(
              margin: const EdgeInsets.only(bottom: 16),
              padding: const EdgeInsets.all(18),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: Color.lerp(p.line, p.accent, .3)!),
                gradient: LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [p.accentSoft, p.surface],
                ),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    api.displayPrice(me['balance']),
                    style: const TextStyle(
                      fontSize: 30.4,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  Text('Доступно', style: TextStyle(color: p.muted)),
                  const SizedBox(height: 14),
                  SizedBox(
                    height: 44,
                    child: BusyButton(
                      'Пополнить',
                      onPressed: () => navigate(2),
                    ),
                  ),
                  const SizedBox(height: 14),
                  SiteGrid(
                    columns: 2,
                    breakpoint: 484,
                    gap: 10,
                    children: [
                      tile(
                        'Потрачено',
                        api.displayPrice(summary['totalSpent']),
                        'на выполненные заказы',
                      ),
                      tile(
                        'Выполнено заказов',
                        text(summary['totalOrders']),
                        'из ${me['orders_all']} всего',
                        tap: () => navigate(3),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
          if ((me['popular'] as List).isNotEmpty) ...[
            const Padding(
              padding: EdgeInsets.only(bottom: 10),
              child: Text(
                'Купить быстро',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
              ),
            ),
            SizedBox(
              height: popularHeight,
              child: ListView.separated(
                scrollDirection: Axis.horizontal,
                itemCount: math.min(8, (me['popular'] as List).length),
                separatorBuilder: (_, index) => const SizedBox(width: 10),
                itemBuilder: (c, i) {
                  final item = me['popular'][i] as Map;
                  return SizedBox(
                    width: 148,
                    child: SitePress(
                      onTap: () => openDonatixLink(
                        context,
                        api,
                        text(item['href']),
                        title: text(item['title']),
                      ),
                      child: Container(
                        margin: const EdgeInsets.only(bottom: 6),
                        padding: const EdgeInsets.symmetric(
                          horizontal: 8,
                          vertical: 12,
                        ),
                        decoration: BoxDecoration(
                          color: p.surface,
                          border: Border.all(color: p.line),
                          borderRadius: BorderRadius.circular(16),
                        ),
                        child: Column(
                          children: [
                            ClipRRect(
                              borderRadius: BorderRadius.circular(14),
                              child: ProductImage(item['image_url'], size: 48),
                            ),
                            const SizedBox(height: 6),
                            Text(
                              text(item['title']),
                              textAlign: TextAlign.center,
                              style: popularStyle,
                            ),
                          ],
                        ),
                      ),
                    ),
                  );
                },
              ),
            ),
            const SizedBox(height: 10),
          ],
          if (me['dcoin_enabled'] == true)
            Surface(
              padding: const EdgeInsets.all(14),
              child: SitePress(
                onTap: () => openDonatixLink(context, api, '/panel/dcoin'),
                child: Row(
                  children: [
                    Container(
                      width: 40,
                      height: 40,
                      alignment: Alignment.center,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        gradient: const LinearGradient(
                          colors: [Color(0xffffdf75), Color(0xfff59e0b)],
                        ),
                      ),
                      child: const Text(
                        'D',
                        style: TextStyle(
                          color: Color(0xff854d0e),
                          fontWeight: FontWeight.w800,
                          fontSize: 22,
                        ),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${me['dcoin']['balance_text']} D',
                            style: const TextStyle(fontWeight: FontWeight.w800),
                          ),
                          Text(
                            '≈ ${api.displayPrice(me['dcoin']['worth_usd'])} · 1 D = ${api.displayPrice(me['dcoin']['price'])}',
                            style: TextStyle(color: p.muted, fontSize: 12),
                          ),
                        ],
                      ),
                    ),
                    Text(
                      '${(num.tryParse(text(me['dcoin']['change'])) ?? 0) >= 0 ? '+' : ''}${me['dcoin']['change']}%',
                      style: TextStyle(
                        color: p.semantic(
                          (num.tryParse(text(me['dcoin']['change'])) ?? 0) >= 0
                              ? 'ok'
                              : 'bad',
                        ),
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const Icon(Icons.chevron_right, size: 18),
                  ],
                ),
              ),
            ),
          if (me['referral_enabled'] == true ||
              (num.tryParse(text(me['referral_percent'])) ?? 0) > 0)
            _ReferralBanner(
              onTap: () => openDonatixLink(context, api, '/panel/referrals'),
            ),
          Surface(
            padding: const EdgeInsets.all(18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    CircleAvatar(
                      radius: 20,
                      backgroundColor: p.accentSoft,
                      foregroundColor: p.accent,
                      child: Text(
                        login
                            .substring(0, math.min(2, login.length))
                            .toUpperCase(),
                        style: const TextStyle(fontWeight: FontWeight.w700),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            login,
                            style: const TextStyle(fontWeight: FontWeight.w700),
                          ),
                          Text(
                            text(me['email']),
                            style: TextStyle(color: p.muted),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 14),
                  child: Divider(height: 1),
                ),
                const Text(
                  'Быстрые действия',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                ),
                Text(
                  'Заказы, движения и поддержка',
                  style: TextStyle(color: p.muted),
                ),
                for (final e in [
                  ('/panel/orders', 'Заказы', 'История и статусы'),
                  ('/panel/transactions', 'Транзакции', 'Все движения баланса'),
                  ('/panel/stats', 'Аналитика', 'Расходы и заказы по дням'),
                  ('/panel/support', 'Поддержка', 'Написать в Telegram'),
                  ('/panel/logins', 'История входов', 'Данные и безопасность'),
                ])
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: SitePress(
                      onTap: () => openDonatixLink(context, api, e.$1),
                      child: Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 14,
                          vertical: 12,
                        ),
                        decoration: BoxDecoration(
                          border: Border.all(color: p.line),
                          borderRadius: BorderRadius.circular(12),
                          color: p.surface,
                        ),
                        child: Row(
                          children: [
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    e.$2,
                                    style: const TextStyle(
                                      fontWeight: FontWeight.w700,
                                      fontSize: 15.5,
                                    ),
                                  ),
                                  Text(
                                    e.$3,
                                    style: TextStyle(
                                      color: p.muted,
                                      fontSize: 13.1,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                            CircleAvatar(
                              radius: 14,
                              backgroundColor: p.surface2,
                              foregroundColor: p.muted,
                              child: const Icon(Icons.chevron_right, size: 16),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
              ],
            ),
          ),
          Container(
            margin: const EdgeInsets.only(bottom: 14),
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(14),
              border: Border.all(
                color: Color.lerp(
                  p.line,
                  p.semantic(active ? 'ok' : 'warn'),
                  .35,
                )!,
              ),
              gradient: LinearGradient(
                colors: [p.soft(active ? 'ok' : 'warn'), p.surface],
              ),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'УРОВЕНЬ',
                  style: TextStyle(
                    fontSize: 11.5,
                    color: p.muted,
                    letterSpacing: .7,
                  ),
                ),
                Text(
                  tier.isEmpty
                      ? ''
                      : '${tier[0].toUpperCase()}${tier.substring(1)}',
                  style: const TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 10),
                Row(
                  children: [
                    Icon(
                      active ? Icons.check_circle_outline : Icons.schedule,
                      color: p.semantic(active ? 'ok' : 'warn'),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            statusTitle(me['status']),
                            style: const TextStyle(fontWeight: FontWeight.w700),
                          ),
                          Text(
                            'Наценка к цене поставщика: ${me['markup']}%',
                            style: TextStyle(fontSize: 13, color: p.muted),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 10),
            child: Text(
              'АККАУНТ',
              style: TextStyle(
                fontSize: 11.5,
                color: p.muted,
                letterSpacing: .7,
              ),
            ),
          ),
          for (final row in [
            ('Email', text(me['email'])),
            ('Валюта счёта', 'USD'),
            ('Регистрация', text(me['createdAt'])),
            if (text(me['project']).isNotEmpty) ('Проект', text(me['project'])),
          ])
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 10,
                ),
                decoration: BoxDecoration(
                  color: p.surface,
                  border: Border.all(color: p.line),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(
                  children: [
                    Text(
                      row.$1,
                      style: TextStyle(color: p.muted, fontSize: 14.4),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Text(
                        row.$2,
                        textAlign: TextAlign.right,
                        style: const TextStyle(fontSize: 14.4),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          _HomeTimezone(api: api),
          const SizedBox(height: 14),
          _HomeApiCard(api: api),
        ],
      );
    },
  );
}

class _ReferralBanner extends StatefulWidget {
  final VoidCallback onTap;
  const _ReferralBanner({required this.onTap});
  @override
  State<_ReferralBanner> createState() => _ReferralBannerState();
}

class _ReferralBannerState extends State<_ReferralBanner>
    with TickerProviderStateMixin {
  late final clock = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 3200),
  );
  late final shine = AnimationController(
    vsync: this,
    duration: const Duration(seconds: 6),
  );
  late final twinkle = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 2400),
  );
  late final pulse = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 2200),
  );
  Timer? shineDelay;
  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (MediaQuery.disableAnimationsOf(context)) {
      for (final c in [clock, shine, twinkle, pulse]) {
        c.stop();
        c.value = 0;
      }
      shineDelay?.cancel();
      shineDelay = null;
    } else if (!clock.isAnimating) {
      clock.repeat();
      twinkle.repeat();
      pulse.repeat();
      shineDelay ??= Timer(const Duration(milliseconds: 1200), () {
        if (mounted && !MediaQuery.disableAnimationsOf(context)) {
          shine.repeat();
        }
      });
    }
  }

  @override
  void dispose() {
    shineDelay?.cancel();
    for (final c in [clock, shine, twinkle, pulse]) {
      c.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final p = SiteColors(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 18),
      child: SitePress(
        onTap: widget.onTap,
        child: SiteShine(
          clock: shine,
          child: Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(20),
              border: Border.all(color: Color.lerp(p.line, p.accent, .35)!),
              gradient: RadialGradient(
                center: Alignment.topLeft,
                radius: 1.5,
                colors: [Color.lerp(p.surface, p.accent, .22)!, p.surface],
              ),
            ),
            child: Column(
              children: [
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Container(
                      width: 56,
                      height: 56,
                      alignment: Alignment.center,
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(16),
                        gradient: LinearGradient(
                          colors: [p.accent, const Color(0xffa855f7)],
                        ),
                      ),
                      child: AnimatedBuilder(
                        animation: clock,
                        child: AnimatedBuilder(
                          animation: twinkle,
                          builder: (c, _) => CustomPaint(
                            foregroundPainter: _GiftSparkPainter(
                              twinkle.value,
                              MediaQuery.disableAnimationsOf(context),
                            ),
                            child: SvgPicture.asset(
                              'assets/ref-gift-base.svg',
                              width: 38,
                              height: 38,
                            ),
                          ),
                        ),
                        builder: (c, child) {
                          final t = clock.value;
                          final bob = t < .3
                              ? t / .3
                              : t < .6
                              ? 1 - (t - .3) / .3
                              : t < .8
                              ? (t - .6) / .2 / 3
                              : (1 - t) / .2 / 3;
                          final angle = t < .6 ? -.087 * bob : .07 * bob;
                          return Transform.translate(
                            offset: Offset(0, -3 * bob),
                            child: Transform.rotate(angle: angle, child: child),
                          );
                        },
                      ),
                    ),
                    const SizedBox(width: 14),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Wrap(
                            spacing: 8,
                            crossAxisAlignment: WrapCrossAlignment.center,
                            children: [
                              AnimatedBuilder(
                                animation: pulse,
                                builder: (c, _) => Container(
                                  padding: const EdgeInsets.symmetric(
                                    horizontal: 8,
                                    vertical: 2,
                                  ),
                                  decoration: BoxDecoration(
                                    color: const Color(0xfffcd34d),
                                    borderRadius: BorderRadius.circular(999),
                                    boxShadow:
                                        MediaQuery.disableAnimationsOf(context)
                                        ? []
                                        : [
                                            BoxShadow(
                                              color: const Color(0xfff59e0b)
                                                  .withValues(
                                                    alpha:
                                                        .45 *
                                                        (1 -
                                                            _glow(pulse.value)),
                                                  ),
                                              spreadRadius:
                                                  6 * _glow(pulse.value),
                                            ),
                                          ],
                                  ),
                                  child: const Text(
                                    'НАВ',
                                    style: TextStyle(
                                      color: Color(0xff3a2606),
                                      fontSize: 10.5,
                                      fontWeight: FontWeight.w800,
                                    ),
                                  ),
                                ),
                              ),
                              Text(
                                'БАРНОМАИ РЕФЕРАЛӢ',
                                style: TextStyle(
                                  fontSize: 12,
                                  color: p.muted,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                            ],
                          ),
                          const SizedBox(height: 4),
                          const Text(
                            'Дӯстонро даъват кунед — бонус гиред',
                            style: TextStyle(
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                              height: 1.3,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            'Аз ҳар фармоиши онҳо бонус — то абад, рост ба ҳисобатон',
                            style: TextStyle(
                              fontSize: 14,
                              height: 1.45,
                              color: p.muted,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 14),
                Container(
                  width: double.infinity,
                  alignment: Alignment.center,
                  padding: const EdgeInsets.symmetric(
                    horizontal: 18,
                    vertical: 13,
                  ),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(12),
                    gradient: LinearGradient(
                      colors: [p.accent, const Color(0xffa855f7)],
                    ),
                  ),
                  child: const Text(
                    'Ҳавола гирифтан →',
                    style: TextStyle(
                      color: Colors.white,
                      fontSize: 15,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

double _glow(double value) =>
    Curves.easeInOut.transform(value <= .6 ? value / .6 : (1 - value) / .4);

class _GiftSparkPainter extends CustomPainter {
  final double time;
  final bool reduced;
  _GiftSparkPainter(this.time, this.reduced);
  @override
  void paint(Canvas canvas, Size size) {
    canvas.scale(size.width / 48, size.height / 48);
    for (var i = 0; i < 2; i++) {
      final phase = (time + i * .5) % 1;
      final value = reduced
          ? 1.0
          : Curves.easeInOut.transform(
              phase <= .5 ? phase * 2 : (1 - phase) * 2,
            );
      canvas.save();
      canvas.translate(i == 0 ? 39 : 8, i == 0 ? 9 : 11);
      canvas.scale(.5 + .5 * value);
      final r = i == 0 ? 4.0 : 3.0, inner = i == 0 ? 1.1 : .8;
      final path = Path()
        ..moveTo(0, -r)
        ..lineTo(inner, -inner)
        ..lineTo(r, 0)
        ..lineTo(inner, inner)
        ..lineTo(0, r)
        ..lineTo(-inner, inner)
        ..lineTo(-r, 0)
        ..lineTo(-inner, -inner)
        ..close();
      canvas.drawPath(
        path,
        Paint()..color = Colors.white.withValues(alpha: .2 + .8 * value),
      );
      canvas.restore();
    }
  }

  @override
  bool shouldRepaint(_GiftSparkPainter oldDelegate) =>
      time != oldDelegate.time || reduced != oldDelegate.reduced;
}

class _HomeTimezone extends StatefulWidget {
  final DonatixApi api;
  const _HomeTimezone({required this.api});
  @override
  State<_HomeTimezone> createState() => _HomeTimezoneState();
}

class _HomeTimezoneState extends State<_HomeTimezone> {
  late Future<Map<String, dynamic>> data = widget.api.get(
    '/api/v1/mobile/timezone',
  );
  bool busy = false;
  @override
  Widget build(BuildContext context) => FutureBuilder<Map<String, dynamic>>(
    future: data,
    builder: (c, snap) {
      if (snap.hasError) {
        return TextButton(
          onPressed: () =>
              setState(() => data = widget.api.get('/api/v1/mobile/timezone')),
          child: const Text('Повторить загрузку часового пояса'),
        );
      }
      if (!snap.hasData) {
        return const LinearProgressIndicator();
      }
      final d = snap.data!;
      return DropdownButtonFormField<String>(
        key: ValueKey(d['choice']),
        initialValue: text(d['choice']),
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
        onChanged: busy
            ? null
            : (value) async {
                if (value == null) {
                  return;
                }
                setState(() => busy = true);
                try {
                  await widget.api.post('/api/v1/mobile/timezone', {
                    'timezone': value,
                  });
                } catch (e) {
                  if (context.mounted) {
                    message(context, e);
                  }
                } finally {
                  if (mounted) {
                    setState(() {
                      busy = false;
                      data = widget.api.get('/api/v1/mobile/timezone');
                    });
                  }
                }
              },
      );
    },
  );
}

class _HomeApiCard extends StatefulWidget {
  final DonatixApi api;
  const _HomeApiCard({required this.api});
  @override
  State<_HomeApiCard> createState() => _HomeApiCardState();
}

class _HomeApiCardState extends State<_HomeApiCard>
    with WidgetsBindingObserver {
  late Future<Map<String, dynamic>> data = widget.api.get(
    '/api/v1/mobile/keys',
  );
  String? shown;
  bool busy = false;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    shown = null;
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed && mounted) {
      setState(() => shown = null);
    }
  }

  Future<void> reveal(Map key) async {
    if (shown != null) {
      setState(() => shown = null);
      return;
    }
    if (busy) {
      return;
    }
    setState(() => busy = true);
    try {
      final d = await widget.api.post(
        '/api/v1/mobile/keys/${key['id']}/reveal',
        {},
      );
      if (mounted) {
        setState(() => shown = text(d['key']));
      }
    } catch (e) {
      if (mounted) {
        message(context, e);
      }
    } finally {
      if (mounted) {
        setState(() => busy = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) => Surface(
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'API ключ',
          style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
        ),
        const Text('Ключ для подключения бота или сайта.'),
        const SizedBox(height: 12),
        FutureBuilder<Map<String, dynamic>>(
          future: data,
          builder: (c, snap) {
            if (snap.hasError) {
              return TextButton(
                onPressed: () => setState(
                  () => data = widget.api.get('/api/v1/mobile/keys'),
                ),
                child: const Text('Повторить загрузку ключа'),
              );
            }
            if (!snap.hasData) {
              return const LinearProgressIndicator();
            }
            final keys = (snap.data!['items'] as List)
                .where((k) => k['revoked_at'] == null)
                .toList();
            if (keys.isEmpty) {
              if (snap.data!['active'] != true) {
                return const Text(
                  'Ключ можно создать после активации аккаунта.',
                );
              }
              return FilledButton.icon(
                icon: const Icon(Icons.add, size: 18),
                label: const Text('Создать ключ'),
                onPressed: busy
                    ? null
                    : () async {
                        setState(() => busy = true);
                        try {
                          final d = await widget.api.post(
                            '/api/v1/mobile/keys',
                            {'name': 'Основной'},
                          );
                          if (mounted) {
                            setState(() {
                              shown = text(d['key']);
                              data = widget.api.get('/api/v1/mobile/keys');
                            });
                          }
                        } catch (e) {
                          if (context.mounted) {
                            message(context, e);
                          }
                        } finally {
                          if (mounted) {
                            setState(() => busy = false);
                          }
                        }
                      },
              );
            }
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: SelectableText(
                        shown ?? '•••••••••••',
                        style: const TextStyle(fontFamily: 'monospace'),
                      ),
                    ),
                    IconButton(
                      tooltip: shown == null ? 'Показать ключ' : 'Скрыть ключ',
                      onPressed: busy ? null : () => reveal(keys.first as Map),
                      icon: Icon(
                        shown == null
                            ? Icons.visibility_outlined
                            : Icons.visibility_off_outlined,
                      ),
                    ),
                    if (shown != null)
                      IconButton(
                        tooltip: 'Скопировать',
                        onPressed: () async {
                          await Clipboard.setData(ClipboardData(text: shown!));
                          if (context.mounted) {
                            message(context, 'Ключ скопирован.');
                          }
                        },
                        icon: const Icon(Icons.copy, size: 18),
                      ),
                  ],
                ),
                Text(
                  keys.first['last_used_at'] == null
                      ? 'Ещё не использовался.'
                      : 'Последнее использование: ${keys.first['last_used_at']}.',
                ),
              ],
            );
          },
        ),
        OutlinedButton.icon(
          onPressed: () => openDonatixLink(context, widget.api, '/panel/api'),
          icon: const Icon(Icons.key, size: 18),
          label: const Text('Управление ключами'),
        ),
        const Divider(height: 28),
        const Text(
          'Документация API',
          style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
        ),
        const Text('Методы, примеры запросов и Swagger.'),
        OutlinedButton(
          onPressed: () => openDonatixLink(context, widget.api, '/docs'),
          child: const Text('Открыть документацию'),
        ),
      ],
    ),
  );
}

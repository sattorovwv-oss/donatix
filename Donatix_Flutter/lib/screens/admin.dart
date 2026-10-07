import 'dart:async';
import 'dart:math' as math;
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_svg/flutter_svg.dart';
import 'package:html/dom.dart' as dom;
import 'package:html/parser.dart' as html;
import '../core/api.dart';
import '../core/navigation.dart';
import '../widgets/site_design.dart';
import '../widgets/ui.dart';
import 'document.dart';
import 'management.dart';

/// Original authenticated routes are the data/action transport. Every admin
/// page has a native layout below; the old DocumentScreen is not used for it.
class AdminScreen extends StatefulWidget {
  final DonatixApi api;
  final String path;
  const AdminScreen({super.key, required this.api, this.path = '/admin'});
  @override
  State<AdminScreen> createState() => _AdminScreenState();
}

enum AdminLayout {
  dashboard,
  listing,
  detail,
  analytics,
  finance,
  settings,
  bots,
  sync,
  traffic,
}

class _AdminScreenState extends State<AdminScreen> {
  late String path = widget.path;
  dom.Document? data;
  Object? error;
  Timer? timer;
  Map<String, dynamic>? job;
  final scroll = ScrollController();
  final anchors = <String, GlobalKey>{};
  int generation = 0;
  String? loadedPath;
  bool loading = false;
  @override
  void initState() {
    super.initState();
    load();
  }

  @override
  void dispose() {
    timer?.cancel();
    scroll.dispose();
    super.dispose();
  }

  String get route => Uri.parse(path).path;
  AdminLayout get page => switch (route) {
    '/admin' || '/admin/' => AdminLayout.dashboard,
    '/admin/users' ||
    '/admin/orders' ||
    '/admin/products' ||
    '/admin/payments' ||
    '/admin/errors' => AdminLayout.listing,
    '/admin/stats' => AdminLayout.analytics,
    '/admin/traffic' => AdminLayout.traffic,
    '/admin/money' || '/admin/finance' => AdminLayout.finance,
    '/admin/settings' || '/admin/pay-settings' => AdminLayout.settings,
    '/admin/bots' || '/admin/shopbot' => AdminLayout.bots,
    '/admin/catalog-sync' => AdminLayout.sync,
    _ => AdminLayout.detail,
  };
  SiteColors get colors => SiteColors(context);
  bool allowed(Uri uri) {
    final root = Uri.parse(DonatixApi.origin);
    return uri.scheme == root.scheme &&
        uri.host == root.host &&
        uri.port == root.port &&
        uri.userInfo.isEmpty;
  }

  Future<void> load([Response<dynamic>? initial]) async {
    final id = ++generation;
    timer?.cancel();
    setState(() {
      error = null;
      loading = true;
      if (path != loadedPath || initial != null) data = null;
      job = null;
      anchors.clear();
    });
    try {
      Response<dynamic>? r = initial;
      if (r != null) path = r.realUri.toString();
      for (var i = 0; i < 6; i++) {
        final uri = Uri.parse(DonatixApi.origin).resolve(path);
        if (!allowed(uri)) throw const ApiFailure('Недоступный адрес.');
        r ??= await widget.api.dio.get<dynamic>(
          uri.toString(),
          options: Options(responseType: ResponseType.bytes),
        );
        if ([301, 302, 303, 307, 308].contains(r.statusCode)) {
          final target = r.headers.value('location');
          if (target == null) {
            throw const ApiFailure('Не удалось открыть страницу.');
          }
          path = uri.resolve(target).toString();
          if (Uri.parse(path).path == '/login') {
            widget.api.onSessionExpired?.call();
            throw const ApiFailure('Войдите снова.', 401);
          }
          r = null;
          continue;
        }
        break;
      }
      if (r == null || (r.statusCode ?? 500) >= 400) {
        throw ApiFailure('Страница недоступна.', r?.statusCode);
      }
      if (!(r.headers.value('content-type') ?? '').contains('text/html')) {
        if (mounted) {
          await Navigator.push(
            context,
            MaterialPageRoute<void>(
              builder: (_) => DocumentScreen(
                api: widget.api,
                path: path,
                title: 'Файл Donatix',
              ),
            ),
          );
        }
        return;
      }
      final doc = html.parse(
        await widget.api.decodeHtml(List<int>.from(r.data as List)),
      );
      final csrf = doc.querySelector('input[name="csrf"]')?.attributes['value'];
      if (csrf != null) widget.api.csrf = csrf;
      if (!mounted || id != generation) return;
      setState(() {
        data = doc;
        loadedPath = path;
      });
      if (route == '/admin/catalog-sync') poll();
    } catch (e) {
      if (mounted && generation == id) setState(() => error = e);
    } finally {
      if (mounted && generation == id) setState(() => loading = false);
    }
  }

  Future<void> poll() async {
    final id = generation;
    timer?.cancel();
    try {
      final d = await widget.api.get('/admin/catalog-sync/status');
      if (mounted && id == generation && route == '/admin/catalog-sync') {
        setState(() {
          job = d;
          for (final button
              in data?.querySelectorAll('.sync-btn') ?? <dom.Element>[]) {
            if (d['running'] == true) {
              button.attributes['disabled'] = '';
            } else {
              button.attributes.remove('disabled');
            }
          }
          final local = data?.querySelector('#k-local');
          if (local != null) local.text = text(d['images_local']);
        });
      }
      if (d['running'] == true && mounted && id == generation) {
        timer = Timer(const Duration(milliseconds: 1500), poll);
      }
    } catch (_) {
      if (mounted && id == generation && route == '/admin/catalog-sync') {
        timer = Timer(const Duration(seconds: 4), poll);
      }
    }
  }

  Future<void> link(String value) async {
    if (value.startsWith('#')) {
      final c = anchors[value.substring(1)]?.currentContext;
      if (c != null) {
        await Scrollable.ensureVisible(
          c,
          duration: const Duration(milliseconds: 250),
        );
      }
      return;
    }
    final uri = Uri.parse(DonatixApi.origin).resolve(path).resolve(value);
    if (allowed(uri) &&
        uri.path.startsWith('/admin') &&
        uri.path != '/admin/pricelist' &&
        !uri.path.endsWith('/receipt') &&
        !uri.path.endsWith('/log') &&
        !uri.path.endsWith('.csv')) {
      path = uri.toString();
      if (scroll.hasClients) scroll.jumpTo(0);
      await load();
    } else if (mounted) {
      await openDonatixLink(context, widget.api, uri.toString());
    }
  }

  Widget vertical(Iterable<Widget> children, {double gap = 8}) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      for (final w in children)
        Padding(
          padding: EdgeInsets.only(bottom: gap),
          child: w,
        ),
    ],
  );
  String kind(dom.Element e) => e.classes.firstWhere(
    (c) => [
      'ok',
      'bad',
      'error',
      'warn',
      'good',
      'danger',
      'g2',
      'g3',
      'g4',
      'g5',
      'plus',
      'minus',
      'c-done',
      'c-ref',
      'c-created',
    ].contains(c),
    orElse: () => 'accent',
  );
  TextStyle style(dom.Element e) => TextStyle(
    color:
        e.classes.contains('muted') ||
            ['label', 'note', 'sub', 'hint'].any(e.classes.contains)
        ? colors.muted
        : [
            'ok',
            'bad',
            'error',
            'warn',
            'good',
            'plus',
            'minus',
            'c-done',
            'c-ref',
            'c-created',
          ].any(e.classes.contains)
        ? colors.semantic(kind(e))
        : colors.ink,
    fontSize: ['small', 'note', 'sub', 'hint'].any(e.classes.contains)
        ? 13
        : 15,
    fontWeight:
        ['b', 'strong'].contains(e.localName) || e.classes.contains('amount')
        ? FontWeight.w600
        : FontWeight.w400,
    height: 1.55,
  );
  Widget heading(dom.Element e) => Text(
    e.text.trim(),
    style: TextStyle(
      fontSize: switch (e.localName) {
        'h1' =>
          (MediaQuery.sizeOf(context).width * .03).clamp(21.6, 28).toDouble(),
        'h2' =>
          (MediaQuery.sizeOf(context).width * .02)
              .clamp(17.28, 20.8)
              .toDouble(),
        _ => 16,
      },
      fontWeight: FontWeight.w700,
      height: 1.2,
      color: colors.ink,
    ),
  );
  Widget icon(dom.Element e, {double size = 20, Color? color}) {
    var svg = e.outerHtml.replaceAll(
      'currentColor',
      '#${(color ?? colors.semantic(kind(e.parent ?? e))).toARGB32().toRadixString(16).substring(2)}',
    );
    svg = svg.replaceAll('viewbox=', 'viewBox=');
    return SvgPicture.string(svg, width: size, height: size);
  }

  Widget badge(dom.Element e) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
    decoration: BoxDecoration(
      color: colors.soft(kind(e)),
      borderRadius: BorderRadius.circular(999),
    ),
    child: Text(
      e.text.trim(),
      style: TextStyle(
        fontSize: 12.8,
        color: colors.semantic(kind(e)),
        fontWeight: FontWeight.w600,
      ),
    ),
  );
  Widget kpi(dom.Element e) {
    final tile = e.querySelector('.tile');
    final label = e.querySelector('.label')?.text.trim() ?? '';
    final value = e.querySelector('.value')?.text.trim() ?? '';
    final child = SiteCard(
      child: Stack(
        children: [
          Padding(
            padding: EdgeInsets.only(right: tile == null ? 0 : 44),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label,
                  style: TextStyle(
                    color: colors.muted,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 10),
                Text(
                  value,
                  style: TextStyle(
                    fontSize: e.classes.contains('scard')
                        ? (MediaQuery.sizeOf(context).width * .045)
                              .clamp(20.8, 28.8)
                              .toDouble()
                        : 20,
                    fontWeight: FontWeight.w700,
                    color: colors.ink,
                  ),
                ),
                for (final note in e.querySelectorAll('.note'))
                  Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Text(note.text.trim(), style: style(note)),
                  ),
                for (final form in e.querySelectorAll('form')) node(form),
              ],
            ),
          ),
          if (tile != null)
            Positioned(
              right: 0,
              top: 0,
              child: Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  color: colors.soft(kind(tile)),
                  borderRadius: BorderRadius.circular(12),
                ),
                alignment: Alignment.center,
                child: tile.querySelector('svg') == null
                    ? Icon(Icons.auto_graph, color: colors.semantic(kind(tile)))
                    : icon(tile.querySelector('svg')!),
              ),
            ),
        ],
      ),
    );
    return e.localName == 'a'
        ? InkWell(
            onTap: () => link(e.attributes['href'] ?? ''),
            borderRadius: BorderRadius.circular(16),
            child: child,
          )
        : child;
  }

  Widget table(dom.Element e) {
    final rows = e.querySelectorAll('tr');
    int span(dom.Element cell) =>
        (int.tryParse(cell.attributes['colspan'] ?? '') ?? 1).clamp(1, 64);
    final count = rows.fold<int>(
      0,
      (value, row) => math.max(
        value,
        row.children.fold<int>(0, (sum, cell) => sum + span(cell)),
      ),
    );
    if (count == 0) return const SizedBox.shrink();
    final widths = List<double>.filled(count, 96);
    for (final row in rows) {
      var index = 0;
      for (final cell in row.children) {
        if (span(cell) == 1) {
          widths[index] = math.max(
            widths[index],
            (cell.text.trim().length * 6.5 + 24).clamp(96, 300).toDouble(),
          );
        }
        index += span(cell);
      }
    }
    Widget cells(dom.Element row) {
      var index = 0;
      final widgets = <Widget>[];
      for (final cell in row.children) {
        final end = index + span(cell);
        final width = widths
            .sublist(index, end)
            .fold<double>(0, (sum, n) => sum + n);
        widgets.add(
          SizedBox(
            width: width,
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 8),
              child: cell.localName == 'th'
                  ? Text(
                      cell.text.trim().toUpperCase(),
                      textAlign: cell.classes.contains('num')
                          ? TextAlign.end
                          : TextAlign.start,
                      style: TextStyle(
                        fontSize: 11.8,
                        letterSpacing: .6,
                        color: colors.muted,
                        fontWeight: FontWeight.w600,
                      ),
                    )
                  : Align(
                      alignment: cell.classes.contains('num')
                          ? Alignment.centerRight
                          : Alignment.centerLeft,
                      child: cell.children.isEmpty
                          ? SelectableText(cell.text.trim(), style: style(cell))
                          : vertical(cell.nodes.map(node), gap: 2),
                    ),
            ),
          ),
        );
        index = end;
      }
      if (index < count) {
        widgets.add(
          SizedBox(
            width: widths.sublist(index).fold<double>(0, (sum, n) => sum + n),
          ),
        );
      }
      return Row(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: widgets,
      );
    }

    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: SizedBox(
        width: widths.fold<double>(0, (sum, n) => sum + n),
        child: Column(
          children: [
            for (final row in rows)
              Container(
                decoration: BoxDecoration(
                  border: Border(bottom: BorderSide(color: colors.line)),
                ),
                child: Opacity(
                  opacity:
                      (double.tryParse(
                                RegExp(r'opacity:\s*([\d.]+)')
                                        .firstMatch(
                                          row.attributes['style'] ?? '',
                                        )
                                        ?.group(1) ??
                                    '',
                              ) ??
                              1)
                          .clamp(0, 1),
                  child: cells(row),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget chart(dom.Element e) {
    final bars = e.querySelectorAll('.bar');
    return SizedBox(
      height: 180,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          for (final bar in bars)
            Expanded(
              child: Tooltip(
                message: bar.attributes['title'] ?? '',
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 3),
                  child: TweenAnimationBuilder<double>(
                    tween: Tween(begin: 0, end: 1),
                    duration: MediaQuery.disableAnimationsOf(context)
                        ? Duration.zero
                        : const Duration(milliseconds: 550),
                    curve: siteEase,
                    builder: (c, t, _) {
                      double percent(String? s, String name) =>
                          double.tryParse(
                            RegExp(
                                  '$name:\\s*([0-9.]+)',
                                ).firstMatch(s ?? '')?.group(1) ??
                                '',
                          ) ??
                          0;
                      final stack = bar.querySelector('.stack');
                      return Column(
                        mainAxisAlignment: MainAxisAlignment.end,
                        children: [
                          Container(
                            width: 28,
                            height: math.max(
                              2,
                              percent(stack?.attributes['style'], '--h') *
                                  1.55 *
                                  t,
                            ),
                            decoration: BoxDecoration(
                              color: colors.accent.withValues(alpha: .2),
                              borderRadius: const BorderRadius.vertical(
                                top: Radius.circular(5),
                              ),
                            ),
                            alignment: Alignment.bottomCenter,
                            child: FractionallySizedBox(
                              heightFactor:
                                  (percent(
                                            stack
                                                ?.querySelector('.pro')
                                                ?.attributes['style'],
                                            '--p',
                                          ) /
                                          100)
                                      .clamp(0, 1),
                              widthFactor: 1,
                              child: ColoredBox(color: colors.accent),
                            ),
                          ),
                          const SizedBox(height: 6),
                          Text(
                            bar.querySelector('.day')?.text ?? '',
                            style: TextStyle(fontSize: 10, color: colors.muted),
                          ),
                        ],
                      );
                    },
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget svgChart(dom.Element wrapper) {
    final svg = wrapper.querySelector('svg')!;
    final copy = svg.clone(true);
    String hex(Color c) => '#${c.toARGB32().toRadixString(16).substring(2)}';
    for (final item in copy.querySelectorAll('*')) {
      for (final key in item.attributes.keys.toList()) {
        item.attributes[key] = item.attributes[key]!
            .replaceAll('var(--c-created)', hex(colors.accent))
            .replaceAll('var(--c-done)', hex(colors.semantic('ok')))
            .replaceAll('var(--c-ref)', hex(colors.semantic('bad')));
      }
      if (item.classes.contains('gridline')) {
        item.attributes['stroke'] = hex(colors.line);
        item.attributes['stroke-width'] = '1';
      }
      if (item.classes.contains('ln')) {
        item.attributes['fill'] = 'none';
        item.attributes['stroke-width'] = '2.5';
        item.attributes['stroke'] = hex(
          item.classes.contains('c-done')
              ? colors.semantic('ok')
              : item.classes.contains('c-ref')
              ? colors.semantic('bad')
              : colors.accent,
        );
      }
    }
    return Padding(
      padding: const EdgeInsets.only(bottom: 24, left: 34),
      child: LayoutBuilder(
        builder: (c, b) => SizedBox(
          height: 260,
          child: Stack(
            clipBehavior: Clip.none,
            children: [
              Positioned.fill(
                child: SvgPicture.string(
                  copy.outerHtml.replaceAll('viewbox=', 'viewBox='),
                  fit: BoxFit.fill,
                ),
              ),
              for (final label in wrapper.querySelectorAll('.ax'))
                Builder(
                  builder: (_) {
                    final s = label.attributes['style'] ?? '';
                    final n =
                        double.tryParse(
                          RegExp(r'([0-9.]+)%').firstMatch(s)?.group(1) ?? '',
                        ) ??
                        0;
                    final ratio =
                        double.tryParse(
                          RegExp(r'\*\s*([0-9.]+)').firstMatch(s)?.group(1) ??
                              '',
                        ) ??
                        0;
                    return Positioned(
                      left: label.classes.contains('y')
                          ? -34
                          : (b.maxWidth * ratio)
                                .clamp(0, math.max(0, b.maxWidth - 24))
                                .toDouble(),
                      top: label.classes.contains('y')
                          ? 260 * n / 100 - 8
                          : 264,
                      child: Text(
                        label.text.trim(),
                        style: TextStyle(color: colors.muted, fontSize: 11),
                      ),
                    );
                  },
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget jobView() {
    final d = job!;
    final stages = {
      'start': 'Запускаю…',
      'catalog': 'Загружаю каталог у поставщика',
      'images': 'Скачиваю картинки',
      'done': 'Готово',
      'error': 'Ошибка',
    };
    final running = d['running'] == true;
    final done = (d['images_done'] as num? ?? 0).toDouble(),
        total = (d['images_total'] as num? ?? 0).toDouble();
    return SiteCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'Ход загрузки',
            style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 14),
          Text(
            '${stages[d['stage']] ?? 'Ещё не запускали.'} ${d['category'] ?? ''}',
          ),
          const SizedBox(height: 12),
          LinearProgressIndicator(
            value: running && total == 0
                ? null
                : total == 0
                ? d['stage'] == 'done'
                      ? 1
                      : 0
                : (done / total).clamp(0, 1),
          ),
          InfoRow(
            'Товаров',
            text(d['products'] ?? (d['result'] as Map?)?['products'] ?? 0),
          ),
          InfoRow('Картинок', '${done.toInt()} / ${total.toInt()}'),
          InfoRow('Ошибок', text(d['images_failed'] ?? 0)),
          InfoRow('Время', '${d['seconds'] ?? 0} с'),
          if ((d['error'] ?? '').toString().isNotEmpty)
            Text(
              text(d['error']),
              style: TextStyle(color: colors.semantic('bad')),
            ),
          if ((d['log'] as List? ?? []).isNotEmpty)
            SelectableText(
              (d['log'] as List).join('\n'),
              style: const TextStyle(fontSize: 12),
            ),
        ],
      ),
    );
  }

  Widget formLayout(dom.Element e, List<Widget> children) {
    if (e.classes.contains('filters')) {
      final parts = e.nodes
          .where((n) => n is! dom.Text || n.text.trim().isNotEmpty)
          .toList();
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (var i = 0; i < parts.length; i++)
            if (parts[i] is dom.Element &&
                (parts[i] as dom.Element).classes.contains('full'))
              Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: children[i],
              ),
          SiteGrid(
            columns: 2,
            breakpoint: 480,
            gap: 10,
            children: [
              for (var i = 0; i < parts.length; i++)
                if (parts[i] is! dom.Element ||
                    !(parts[i] as dom.Element).classes.contains('full'))
                  children[i],
            ],
          ),
        ],
      );
    }
    if (e.classes.contains('sync-actions')) {
      return SiteGrid(minimum: 220, gap: 10, children: children);
    }
    if (e.classes.contains('pm-grid')) {
      return LayoutBuilder(
        builder: (c, b) => b.maxWidth <= 480 || children.length != 2
            ? vertical(children, gap: 12)
            : Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(flex: 2, child: children[0]),
                  const SizedBox(width: 12),
                  Expanded(child: children[1]),
                ],
              ),
      );
    }
    if (e.classes.contains('pm-head')) {
      return Wrap(
        spacing: 10,
        runSpacing: 8,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: children,
      );
    }
    if (e.classes.contains('grid') || e.classes.contains('filters')) {
      return SiteGrid(
        minimum: e.classes.contains('three')
            ? 250
            : e.classes.contains('two')
            ? 320
            : 240,
        gap: 12,
        children: children,
      );
    }
    if (e.classes.contains('card')) {
      return SiteCard(child: vertical(children, gap: 8));
    }
    if (e.classes.contains('list') || e.classes.contains('pay-form')) {
      return vertical(children, gap: 14);
    }
    if (e.classes.contains('flash')) {
      return Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: colors.soft(kind(e)),
          borderRadius: BorderRadius.circular(12),
        ),
        child: vertical(children),
      );
    }
    if (e.classes.contains('inline') || e.classes.contains('bot-actions')) {
      return Wrap(spacing: 8, runSpacing: 8, children: children);
    }
    return vertical(children, gap: 4);
  }

  Widget node(dom.Node n) {
    if (n is dom.Text) {
      return n.text.trim().isEmpty
          ? const SizedBox.shrink()
          : Text(
              n.text.trim(),
              style: TextStyle(color: colors.ink, height: 1.55),
            );
    }
    if (n is! dom.Element ||
        n.attributes.containsKey('hidden') ||
        [
          'script',
          'style',
          'noscript',
          'input',
          'meta',
        ].contains(n.localName)) {
      return const SizedBox.shrink();
    }
    Widget child;
    if (n.id == 'job' && job != null) {
      child = jobView();
    } else if (n.localName == 'form') {
      child = NativeForm(
        key: ValueKey('$path:${n.outerHtml}'),
        api: widget.api,
        form: n,
        sourcePath: path,
        render: node,
        completed: load,
        layout: formLayout,
        bare: true,
      );
    } else if (['kpi', 'scard', 'pstat', 'stat'].any(n.classes.contains)) {
      child = kpi(n);
    } else if (n.localName == 'svg') {
      child = icon(n);
    } else if (n.localName == 'table') {
      child = table(n);
    } else if (n.classes.contains('vbars') || n.classes.contains('hbars')) {
      child = trafficBars(n);
    } else if (n.classes.contains('vbars-x') || n.classes.contains('hbars-x')) {
      child = Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          for (final label in n.children)
            Expanded(
              child: Text(
                label.text.trim(),
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 10.9, color: colors.muted),
              ),
            ),
        ],
      );
    } else if (n.classes.contains('fn-row')) {
      child = funnelRow(n);
    } else if (n.classes.contains('fin-bar')) {
      child = financialBar(n);
    } else if (n.classes.contains('fin-rows')) {
      child = vertical(
        n.children.map(
          (row) => Container(
            padding: const EdgeInsets.symmetric(vertical: 12),
            decoration: BoxDecoration(
              border: Border(bottom: BorderSide(color: colors.line)),
            ),
            child: Row(
              children: [
                for (final cell in row.children) Expanded(child: node(cell)),
              ],
            ),
          ),
        ),
      );
    } else if (n.classes.contains('kinds-head') ||
        n.classes.contains('kr-top')) {
      child = Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (var i = 0; i < n.children.length; i++)
            if (i == 0)
              Expanded(child: node(n.children[i]))
            else
              SizedBox(
                width: 52,
                child: Align(
                  alignment: Alignment.centerRight,
                  child: node(n.children[i]),
                ),
              ),
        ],
      );
    } else if (n.classes.contains('kr-sum')) {
      child = Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: n.children.map(node).toList(),
      );
    } else if (n.classes.contains('kind-row') ||
        n.classes.contains('ins') ||
        n.classes.contains('fin-item')) {
      child = Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(
          color: n.classes.contains('profit')
              ? colors.soft('ok')
              : colors.surface2,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: colors.line),
        ),
        child: vertical(n.nodes.map(node), gap: 4),
      );
    } else if (n.classes.contains('ins-grid')) {
      child = SiteGrid(
        minimum: 200,
        gap: 12,
        children: n.children.map(node).toList(),
      );
    } else if (n.classes.contains('online')) {
      child = Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
        decoration: BoxDecoration(
          color: colors.surface,
          border: Border.all(color: colors.line),
          borderRadius: BorderRadius.circular(999),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            SitePulseDot(color: colors.semantic('ok')),
            const SizedBox(width: 8),
            Text(n.text.trim()),
          ],
        ),
      );
    } else if (n.classes.contains('tile')) {
      child = Container(
        width: n.classes.contains('sm') ? 32 : 40,
        height: n.classes.contains('sm') ? 32 : 40,
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: colors.soft(kind(n)),
          borderRadius: BorderRadius.circular(12),
        ),
        child: n.querySelector('svg') != null
            ? icon(n.querySelector('svg')!)
            : Text(n.text.trim()),
      );
    } else if (n.classes.contains('chart')) {
      child = chart(n);
    } else if (n.classes.contains('chart-wrap') &&
        n.querySelector('svg') != null) {
      child = svgChart(n);
    } else if (n.classes.contains('badge') || n.classes.contains('dlt')) {
      child = badge(n);
    } else if (n.classes.contains('avatar')) {
      child = CircleAvatar(
        radius: n.classes.contains('sm') ? 16 : 20,
        backgroundColor: colors.accentSoft,
        child: Text(
          n.text.trim(),
          style: TextStyle(
            color: colors.accent,
            fontSize: 13,
            fontWeight: FontWeight.w700,
          ),
        ),
      );
    } else if (n.localName == 'a' && n.classes.contains('todo-item')) {
      child = SitePress(
        onTap: () => link(n.attributes['href'] ?? ''),
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          decoration: BoxDecoration(
            color: colors.soft(kind(n)),
            borderRadius: BorderRadius.circular(16),
            border: Border.all(
              color: colors.semantic(kind(n)).withValues(alpha: .25),
            ),
          ),
          child: Row(
            children: [
              for (final part in n.nodes)
                if (part is dom.Element && part.localName == 'svg')
                  Padding(
                    padding: const EdgeInsets.only(right: 12),
                    child: icon(part),
                  )
                else if (part is dom.Element && part.localName == 'span')
                  Expanded(
                    child: Text(
                      text(part.text).trim(),
                      style: TextStyle(
                        color: colors.semantic(kind(n)),
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  )
                else if (text(part.text).trim().isNotEmpty)
                  Text(
                    text(part.text).trim(),
                    style: TextStyle(
                      color: colors.semantic(kind(n)),
                      fontWeight: FontWeight.w700,
                    ),
                  ),
            ],
          ),
        ),
      );
    } else if (n.localName == 'a') {
      if (n.classes.contains('person') || n.classes.contains('who')) {
        child = InkWell(
          onTap: () => link(n.attributes['href'] ?? ''),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Row(
              children: [
                for (final item in n.nodes)
                  if (item is dom.Text && item.text.trim().isNotEmpty)
                    Expanded(
                      child: Text(
                        item.text.trim(),
                        overflow: TextOverflow.ellipsis,
                      ),
                    )
                  else if (item is dom.Element && item.classes.contains('grow'))
                    Expanded(child: node(item))
                  else
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: node(item),
                    ),
              ],
            ),
          ),
        );
      } else {
        child = n.classes.contains('primary')
            ? FilledButton(
                onPressed: () => link(n.attributes['href'] ?? ''),
                child: Text(n.text.trim()),
              )
            : n.classes.contains('btn')
            ? OutlinedButton(
                onPressed: () => link(n.attributes['href'] ?? ''),
                child: Text(n.text.trim()),
              )
            : TextButton(
                onPressed: () => link(n.attributes['href'] ?? ''),
                style: TextButton.styleFrom(
                  alignment: Alignment.centerLeft,
                  padding: const EdgeInsets.symmetric(horizontal: 4),
                ),
                child: Text(n.text.trim()),
              );
      }
    } else if (n.localName == 'button') {
      final copy = n.attributes['data-copy'];
      child = copy != null
          ? TextButton(
              onPressed: () => copyValue(context, copy),
              child: Text(n.text.trim()),
            )
          : const SizedBox.shrink();
    } else if (n.classes.contains('periods')) {
      child = Wrap(
        spacing: 6,
        runSpacing: 6,
        children: [
          for (final a in n.querySelectorAll('a'))
            ChoiceChip(
              label: Text(a.text.trim()),
              selected: a.classes.contains('on'),
              onSelected: (_) => link(a.attributes['href']!),
            ),
        ],
      );
    } else if (['h1', 'h2', 'h3', 'h4'].contains(n.localName)) {
      child = heading(n);
    } else if (n.localName == 'img') {
      child = ProductImage(n.attributes['src'], size: 80);
    } else if (n.localName == 'br') {
      child = const SizedBox(height: 6);
    } else if (n.localName == 'hr') {
      child = Divider(color: colors.line);
    } else if (n.localName == 'pre') {
      child = Container(
        width: double.infinity,
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: const Color(0xff16181d),
          borderRadius: BorderRadius.circular(12),
        ),
        child: SelectableText(
          n.text,
          style: const TextStyle(
            fontSize: 12.9,
            height: 1.6,
            color: Color(0xffe4e6eb),
          ),
        ),
      );
    } else if (n.localName == 'details') {
      child = ExpansionTile(
        title: Text(n.querySelector('summary')?.text ?? 'Подробнее'),
        initiallyExpanded: n.attributes.containsKey('open'),
        children: [
          for (final c in n.children.where((c) => c.localName != 'summary'))
            node(c),
        ],
      );
    } else if (n.classes.contains('summary')) {
      child = SiteCard(
        padding: EdgeInsets.zero,
        child: Column(
          children: [
            for (final row in n.children)
              Container(
                padding: const EdgeInsets.symmetric(
                  vertical: 10,
                  horizontal: 12,
                ),
                decoration: BoxDecoration(
                  color: row.classes.contains('total') ? colors.surface2 : null,
                  border: Border(bottom: BorderSide(color: colors.line)),
                ),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    for (final cell in row.children)
                      Expanded(child: node(cell)),
                  ],
                ),
              ),
          ],
        ),
      );
    } else if (n.classes.contains('heat')) {
      child = heat(n);
    } else if (n.classes.contains('tl-row')) {
      child = topRow(n);
    } else if (n.classes.contains('dash-two')) {
      child = LayoutBuilder(
        builder: (c, bounds) {
          final children = n.children.map(node).toList();
          if (bounds.maxWidth <= 900 || children.length != 2) {
            return vertical(children, gap: 14);
          }
          return Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(flex: 8, child: children[0]),
              const SizedBox(width: 14),
              Expanded(flex: 5, child: children[1]),
            ],
          );
        },
      );
    } else if (n.classes.contains('grid') ||
        n.classes.contains('tr-two') ||
        n.classes.contains('fin-flow')) {
      final children = n.children.map(node).toList();
      child = n.classes.contains('scards')
          ? LayoutBuilder(
              builder: (c, b) => SiteGrid(
                columns: b.maxWidth <= 560 ? 2 : null,
                breakpoint: 0,
                minimum: 190,
                gap: 10,
                children: children,
              ),
            )
          : n.classes.contains('fin-flow')
          ? financialFlow(children)
          : SiteGrid(
              minimum: n.classes.contains('three')
                  ? 250
                  : n.classes.contains('two')
                  ? 320
                  : 240,
              children: children,
            );
    } else if (n.classes.contains('flash') || n.classes.contains('todo-item')) {
      child = Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(
          color: colors.soft(kind(n)),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Text(
          n.text.trim(),
          style: TextStyle(color: colors.semantic(kind(n)), height: 1.55),
        ),
      );
    } else if (n.classes.contains('card')) {
      child = SiteCard(child: vertical(n.nodes.map(node), gap: 8));
    } else if (n.classes.contains('card-head') ||
        n.classes.contains('section-title') ||
        n.classes.contains('bot-head')) {
      child = Wrap(
        alignment: WrapAlignment.spaceBetween,
        crossAxisAlignment: WrapCrossAlignment.center,
        spacing: 12,
        runSpacing: 8,
        children: n.children.map(node).toList(),
      );
    } else if (n.classes.contains('legend') ||
        n.classes.contains('bot-actions') ||
        n.classes.contains('actions')) {
      child = Wrap(
        spacing: 10,
        runSpacing: 8,
        children: n.nodes.map(node).toList(),
      );
    } else if ([
          'p',
          'span',
          'b',
          'strong',
          'small',
          'code',
          'li',
          'label',
        ].contains(n.localName) &&
        n.querySelector('form,input,a,svg,button') == null) {
      child = SelectableText(n.text.trim(), style: style(n));
    } else {
      child = vertical(n.nodes.map(node), gap: 6);
    }
    final tip = n.attributes['data-tip'] ?? n.attributes['title'];
    if (tip != null && tip.isNotEmpty) {
      child = Tooltip(message: tip.replaceAll('|', '\n'), child: child);
    }
    if (n.id.isNotEmpty) {
      child = KeyedSubtree(
        key: anchors.putIfAbsent(n.id, GlobalKey.new),
        child: child,
      );
    }
    return child;
  }

  double percent(dom.Element? e, String variable) =>
      (double.tryParse(
                RegExp(
                      '${RegExp.escape(variable)}\\s*:\\s*([\\d.]+)%',
                    ).firstMatch(e?.attributes['style'] ?? '')?.group(1) ??
                    '',
              ) ??
              0)
          .clamp(0, 100)
          .toDouble();

  Widget trafficBars(dom.Element e) {
    final daily = e.classes.contains('vbars');
    final height = daily ? 200.0 : 130.0;
    return Semantics(
      label: daily ? 'Посетители по дням' : 'По часам',
      child: SizedBox(
        height: height,
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            for (final bar in e.children)
              Expanded(
                child: Tooltip(
                  message: (bar.attributes['data-tip'] ?? '').replaceAll(
                    '|',
                    '\n',
                  ),
                  child: TweenAnimationBuilder<double>(
                    tween: Tween(
                      begin: 0,
                      end: percent(
                        bar.querySelector(daily ? '.vb-bar' : 'span'),
                        '--h',
                      ),
                    ),
                    duration: MediaQuery.disableAnimationsOf(context)
                        ? Duration.zero
                        : const Duration(milliseconds: 700),
                    curve: siteEase,
                    builder: (c, value, _) => Container(
                      height: math.max(2, height * value / 100),
                      margin: const EdgeInsets.symmetric(horizontal: 1),
                      decoration: BoxDecoration(
                        color: e.classes.contains('buys')
                            ? colors.semantic('c-done')
                            : colors.accent.withValues(alpha: .85),
                        borderRadius: const BorderRadius.vertical(
                          top: Radius.circular(4),
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

  Widget funnelRow(dom.Element e) => Padding(
    padding: const EdgeInsets.only(bottom: 10),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                e.querySelector('.fn-t')?.text ?? '',
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
            Text(
              e.querySelector('.fn-n')?.text ?? '',
              style: const TextStyle(fontWeight: FontWeight.w700),
            ),
            const SizedBox(width: 10),
            Text(
              e.querySelector('.fn-c')?.text ?? '',
              style: TextStyle(fontSize: 13, color: colors.muted),
            ),
          ],
        ),
        const SizedBox(height: 6),
        TweenAnimationBuilder<double>(
          tween: Tween(
            begin: 0,
            end: percent(e.querySelector('.fn-bar'), '--w') / 100,
          ),
          duration: MediaQuery.disableAnimationsOf(context)
              ? Duration.zero
              : const Duration(milliseconds: 500),
          builder: (c, value, _) => ClipRRect(
            borderRadius: BorderRadius.circular(6),
            child: LinearProgressIndicator(
              value: value,
              minHeight: 12,
              color: colors.accent,
              backgroundColor: colors.surface2,
            ),
          ),
        ),
      ],
    ),
  );

  Widget financialBar(dom.Element e) => ClipRRect(
    borderRadius: BorderRadius.circular(6),
    child: SizedBox(
      height: 12,
      child: LayoutBuilder(
        builder: (c, b) => Row(
          children: [
            for (final part in e.children)
              SizedBox(
                width: b.maxWidth * percent(part, 'width') / 100,
                child: ColoredBox(
                  color: part.classes.contains('gain')
                      ? colors.semantic('ok')
                      : colors.accentLine,
                ),
              ),
          ],
        ),
      ),
    ),
  );

  Widget financialFlow(List<Widget> children) => LayoutBuilder(
    builder: (c, b) => b.maxWidth <= 700
        ? vertical(children, gap: 10)
        : Row(
            crossAxisAlignment: CrossAxisAlignment.center,
            children: [
              for (var i = 0; i < children.length; i++)
                if (i.isOdd)
                  SizedBox(width: 40, child: Center(child: children[i]))
                else
                  Expanded(child: children[i]),
            ],
          ),
  );

  Widget heat(dom.Element e) => SingleChildScrollView(
    scrollDirection: Axis.horizontal,
    child: Column(
      children: [
        for (final row in e.querySelectorAll('.heat-row'))
          Row(
            children: [
              for (var i = 0; i < row.children.length; i++)
                SizedBox(
                  width: i == 0 ? 38 : 18,
                  height: 24,
                  child: row.children[i].classes.contains('hc')
                      ? Tooltip(
                          message:
                              (row.children[i].attributes['data-tip'] ?? '')
                                  .replaceAll('|', '\n'),
                          child: Container(
                            margin: const EdgeInsets.all(2),
                            decoration: BoxDecoration(
                              borderRadius: BorderRadius.circular(3),
                              color: colors.accent.withValues(
                                alpha:
                                    .08 +
                                    .17 *
                                        (int.tryParse(
                                              row.children[i].classes
                                                  .firstWhere(
                                                    (x) => RegExp(
                                                      r'^l\d$',
                                                    ).hasMatch(x),
                                                    orElse: () => 'l0',
                                                  )
                                                  .substring(1),
                                            ) ??
                                            0),
                              ),
                            ),
                          ),
                        )
                      : Text(
                          row.children[i].text,
                          style: TextStyle(fontSize: 10, color: colors.muted),
                        ),
                ),
            ],
          ),
      ],
    ),
  );
  Widget topRow(dom.Element e) {
    final percent =
        double.tryParse(
          RegExp(r'--w:\s*([\d.]+)')
                  .firstMatch(
                    e.querySelector('.tl-bar')?.attributes['style'] ?? '',
                  )
                  ?.group(1) ??
              '',
        ) ??
        0;
    return SizedBox(
      height: 38,
      child: Stack(
        children: [
          FractionallySizedBox(
            widthFactor: (percent / 100).clamp(0, 1),
            heightFactor: 1,
            child: ColoredBox(color: colors.accentSoft),
          ),
          Positioned.fill(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      e.querySelector('.tl-name')?.text.trim() ?? '',
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  Text(
                    e.querySelector('.tl-n')?.text ?? '',
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  List<Widget> dashboard(dom.Element main) => [
    if (main.querySelector('.page-hero') case final dom.Element hero)
      node(hero),
    if (main.querySelector('.todo') case final dom.Element todo) node(todo),
    if (main.querySelector('.kpis') case final dom.Element cards) node(cards),
    if (main.querySelector('.dash-two') case final dom.Element two) node(two),
    for (final e in main.children.where(
      (e) =>
          !e.classes.contains('page-hero') &&
          !e.classes.contains('todo') &&
          !e.classes.contains('kpis') &&
          !e.classes.contains('dash-two'),
    ))
      node(e),
  ];
  List<Widget> listing(dom.Element main) => [
    for (final e in main.children.where((e) => e.classes.contains('page-hero')))
      node(e),
    for (final e in main.children.where(
      (e) => !e.classes.contains('page-hero'),
    ))
      e.localName == 'table'
          ? SiteCard(padding: const EdgeInsets.all(8), child: table(e))
          : node(e),
  ];
  List<Widget> detail(dom.Element main) => [
    for (final e in main.children)
      e.classes.contains('grid') && e.classes.contains('two')
          ? SiteGrid(
              columns: 2,
              breakpoint: 900,
              children: e.children.map(node).toList(),
            )
          : node(e),
  ];
  List<Widget> analytics(dom.Element main) => [
    for (final e in main.children)
      e.classes.contains('scards')
          ? SiteGrid(
              columns: 2,
              breakpoint: 0,
              gap: 10,
              children: e.children.map(kpi).toList(),
            )
          : node(e),
  ];
  List<Widget> finance(dom.Element main) => [
    for (final e in main.children)
      e.classes.contains('kpis')
          ? SiteGrid(minimum: 210, children: e.children.map(kpi).toList())
          : node(e),
  ];
  List<Widget> settings(dom.Element main) => [
    for (final e in main.children)
      if (e.localName == 'form')
        NativeForm(
          key: ValueKey('$path:${e.outerHtml}'),
          api: widget.api,
          form: e,
          sourcePath: path,
          render: node,
          completed: load,
          layout: formLayout,
          bare: true,
        )
      else
        node(e),
  ];
  List<Widget> bots(dom.Element main) => [
    for (final e in main.children)
      e.classes.contains('list')
          ? vertical(e.children.map(node), gap: 14)
          : node(e),
  ];
  List<Widget> sync(dom.Element main) => [
    for (final e in main.children)
      e.id == 'job' && job != null ? jobView() : node(e),
  ];
  List<Widget> traffic(dom.Element main) => [
    for (final e in main.children)
      e.classes.contains('scards')
          ? LayoutBuilder(
              builder: (c, b) => SiteGrid(
                columns: b.maxWidth < 560 ? 2 : null,
                breakpoint: 0,
                minimum: 190,
                gap: 10,
                children: e.children.map(kpi).toList(),
              ),
            )
          : node(e),
  ];
  Widget navigation({bool drawer = false}) => ListView(
    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 12),
    children: [
      if (drawer)
        Padding(
          padding: const EdgeInsets.only(bottom: 18),
          child: Row(
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: Image.asset('assets/logo.png', width: 32, height: 32),
              ),
              const SizedBox(width: 10),
              const Expanded(
                child: Text(
                  'Donatix',
                  style: TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
                ),
              ),
              IconButton(
                onPressed: () => Navigator.pop(context),
                icon: const Icon(Icons.close),
                tooltip: 'Закрыть меню',
              ),
            ],
          ),
        ),
      for (final element
          in data?.querySelectorAll('aside nav > h4, aside nav > a[href]') ??
              <dom.Element>[])
        if (element.localName == 'h4')
          Padding(
            padding: const EdgeInsets.fromLTRB(10, 14, 10, 8),
            child: Text(
              element.text.trim().toUpperCase(),
              style: TextStyle(
                fontSize: 11.8,
                color: colors.muted,
                fontWeight: FontWeight.w600,
                letterSpacing: .6,
              ),
            ),
          )
        else
          Padding(
            padding: const EdgeInsets.only(bottom: 4),
            child: Material(
              color: element.classes.contains('on')
                  ? colors.accentSoft
                  : Colors.transparent,
              borderRadius: BorderRadius.circular(12),
              child: InkWell(
                borderRadius: BorderRadius.circular(12),
                onTap: () {
                  if (drawer) Navigator.pop(context);
                  link(element.attributes['href']!);
                },
                child: Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 10,
                  ),
                  child: Row(
                    children: [
                      if (element.querySelector('svg')
                          case final dom.Element svg)
                        icon(
                          svg,
                          size: 18,
                          color: element.classes.contains('on')
                              ? colors.accent
                              : colors.muted,
                        ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(
                          element.querySelector('span')?.text.trim() ??
                              element.text.trim(),
                          style: TextStyle(
                            fontSize: 15,
                            color: element.classes.contains('on')
                                ? colors.accent
                                : colors.ink,
                            fontWeight: element.classes.contains('on')
                                ? FontWeight.w600
                                : FontWeight.w500,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
    ],
  );
  @override
  Widget build(BuildContext context) {
    final main =
        data?.querySelector('main#main') ?? data?.querySelector('main');
    final title =
        main?.querySelector('h1')?.text.trim() ?? 'Администрация Donatix';
    final blocks = main == null
        ? <Widget>[]
        : switch (page) {
            AdminLayout.dashboard => dashboard(main),
            AdminLayout.listing => listing(main),
            AdminLayout.detail => detail(main),
            AdminLayout.analytics => analytics(main),
            AdminLayout.finance => finance(main),
            AdminLayout.settings => settings(main),
            AdminLayout.bots => bots(main),
            AdminLayout.sync => sync(main),
            AdminLayout.traffic => traffic(main),
          };
    final wide = MediaQuery.sizeOf(context).width > 860;
    final content = main == null
        ? StateView(error: error, retry: load)
        : RefreshIndicator(
            onRefresh: load,
            child: ListView(
              controller: scroll,
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.fromLTRB(16, 6, 16, 40),
              children: [
                if (loading) const LinearProgressIndicator(),
                if (error != null)
                  Padding(
                    padding: const EdgeInsets.all(12),
                    child: Text(
                      '$error',
                      style: TextStyle(color: colors.semantic('bad')),
                    ),
                  ),
                for (var i = 0; i < blocks.length; i++)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 14),
                    child: SiteReveal(index: i, child: blocks[i]),
                  ),
              ],
            ),
          );
    return Scaffold(
      appBar: AppBar(
        toolbarHeight: 58,
        title: Semantics(
          label: title,
          child: Row(
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: Image.asset('assets/logo.png', width: 30, height: 30),
              ),
              const SizedBox(width: 10),
              const Text(
                'Donatix',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
              ),
            ],
          ),
        ),
        actions: [
          IconButton(
            onPressed: load,
            tooltip: 'Обновить',
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      drawer: wide
          ? null
          : Drawer(child: SafeArea(child: navigation(drawer: true))),
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 1280),
          child: wide
              ? Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    SizedBox(width: 236, child: navigation()),
                    const SizedBox(width: 28),
                    Expanded(child: content),
                  ],
                )
              : content,
        ),
      ),
    );
  }
}

import 'dart:convert';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_svg/flutter_svg.dart';
import 'package:html/dom.dart' as dom;
import 'package:html/parser.dart' as html;
import '../core/api.dart';
import '../core/navigation.dart';
import '../widgets/ui.dart';
import 'document.dart';
import 'management.dart';

/// Native rendering for the site's current support and analytics pages.
/// Existing authenticated forms remain the source of actions and data.
class SiteSectionScreen extends StatelessWidget {
  final DonatixApi api;
  final String path, title;
  const SiteSectionScreen({
    super.key,
    required this.api,
    required this.path,
    required this.title,
  });

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: Text(title)),
    body: AsyncPage(
      load: () async => {'document': await api.existingSite.sectionPage(path)},
      builder: (_, data) => SiteSectionContent(
        api: api,
        document: data['document'],
        path: path,
        title: title,
      ),
    ),
  );
}

class SiteSectionContent extends StatefulWidget {
  final DonatixApi api;
  final dom.Document document;
  final String path, title;
  const SiteSectionContent({
    super.key,
    required this.api,
    required this.document,
    required this.path,
    required this.title,
  });
  @override
  State<SiteSectionContent> createState() => _SiteSectionContentState();
}

class _SiteSectionContentState extends State<SiteSectionContent> {
  late dom.Document document = widget.document;
  late String path = widget.path;
  String query = '';

  @override
  void didUpdateWidget(covariant SiteSectionContent oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(widget.document, oldWidget.document)) {
      document = widget.document;
      path = widget.path;
      query = '';
    }
  }

  Future<void> link(String href) async {
    final root = Uri.parse(DonatixApi.origin);
    final uri = root.resolve(path).resolve(href);
    final section = Uri.parse(widget.path).path.startsWith('/panel/support')
        ? '/panel/support'
        : '/panel/stats';
    if (uri.origin == root.origin &&
        uri.userInfo.isEmpty &&
        (uri.path == section || uri.path.startsWith('$section/'))) {
      await Navigator.push(
        context,
        MaterialPageRoute<void>(
          builder: (_) => SiteSectionScreen(
            api: widget.api,
            path: uri.toString(),
            title: widget.title,
          ),
        ),
      );
    } else {
      await openDonatixLink(context, widget.api, uri.toString());
    }
  }

  Future<void> submitted(Response<dynamic>? response, int owner) async {
    widget.api.requireAccount(owner);
    if (response == null) return;
    final next = response.requestOptions.uri.toString();
    late dom.Document updated;
    String target = next;
    if ([301, 302, 303, 307, 308].contains(response.statusCode)) {
      final location = response.headers.value('location');
      if (location == null) {
        throw const ApiFailure('Не удалось открыть заявку.');
      }
      target = Uri.parse(next).resolve(location).toString();
      // page validates the origin, redirects and the current account.
      updated = await widget.api.existingSite.sectionPage(target);
    } else {
      if ((response.statusCode ?? 500) >= 400) {
        throw ApiFailure('Не удалось отправить форму.', response.statusCode);
      }
      final body = response.data;
      updated = html.parse(
        body is List<int> ? await widget.api.decodeHtml(body) : '$body',
      );
    }
    widget.api.requireAccount(owner);
    final csrf = updated
        .querySelector('input[name="csrf"]')
        ?.attributes['value'];
    if (csrf != null && csrf.isNotEmpty) widget.api.csrf = csrf;
    if (mounted) {
      setState(() {
        document = updated;
        path = target;
        query = '';
      });
    }
  }

  Future<void> topics() async {
    final items = document.querySelectorAll('#topic-sheet .sp-topic[href]');
    final selected = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      builder: (context) => SafeArea(
        child: FractionallySizedBox(
          heightFactor: .8,
          child: ListView(
            padding: const EdgeInsets.all(18),
            children: [
              Heading(
                ExistingSiteApi.text(document.querySelector('#sheet-title')),
              ),
              for (final item in items)
                ListTile(
                  title: Text(ExistingSiteApi.text(item.querySelector('b'))),
                  subtitle: Text(
                    ExistingSiteApi.text(item.querySelector('small')),
                  ),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: () => Navigator.pop(context, item.attributes['href']),
                ),
            ],
          ),
        ),
      ),
    );
    if (selected != null && mounted) await link(selected);
  }

  List<Map> get answers {
    for (final script in document.querySelectorAll('script')) {
      final source = RegExp(
        r'(?:var|let|const)\s+KB\s*=\s*(\[.*?\])\s*(?:;|,\s*[A-Za-z_$][\w$]*\s*=)',
        dotAll: true,
      ).firstMatch(script.text)?.group(1);
      if (source == null) continue;
      try {
        final value = jsonDecode(source);
        if (value is List) return value.whereType<Map>().toList();
      } on FormatException {
        // Category links still work if the site changes its search script.
      }
    }
    return [];
  }

  Widget search() {
    final words = query.toLowerCase().trim().split(RegExp(r'\s+'));
    final hits = query.trim().isEmpty
        ? <Map>[]
        : answers
              .where((answer) {
                final source =
                    '${answer['q']} ${answer['a']} ${answer['title']}'
                        .toLowerCase();
                return words.every(source.contains);
              })
              .take(6)
              .toList();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        TextField(
          decoration: const InputDecoration(
            labelText: 'Поиск по базе знаний',
            prefixIcon: Icon(Icons.search),
          ),
          onChanged: (value) => setState(() => query = value),
        ),
        const SizedBox(height: 12),
        for (final answer in hits)
          Surface(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Heading(text(answer['q'])),
                SelectableText(text(answer['a'])),
                TextButton(
                  onPressed: () =>
                      link('/panel/support/kb/${answer['section']}'),
                  child: const Text('Подробнее'),
                ),
              ],
            ),
          ),
      ],
    );
  }

  Widget chart(dom.Element element) {
    final copy = element.clone(true);
    String hex(Color color) =>
        '#${color.toARGB32().toRadixString(16).substring(2)}';
    final colors = {
      '--c-created': hex(Theme.of(context).colorScheme.primary),
      '--c-done': '#16a34a',
      '--c-ref': '#ef4444',
    };
    for (final item in [copy, ...copy.querySelectorAll('*')]) {
      for (final key in item.attributes.keys.toList()) {
        for (final color in colors.entries) {
          item.attributes[key] = item.attributes[key]!.replaceAll(
            'var(${color.key})',
            color.value,
          );
        }
      }
      if (item.classes.contains('gridline')) {
        item.attributes['stroke'] = hex(Theme.of(context).dividerColor);
      }
      if (item.classes.contains('ln')) {
        item.attributes['fill'] = 'none';
        item.attributes['stroke-width'] = '2.5';
        item.attributes['stroke'] =
            colors[item.classes.contains('c-done')
                ? '--c-done'
                : item.classes.contains('c-ref')
                ? '--c-ref'
                : '--c-created']!;
      }
    }
    return SizedBox(
      height: 220,
      width: double.infinity,
      child: SvgPicture.string(
        copy.outerHtml.replaceAll('viewbox=', 'viewBox='),
      ),
    );
  }

  Widget node(dom.Node value) {
    if (value is dom.Text) {
      var content = value.text.trim();
      if (Uri.parse(path).path == '/panel/stats') {
        content = content.replaceAllMapped(
          RegExp(r'\$(-?\d+(?:[.,]\d+)?)'),
          (match) =>
              widget.api.displayPrice(match.group(1)!.replaceAll(',', '.')),
        );
      }
      return content.isEmpty
          ? const SizedBox.shrink()
          : SelectableText(content);
    }
    if (value is! dom.Element) return const SizedBox.shrink();
    final tag = value.localName;
    if (value.attributes.containsKey('hidden') ||
        ['script', 'style', 'head', 'meta', 'noscript'].contains(tag)) {
      return const SizedBox.shrink();
    }
    if (tag == 'svg') {
      return value.classes.contains('line-chart')
          ? chart(value)
          : const SizedBox.shrink();
    }
    if (value.id == 'kb-q') return search();
    if (value.classes.contains('sp-search')) return search();
    if (tag == 'form') {
      final owner = widget.api.userId;
      return NativeForm(
        key: ObjectKey(value),
        api: widget.api,
        form: value,
        sourcePath: path,
        render: node,
        completed: ([response]) => submitted(response, owner),
      );
    }
    if (tag == 'input') return const SizedBox.shrink();
    if (tag == 'button') {
      if (value.attributes.containsKey('data-sheet-open')) {
        return Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: BusyButton(ExistingSiteApi.text(value), onPressed: topics),
        );
      }
      final copy = value.attributes['data-copy'];
      return copy == null
          ? const SizedBox.shrink()
          : TextButton(
              onPressed: () => copyValue(context, copy),
              child: Text(ExistingSiteApi.text(value)),
            );
    }
    if (tag == 'a') {
      final href = value.attributes['href'];
      if (href == null) return SelectableText(ExistingSiteApi.text(value));
      if (value.classes.contains('sp-card') ||
          value.classes.contains('sp-topic')) {
        return Surface(
          child: ListTile(
            contentPadding: EdgeInsets.zero,
            title: Text(ExistingSiteApi.text(value.querySelector('b'))),
            subtitle: Text(ExistingSiteApi.text(value.querySelector('small'))),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => link(href),
          ),
        );
      }
      return TextButton(
        onPressed: () => link(href),
        child: Text(ExistingSiteApi.text(value)),
      );
    }
    if (['h1', 'h2', 'h3', 'h4'].contains(tag)) {
      return Heading(ExistingSiteApi.text(value));
    }
    if (tag == 'img') {
      final url = imageUrl(value.attributes['src']);
      return url.isEmpty
          ? const SizedBox.shrink()
          : Image.network(
              url,
              headers:
                  Uri.parse(url).origin ==
                          Uri.parse(DonatixApi.origin).origin &&
                      widget.api.session != null
                  ? {'Cookie': 'dx_session=${widget.api.session}'}
                  : null,
              fit: BoxFit.contain,
              errorBuilder: (_, __, ___) => Text(value.attributes['alt'] ?? ''),
            );
    }
    if (tag == 'details') {
      return ExpansionTile(
        title: Text(ExistingSiteApi.text(value.querySelector('summary'))),
        children: value.children
            .where((c) => c.localName != 'summary')
            .map(node)
            .toList(),
      );
    }
    if (tag == 'hr') return const Divider();
    if (tag == 'br') return const SizedBox(height: 8);
    if (['p', 'li', 'pre', 'code'].contains(tag) &&
        value.querySelector('a, form, button') == null) {
      return Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: SelectableText(value.text.trim()),
      );
    }
    final children = value.nodes.map(node).toList();
    final content = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: children,
    );
    if (value.classes.any(
      ['card', 'flash', 'scard', 'stat', 'kpi', 'pstat', 'sp-empty'].contains,
    )) {
      return Surface(child: content);
    }
    if (value.classes.contains('periods') ||
        value.classes.contains('period-chips')) {
      return Wrap(spacing: 8, children: children);
    }
    return content;
  }

  @override
  Widget build(BuildContext context) =>
      node(document.querySelector('main#main, main') ?? document.body!);
}

import 'dart:async';
import 'package:dio/dio.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:html/dom.dart' as dom;
import 'package:html/parser.dart' as html;
import '../core/api.dart';
import '../core/checkout.dart';
import '../core/navigation.dart';
import '../widgets/ui.dart';
import '../widgets/site_design.dart';
import 'management.dart';

/// Legal text and server-controlled administration are rendered as Flutter
/// widgets. Forms call the original authenticated routes; there is no WebView.
class DocumentScreen extends StatefulWidget {
  final DonatixApi api;
  final String path, title;
  const DocumentScreen({
    super.key,
    required this.api,
    required this.path,
    required this.title,
  });
  @override
  State<DocumentScreen> createState() => _DocumentScreenState();
}

class _DocumentScreenState extends State<DocumentScreen> {
  dom.Document? document;
  Object? error;
  late String path = widget.path;
  Uint8List? binary;
  String mime = '', filename = '';
  final anchors = <String, GlobalKey>{};
  Timer? jobTimer;
  Map<String, dynamic>? job;
  @override
  void initState() {
    super.initState();
    load();
  }

  @override
  void dispose() {
    jobTimer?.cancel();
    super.dispose();
  }

  Future<void> updateJob() async {
    jobTimer?.cancel();
    try {
      final d = await widget.api.get('/admin/catalog-sync/status');
      if (mounted) setState(() => job = d);
    } catch (e) {
      if (mounted) setState(() => error = e);
    }
    if (mounted && job?['running'] == true) {
      jobTimer = Timer(const Duration(seconds: 2), updateJob);
    }
  }

  bool sameOrigin(String value) {
    final u = Uri.parse(DonatixApi.origin).resolve(value),
        root = Uri.parse(DonatixApi.origin);
    return u.scheme == root.scheme &&
        u.host == root.host &&
        u.port == root.port &&
        u.userInfo.isEmpty;
  }

  Future<void> load([Response<dynamic>? initial]) async {
    jobTimer?.cancel();
    job = null;
    if (mounted) {
      setState(() {
        error = null;
        document = null;
        binary = null;
        anchors.clear();
      });
    }
    try {
      Response<dynamic>? r = initial;
      if (r != null) path = r.realUri.toString();
      for (var i = 0; i < 6; i++) {
        if (!sameOrigin(path)) {
          throw const ApiFailure('Адрес страницы не принадлежит Donatix.');
        }
        r ??= await widget.api.dio.get<dynamic>(
          Uri.parse(path).replace(fragment: '').toString(),
          options: Options(responseType: ResponseType.bytes),
        );
        if ([301, 302, 303, 307, 308].contains(r.statusCode)) {
          final target = r.headers.value('location');
          if (target == null || !sameOrigin(target)) {
            throw const ApiFailure('Недоступный переход.');
          }
          path = Uri.parse(path).resolve(target).toString();
          r = null;
          continue;
        }
        break;
      }
      if (r == null) throw const ApiFailure('Слишком много переходов.');
      mime = r.headers.value('content-type') ?? '';
      final bytes = r.data is List<int>
          ? Uint8List.fromList(r.data as List<int>)
          : Uint8List.fromList('${r.data}'.codeUnits);
      if (mime.contains('text/html')) {
        // Dio's byte response must be decoded as UTF-8, preserving all source text.
        final decoded = html.parse(await widget.api.decodeHtml(bytes));
        final csrf = decoded
            .querySelector('input[name="csrf"]')
            ?.attributes['value'];
        if (csrf != null) widget.api.csrf = csrf;
        if (decoded.querySelector('form[action="/login"]') != null &&
            widget.api.userId != 0) {
          await widget.api.clear();
          widget.api.onSessionExpired?.call();
          throw const ApiFailure('Войдите в аккаунт.', 401);
        }
        if (mounted) {
          setState(() => document = decoded);
        }
      } else {
        filename = Uri.parse(path).pathSegments.last;
        if (filename.isEmpty) filename = 'Donatix-export';
        if (mounted) setState(() => binary = bytes);
      }
      if (Uri.parse(path).path == '/admin/catalog-sync') updateJob();
      final fragment = Uri.parse(path).fragment;
      if (fragment.isNotEmpty) {
        WidgetsBinding.instance.addPostFrameCallback((_) => scrollTo(fragment));
      }
    } catch (e) {
      if (mounted) setState(() => error = e);
    }
  }

  void scrollTo(String id) {
    final c = anchors[id]?.currentContext;
    if (c != null) {
      Scrollable.ensureVisible(c, duration: const Duration(milliseconds: 250));
    }
  }

  Future<void> link(String href) async {
    if (href.startsWith('#')) {
      scrollTo(href.substring(1));
      return;
    }
    final u = Uri.parse(DonatixApi.origin).resolve(path).resolve(href);
    if (sameOrigin(u.toString()) &&
        u.path.startsWith('/admin') &&
        u.path != '/admin/pricelist') {
      path = u.toString();
      await load();
    } else if (mounted) {
      await openDonatixLink(context, widget.api, u.toString());
    }
  }

  Widget node(dom.Node n) {
    if (n is dom.Text) {
      final value = n.text.trim();
      return value.isEmpty
          ? const SizedBox.shrink()
          : Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: SelectableText(value),
            );
    }
    if (n is! dom.Element) return const SizedBox.shrink();
    final tag = n.localName;
    if ([
      'script',
      'style',
      'head',
      'noscript',
      'input',
      'meta',
    ].contains(tag)) {
      return const SizedBox.shrink();
    }
    final children = n.nodes
        .where((x) => x is! dom.Text || x.text.trim().isNotEmpty)
        .map(node)
        .toList();
    Widget result;
    if (n.id == 'job' && job != null) {
      return Surface(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Heading(job!['running'] == true ? 'Загрузка идёт' : 'Ход загрузки'),
            for (final k in [
              'stage',
              'category',
              'products',
              'images_done',
              'images_total',
              'images_failed',
              'seconds',
              'error',
            ])
              if (job![k] != null) InfoRow(k, text(job![k])),
            SelectableText((job!['log'] as List? ?? []).join('\n')),
          ],
        ),
      );
    }
    if (tag == 'form') {
      result = NativeForm(
        api: widget.api,
        form: n,
        sourcePath: path,
        render: node,
        completed: load,
      );
    } else if (tag == 'img') {
      final url = imageUrl(n.attributes['src']);
      result = url.isEmpty
          ? const SizedBox.shrink()
          : Image.network(
              url,
              fit: BoxFit.contain,
              height: 140,
              errorBuilder: (_, __, ___) => Text(n.attributes['alt'] ?? ''),
            );
    } else if (tag == 'a') {
      result = TextButton(
        onPressed: () => link(n.attributes['href'] ?? ''),
        child: Text(n.text.trim()),
      );
    } else if (tag == 'button') {
      final copy = n.attributes['data-copy'];
      result = copy == null
          ? const SizedBox.shrink()
          : TextButton(
              onPressed: () => copyValue(context, copy),
              child: Text(n.text.trim()),
            );
    } else if (['h1', 'h2', 'h3', 'h4'].contains(tag)) {
      result = Heading(n.text.trim());
    } else if (tag == 'table') {
      final rows = n.querySelectorAll('tr').toList();
      final columns = rows.fold<int>(
        0,
        (count, r) => count > r.children.length ? count : r.children.length,
      );
      result = SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Table(
          defaultColumnWidth: const FixedColumnWidth(230),
          border: TableBorder.all(color: Theme.of(context).dividerColor),
          children: [
            for (final row in rows)
              TableRow(
                children: [
                  for (final cell in [
                    ...row.children,
                    ...List.generate(
                      columns - row.children.length,
                      (_) => dom.Element.tag('td'),
                    ),
                  ])
                    Padding(
                      padding: const EdgeInsets.all(10),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: cell.nodes.map(node).toList(),
                      ),
                    ),
                ],
              ),
          ],
        ),
      );
    } else if (tag == 'details') {
      result = ExpansionTile(
        title: Text(n.querySelector('summary')?.text.trim() ?? 'Подробнее'),
        children: n.children
            .where((c) => c.localName != 'summary')
            .map(node)
            .toList(),
      );
    } else if (tag == 'pre' || tag == 'code') {
      result = Container(
        width: double.infinity,
        margin: const EdgeInsets.symmetric(vertical: 8),
        padding: const EdgeInsets.all(12),
        color: Theme.of(context).colorScheme.surfaceContainerHighest,
        child: SelectableText(
          n.text,
          style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
        ),
      );
    } else if (tag == 'br') {
      result = const SizedBox(height: 8);
    } else if (tag == 'hr') {
      result = const Divider();
    } else if (n.classes.contains('card') ||
        n.classes.contains('flash') ||
        n.classes.contains('kpi')) {
      result = Surface(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: children,
        ),
      );
    } else if (['p', 'li', 'label'].contains(tag) &&
        n.querySelector('a,form,input,button') == null) {
      result = Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: SelectableText('${tag == 'li' ? '• ' : ''}${n.text.trim()}'),
      );
    } else {
      result = Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: children,
      );
    }
    final id = n.id;
    if (id.isNotEmpty) {
      result = KeyedSubtree(
        key: anchors.putIfAbsent(id, GlobalKey.new),
        child: result,
      );
    }
    return result;
  }

  Future<void> share() async {
    try {
      await const MethodChannel('tj.donatix.app/native').invokeMethod<void>(
        'shareFile',
        {'bytes': binary, 'mime': mime.split(';').first, 'name': filename},
      );
    } catch (e) {
      if (mounted) message(context, 'Не удалось открыть файл: $e');
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: Text(widget.title),
      actions: [IconButton(onPressed: load, icon: const Icon(Icons.refresh))],
    ),
    body: document == null && binary == null
        ? StateView(error: error, retry: load)
        : binary != null
        ? ListView(
            padding: const EdgeInsets.all(18),
            children: [
              if (mime.startsWith('image/')) Image.memory(binary!),
              Text(filename),
              const SizedBox(height: 16),
              BusyButton('Открыть / сохранить файл', onPressed: share),
            ],
          )
        : RefreshIndicator(
            onRefresh: load,
            child: ListView(
              padding: const EdgeInsets.all(18),
              children: [
                if (path.contains('/admin'))
                  Wrap(
                    children: [
                      for (final a in document!.querySelectorAll(
                        'aside nav a[href]',
                      ))
                        TextButton(
                          onPressed: () => link(a.attributes['href']!),
                          child: Text(a.text.trim()),
                        ),
                    ],
                  ),
                node(
                  document!.querySelector('main#main') ??
                      document!.querySelector('main') ??
                      document!.body!,
                ),
              ],
            ),
          ),
  );
}

class NativeForm extends StatefulWidget {
  final DonatixApi api;
  final dom.Element form;
  final String sourcePath;
  final Widget Function(dom.Node) render;
  final Future<void> Function([Response<dynamic>?]) completed;
  final Widget Function(dom.Element, List<Widget>)? layout;
  final bool bare;
  const NativeForm({
    super.key,
    required this.api,
    required this.form,
    required this.sourcePath,
    required this.render,
    required this.completed,
    this.layout,
    this.bare = false,
  });
  @override
  State<NativeForm> createState() => _NativeFormState();
}

class _NativeFormState extends State<NativeForm> {
  var formKey = GlobalKey<FormState>();
  final controllers = <dom.Element, TextEditingController>{};
  final values = <dom.Element, String>{}, files = <dom.Element, String>{};
  bool busy = false;
  int formRevision = 0;
  @override
  void initState() {
    super.initState();
    readForm();
  }

  @override
  void didUpdateWidget(covariant NativeForm oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.form, widget.form)) {
      for (final c in controllers.values) {
        c.dispose();
      }
      controllers.clear();
      values.clear();
      files.clear();
      formKey = GlobalKey<FormState>();
      formRevision++;
      readForm();
    }
  }

  void readForm() {
    for (final e in widget.form.querySelectorAll('input,textarea,select')) {
      if (e.localName == 'select') {
        final option =
            e.querySelector('option[selected]') ?? e.querySelector('option');
        values[e] = option?.attributes['value'] ?? option?.text.trim() ?? '';
      } else if (['checkbox', 'radio'].contains(e.attributes['type'])) {
        if (e.attributes.containsKey('checked')) {
          values[e] = e.attributes['value'] ?? 'on';
        }
      } else {
        controllers[e] = TextEditingController(
          text: e.localName == 'textarea'
              ? e.text
              : e.attributes['value'] ?? '',
        );
      }
    }
  }

  @override
  void dispose() {
    for (final c in controllers.values) {
      c.dispose();
    }
    super.dispose();
  }

  String label(dom.Element e) {
    final id = e.id;
    final labels = widget.form.querySelectorAll('label');
    for (final l in labels) {
      if ((id.isNotEmpty && l.attributes['for'] == id) || l.nodes.contains(e)) {
        return l.text.trim();
      }
    }
    return e.attributes['aria-label'] ??
        e.attributes['placeholder'] ??
        e.attributes['name'] ??
        '';
  }

  Future<void> pick(dom.Element e) async {
    final selected = await FilePicker.platform.pickFiles();
    if (selected?.files.single.path != null && mounted) {
      setState(() => files[e] = selected!.files.single.path!);
    }
  }

  Future<void> submit(dom.Element button) async {
    if (busy || !(formKey.currentState?.validate() ?? false)) return;
    final revision = formRevision;
    final method = (widget.form.attributes['method'] ?? 'get').toUpperCase();
    setState(() => busy = true);
    try {
      if (method != 'GET' &&
          !await confirmAction(
            context,
            button.text.trim().isEmpty
                ? 'Сохранить изменения?'
                : button.text.trim(),
            'Действие будет выполнено на сервере Donatix.',
          )) {
        return;
      }
      if (!mounted) return;
      if (revision != formRevision) {
        throw const ApiFailure(
          'Форма обновилась. Проверьте значения и сохраните снова.',
        );
      }
      final entries = <MapEntry<String, String>>[];
      final fileEntries = <MapEntry<String, MultipartFile>>[];
      for (final e in widget.form.querySelectorAll('input,textarea,select')) {
        final name = e.attributes['name'];
        if (name == null || e.attributes.containsKey('disabled')) continue;
        if (e.attributes['type'] == 'file') {
          if (files[e] != null) {
            fileEntries.add(
              MapEntry(name, await MultipartFile.fromFile(files[e]!)),
            );
          }
          continue;
        }
        if (['checkbox', 'radio'].contains(e.attributes['type']) &&
            !values.containsKey(e)) {
          continue;
        }
        entries.add(
          MapEntry(name, name == 'csrf' ? widget.api.csrf : fieldValue(e)),
        );
      }
      if (button.attributes['name'] != null) {
        entries.add(
          MapEntry(
            button.attributes['name']!,
            button.attributes['value'] ?? '',
          ),
        );
      }
      final root = Uri.parse(DonatixApi.origin);
      var target = root
          .resolve(widget.sourcePath)
          .resolve(
            button.attributes['formaction'] ??
                widget.form.attributes['action'] ??
                widget.sourcePath,
          );
      if (target.scheme != root.scheme ||
          target.host != root.host ||
          target.port != root.port ||
          target.userInfo.isNotEmpty) {
        throw const ApiFailure('Форма отправляется только на сервер Donatix.');
      }
      Response<dynamic> response;
      if (method == 'GET') {
        target = target.replace(
          queryParameters: {for (final e in entries) e.key: e.value},
        );
        response = await widget.api.dio.get<dynamic>(
          target.toString(),
          options: Options(responseType: ResponseType.bytes),
        );
      } else {
        if (!entries.any((e) => e.key == 'csrf')) {
          entries.add(MapEntry('csrf', widget.api.csrf));
        }
        final data = FormData()
          ..fields.addAll(entries)
          ..files.addAll(fileEntries);
        response = await widget.api.dio.request<dynamic>(
          target.toString(),
          data: data,
          options: Options(method: method, responseType: ResponseType.bytes),
        );
      }
      if (mounted) await widget.completed(response);
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  bool isMarkup(dom.Element e) {
    final path = Uri.parse(widget.sourcePath).path;
    final name = e.attributes['name'] ?? '';
    return (path == '/admin/settings' && name.startsWith('markup_')) ||
        (path.startsWith('/admin/users/') && name == 'markup_override');
  }

  String fieldValue(dom.Element e) {
    final value = controllers[e]?.text ?? values[e] ?? '';
    if (!isMarkup(e)) return value;
    final normalized = value.trim().replaceAll(',', '.');
    return e.attributes['name'] == 'markup_override'
        ? normalized
        : normalized.replaceAll('%', '').trim();
  }

  String? validateField(dom.Element e, String? value) {
    if (e.attributes.containsKey('required') && (value ?? '').trim().isEmpty) {
      return 'Заполните поле';
    }
    if (!isMarkup(e) || (value ?? '').trim().isEmpty) return null;
    final raw = (value ?? '').trim().replaceAll(',', '.');
    final personal = e.attributes['name'] == 'markup_override';
    final number = double.tryParse(
      personal ? raw : raw.replaceAll('%', '').trim(),
    );
    if (number == null || !number.isFinite) return 'Введите число в процентах';
    if (number < (personal ? -50 : 0) || number > (personal ? 500 : 100)) {
      return personal ? 'Допустимо от −50 до 500 %' : 'Допустимо от 0 до 100 %';
    }
    return null;
  }

  Widget node(dom.Node n) {
    if (n is! dom.Element) return widget.render(n);
    if (n.attributes.containsKey('hidden') ||
        ['script', 'style'].contains(n.localName)) {
      return const SizedBox.shrink();
    }
    if (n.localName == 'svg' ||
        n.classes.contains('tile') ||
        n.localName == 'img') {
      return widget.render(n);
    }
    if (controllers.containsKey(n) ||
        values.containsKey(n) ||
        ['input', 'textarea', 'select'].contains(n.localName)) {
      final type = n.attributes['type'] ?? 'text';
      if (type == 'hidden') return const SizedBox.shrink();
      final disabled = busy || n.attributes.containsKey('disabled');
      if (type == 'file') {
        return OutlinedButton.icon(
          onPressed: disabled ? null : () => pick(n),
          icon: const Icon(Icons.attach_file),
          label: Text(files[n] == null ? label(n) : files[n]!.split('/').last),
        );
      }
      if (type == 'checkbox') {
        return CheckboxListTile(
          contentPadding: EdgeInsets.zero,
          title: Text(label(n)),
          value: values.containsKey(n),
          onChanged: disabled
              ? null
              : (v) => setState(() {
                  if (v == true) {
                    values[n] = n.attributes['value'] ?? 'on';
                  } else {
                    values.remove(n);
                  }
                }),
        );
      }
      if (type == 'radio') {
        return RadioGroup<String>(
          groupValue: values.entries
              .where((e) => e.key.attributes['name'] == n.attributes['name'])
              .map((e) => e.value)
              .firstOrNull,
          onChanged: (v) {
            if (disabled) return;
            setState(() {
              values.removeWhere(
                (e, _) => e.attributes['name'] == n.attributes['name'],
              );
              if (v != null) values[n] = v;
            });
          },
          child: RadioListTile<String>(
            title: Text(label(n)),
            value: n.attributes['value'] ?? '',
            enabled: !disabled,
          ),
        );
      }
      if (n.localName == 'select') {
        return Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: DropdownButtonFormField<String>(
            initialValue: values[n],
            isExpanded: true,
            decoration: InputDecoration(labelText: label(n)),
            items: n
                .querySelectorAll('option')
                .map(
                  (o) => DropdownMenuItem(
                    value: o.attributes['value'] ?? o.text,
                    child: Text(o.text.trim(), overflow: TextOverflow.ellipsis),
                  ),
                )
                .toList(),
            onChanged: disabled
                ? null
                : (v) => setState(() => values[n] = v ?? ''),
            validator: (v) =>
                n.attributes.containsKey('required') && (v ?? '').isEmpty
                ? 'Выберите значение'
                : null,
          ),
        );
      }
      return Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: TextFormField(
          key: ValueKey(n.attributes['name'] ?? n.id),
          controller: controllers[n],
          enabled: !disabled,
          obscureText: type == 'password',
          readOnly: n.attributes.containsKey('readonly'),
          minLines: type == 'password'
              ? 1
              : n.localName == 'textarea'
              ? 3
              : 1,
          maxLines: type == 'password'
              ? 1
              : n.localName == 'textarea'
              ? 8
              : 1,
          keyboardType:
              isMarkup(n) ||
                  ['number', 'range'].contains(type) ||
                  ['numeric', 'decimal'].contains(n.attributes['inputmode'])
              ? const TextInputType.numberWithOptions(
                  decimal: true,
                  signed: true,
                )
              : type == 'email'
              ? TextInputType.emailAddress
              : type == 'url'
              ? TextInputType.url
              : TextInputType.text,
          decoration: InputDecoration(
            labelText: label(n),
            hintText: n.attributes['placeholder'],
            suffixText: isMarkup(n) ? '%' : null,
            helperText: isMarkup(n) && !n.attributes.containsKey('required')
                ? 'Пусто — использовать наценку уровня'
                : null,
            helperMaxLines: 2,
          ),
          maxLength: int.tryParse(n.attributes['maxlength'] ?? ''),
          validator: (v) => validateField(n, v),
        ),
      );
    }
    if (n.localName == 'button') {
      if ((n.attributes['type'] ?? 'submit') != 'submit') {
        final copy = n.attributes['data-copy'];
        return copy == null
            ? const SizedBox.shrink()
            : TextButton(
                onPressed: () => copyValue(context, copy),
                child: Text(n.text.trim()),
              );
      }
      final action = n.attributes.containsKey('disabled') || busy
          ? null
          : () => submit(n);
      final label = busy
          ? const SizedBox(
              width: 18,
              height: 18,
              child: CircularProgressIndicator(strokeWidth: 2),
            )
          : Text(n.text.trim().isEmpty ? 'Сохранить' : n.text.trim());
      if (n.classes.contains('sync-btn')) {
        final p = SiteColors(context);
        return Opacity(
          opacity: action == null ? .5 : 1,
          child: Material(
            color: n.classes.contains('main') ? p.accentSoft : p.surface,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(14),
              side: BorderSide(
                color: n.classes.contains('main') ? p.accent : p.line,
              ),
            ),
            child: InkWell(
              borderRadius: BorderRadius.circular(14),
              onTap: action,
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Row(
                  children: [
                    if (n.querySelector('.tile') case final dom.Element tile)
                      widget.render(tile),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            n.querySelector('b')?.text.trim() ?? n.text.trim(),
                            style: const TextStyle(fontWeight: FontWeight.w700),
                          ),
                          if (n.querySelector('.sub')
                              case final dom.Element sub)
                            Text(
                              sub.text.trim(),
                              style: TextStyle(fontSize: 13.6, color: p.muted),
                            ),
                        ],
                      ),
                    ),
                    if (busy)
                      const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      ),
                  ],
                ),
              ),
            ),
          ),
        );
      }
      final button =
          n.classes.contains('primary') || n.classes.contains('danger')
          ? FilledButton(
              onPressed: action,
              style: n.classes.contains('danger')
                  ? FilledButton.styleFrom(
                      backgroundColor: Theme.of(context).colorScheme.error,
                      foregroundColor: Theme.of(context).colorScheme.onError,
                    )
                  : null,
              child: label,
            )
          : OutlinedButton(onPressed: action, child: label);
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: n.classes.contains('block')
            ? SizedBox(width: double.infinity, child: button)
            : button,
      );
    }
    if (n.localName == 'label' && n.querySelector('input,select') == null) {
      return const SizedBox.shrink();
    }
    if (n.localName == 'label' &&
        n.querySelector('input[type="checkbox"],input[type="radio"]') != null) {
      return node(n.querySelector('input')!);
    }
    if (n.children.isEmpty ||
        ['a', 'h1', 'h2', 'h3', 'p', 'pre'].contains(n.localName)) {
      return widget.render(n);
    }
    final children = n.nodes
        .where((x) => x is! dom.Text || x.text.trim().isNotEmpty)
        .map(node)
        .toList();
    return widget.layout?.call(n, children) ??
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: children,
        );
  }

  @override
  Widget build(BuildContext context) =>
      widget.bare ? content() : Surface(child: content());
  Widget content() => Form(
    key: formKey,
    child:
        widget.layout?.call(
          widget.form,
          widget.form.nodes
              .where((x) => x is! dom.Text || x.text.trim().isNotEmpty)
              .map(node)
              .toList(),
        ) ??
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: widget.form.nodes
              .where((x) => x is! dom.Text || x.text.trim().isNotEmpty)
              .map(node)
              .toList(),
        ),
  );
}

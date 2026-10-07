import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_svg/flutter_svg.dart';
import 'package:dio/dio.dart';
import '../core/api.dart';
import '../widgets/ui.dart';
import '../widgets/site_design.dart';
import 'document.dart';
import 'catalog.dart';
import 'management.dart';
import '../core/checkout.dart';
import '../core/apple_login.dart';

class AuthScreen extends StatefulWidget {
  final DonatixApi api;
  final VoidCallback onDone;
  const AuthScreen({super.key, required this.api, required this.onDone});
  @override
  State<AuthScreen> createState() => _AuthScreenState();
}

class _AuthScreenState extends State<AuthScreen> with WidgetsBindingObserver {
  final form = GlobalKey<FormState>();
  final email = TextEditingController(),
      password = TextEditingController(),
      login = TextEditingController(),
      password2 = TextEditingController(),
      project = TextEditingController(),
      referral = TextEditingController(),
      code = TextEditingController();
  Map<String, dynamic>? config, oauth;
  Timer? oauthTimer;
  bool claiming = false;
  bool connecting = false;
  Object? connectionError;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    initialize();
  }

  Future<void> initialize() async {
    if (connecting) return;
    setState(() {
      connecting = true;
      connectionError = null;
    });
    try {
      final d = await widget.api.get('/api/v1/mobile/config');
      final saved = await widget.api.storage.read(key: 'oauth_pending');
      Map<String, dynamic>? restored;
      if (saved != null) {
        try {
          restored = Map<String, dynamic>.from(jsonDecode(saved) as Map);
          if (restored['ticket'] is! String ||
              restored['verifier'] is! String ||
              restored['url'] is! String) {
            restored = null;
          }
        } catch (_) {
          restored = null;
        }
        if (restored == null) {
          await widget.api.storage.delete(key: 'oauth_pending');
        }
      }
      if (mounted) {
        setState(() {
          config = d;
          oauth = restored;
        });
      }
      if (oauth != null) claim();
    } catch (e) {
      if (mounted) setState(() => connectionError = e);
    } finally {
      if (mounted) setState(() => connecting = false);
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    oauthTimer?.cancel();
    if (state == AppLifecycleState.resumed) {
      if (oauth != null) {
        claim();
      } else if (config == null || connectionError != null) {
        initialize();
      }
    }
  }

  Future<void> google() async {
    if (busy || oauth != null || connecting) return;
    if (register && !accepted) {
      message(context, 'Примите условия и политику конфиденциальности.');
      return;
    }
    setState(() => busy = true);
    try {
      final verifier = operationId() + operationId();
      final d = await widget.api.post('/api/v1/mobile/oauth/prepare', {
        'challenge': sha256.convert(utf8.encode(verifier)).toString(),
      });
      oauth = {'ticket': d['ticket'], 'verifier': verifier, 'url': d['url']};
      await widget.api.storage.write(
        key: 'oauth_pending',
        value: jsonEncode(oauth),
      );
      if (mounted) await externalLink(context, text(d['url']));
      claim();
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> claim() async {
    if (claiming || oauth == null) return;
    claiming = true;
    final attempt = oauth!;
    oauthTimer?.cancel();
    try {
      final d = await widget.api.post('/api/v1/mobile/oauth/claim', {
        'ticket': attempt['ticket'],
        'verifier': attempt['verifier'],
      });
      if (!mounted || !identical(oauth, attempt)) return;
      if (d['pending'] == false) {
        await widget.api.storage.delete(key: 'oauth_pending');
        oauth = null;
        await widget.api.bootstrap();
        if (mounted) widget.onDone();
      }
    } catch (e) {
      if (mounted &&
          identical(oauth, attempt) &&
          e is ApiFailure &&
          e.status != null &&
          e.status! >= 400 &&
          e.status! < 500) {
        await widget.api.storage.delete(key: 'oauth_pending');
        oauth = null;
        if (mounted) {
          setState(() {});
          message(context, e);
        }
      }
    } finally {
      claiming = false;
      if (mounted &&
          oauth != null &&
          WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed) {
        oauthTimer = Timer(const Duration(seconds: 3), claim);
      }
    }
  }

  Future<void> apple() async {
    if (busy) return;
    if (register && !accepted) {
      message(context, 'Примите условия и политику конфиденциальности.');
      return;
    }
    setState(() => busy = true);
    try {
      final done = await AppleSignIn.authenticate(widget.api);
      if (!mounted) return;
      if (done) {
        widget.onDone();
      } else {
        setState(() => verification = true);
      }
    } on PlatformException catch (e) {
      if (mounted && e.code != 'apple_canceled') {
        message(context, e.message ?? 'Не удалось войти через Apple.');
      }
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  bool register = false,
      busy = false,
      verification = false,
      hidden = true,
      accepted = false;
  @override
  void dispose() {
    email.dispose();
    password.dispose();
    login.dispose();
    password2.dispose();
    project.dispose();
    referral.dispose();
    oauthTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    code.dispose();
    super.dispose();
  }

  Future<void> submit() async {
    if (busy || oauth != null || connecting || connectionError != null) return;
    if (register && config?['registration_open'] == false) return;
    if (!(form.currentState?.validate() ?? false)) return;
    if (register && !accepted) {
      message(context, 'Примите условия и политику конфиденциальности.');
      return;
    }
    setState(() => busy = true);
    try {
      if (verification) {
        await widget.api.verifyCode(code.text.trim());
        widget.onDone();
      } else {
        final done = await widget.api.signIn({
          'email': email.text.trim(),
          'password': password.text,
          if (register) 'login': login.text.trim(),
          if (register) 'password2': password2.text,
          if (register) 'project': project.text.trim(),
          if (register) 'ref': referral.text.trim(),
        }, register: register);
        if (done) {
          password.clear();
          widget.onDone();
        } else if (mounted) {
          setState(() => verification = true);
        }
      }
    } catch (e) {
      if (mounted) {
        message(
          context,
          e is DioException ? 'Нет связи с сервером. Проверьте интернет.' : e,
        );
      }
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      toolbarHeight: 58,
      title: Row(
        children: [
          ClipRRect(
            borderRadius: BorderRadius.circular(8),
            child: Image.asset('assets/logo.png', width: 28, height: 28),
          ),
          const SizedBox(width: 9),
          const Text(
            'Donatix',
            style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
          ),
        ],
      ),
    ),
    body: SafeArea(
      child: Align(
        alignment: Alignment.topCenter,
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 440),
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 28),
            child: Form(
              key: form,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  SiteReveal(
                    child: Surface(
                      padding: const EdgeInsets.all(24),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Row(
                            children: [
                              Container(
                                width: 44,
                                height: 44,
                                alignment: Alignment.center,
                                decoration: BoxDecoration(
                                  color: SiteColors(context).accentSoft,
                                  borderRadius: BorderRadius.circular(12),
                                ),
                                child: Icon(
                                  verification
                                      ? Icons.verified_user_outlined
                                      : register
                                      ? Icons.handshake_outlined
                                      : Icons.grid_view,
                                  color: SiteColors(context).accent,
                                  size: 24,
                                ),
                              ),
                              const SizedBox(width: 14),
                              Expanded(
                                child: Text(
                                  verification
                                      ? 'Подтвердите вход'
                                      : register
                                      ? 'Стать партнёром'
                                      : 'Вход в панель',
                                  style: const TextStyle(
                                    fontSize: 20.8,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                              ),
                            ],
                          ),
                          const Padding(
                            padding: EdgeInsets.symmetric(vertical: 16),
                            child: Divider(height: 1),
                          ),
                          if (verification)
                            const Padding(
                              padding: EdgeInsets.only(bottom: 16),
                              child: Text(
                                'Введите код из Telegram-бота администратора.',
                              ),
                            ),
                          if (!verification &&
                              Platform.isIOS &&
                              config?['apple_enabled'] == true) ...[
                            FilledButton.icon(
                              onPressed: busy ? null : apple,
                              style: FilledButton.styleFrom(
                                backgroundColor: Colors.black,
                                foregroundColor: Colors.white,
                              ),
                              icon: const Icon(Icons.apple),
                              label: const Text('Войти с Apple'),
                            ),
                            const SizedBox(height: 12),
                          ],
                          if (!verification &&
                              config?['google_enabled'] == true) ...[
                            OutlinedButton.icon(
                              onPressed: busy || oauth != null ? null : google,
                              icon: SvgPicture.asset(
                                'assets/google.svg',
                                width: 20,
                                height: 20,
                              ),
                              label: Text(
                                register
                                    ? 'Регистрация через Google'
                                    : 'Войти через Google',
                              ),
                            ),
                            const Padding(
                              padding: EdgeInsets.symmetric(vertical: 16),
                              child: Row(
                                children: [
                                  Expanded(child: Divider()),
                                  Padding(
                                    padding: EdgeInsets.symmetric(
                                      horizontal: 12,
                                    ),
                                    child: Text('или'),
                                  ),
                                  Expanded(child: Divider()),
                                ],
                              ),
                            ),
                          ],
                          if (!verification &&
                              (connecting || connectionError != null))
                            Padding(
                              padding: const EdgeInsets.only(bottom: 16),
                              child: Column(
                                children: [
                                  if (connecting)
                                    const LinearProgressIndicator()
                                  else ...[
                                    Text('$connectionError'),
                                    TextButton.icon(
                                      onPressed: initialize,
                                      icon: const Icon(Icons.refresh),
                                      label: const Text(
                                        'Повторить подключение',
                                      ),
                                    ),
                                  ],
                                ],
                              ),
                            ),
                          if (oauth != null) ...[
                            const Text(
                              'Подтвердите вход в браузере и вернитесь в приложение.',
                            ),
                            TextButton(
                              onPressed: claim,
                              child: const Text('Проверить подтверждение'),
                            ),
                            TextButton(
                              onPressed: () =>
                                  externalLink(context, text(oauth!['url'])),
                              child: const Text('Открыть браузер ещё раз'),
                            ),
                            TextButton(
                              onPressed: claiming
                                  ? null
                                  : () async {
                                      oauthTimer?.cancel();
                                      await widget.api.storage.delete(
                                        key: 'oauth_pending',
                                      );
                                      if (mounted) setState(() => oauth = null);
                                    },
                              child: const Text('Отменить вход через Google'),
                            ),
                          ],
                          if (verification)
                            TextFormField(
                              controller: code,
                              decoration: const InputDecoration(
                                labelText: 'Код',
                              ),
                              keyboardType: TextInputType.number,
                              validator: (v) =>
                                  RegExp(r'^\d{6}$').hasMatch(v ?? '')
                                  ? null
                                  : 'Введите 6 цифр',
                            )
                          else ...[
                            TextFormField(
                              controller: email,
                              decoration: InputDecoration(
                                labelText: register
                                    ? 'Email'
                                    : 'Email или имя пользователя',
                              ),
                              keyboardType: register
                                  ? TextInputType.emailAddress
                                  : TextInputType.text,
                              autofillHints: [
                                register
                                    ? AutofillHints.email
                                    : AutofillHints.username,
                              ],
                              validator: (v) => (v ?? '').trim().isEmpty
                                  ? 'Введите email или имя пользователя'
                                  : register && !(v ?? '').contains('@')
                                  ? 'Введите email'
                                  : null,
                            ),
                            const SizedBox(height: 14),
                            if (register)
                              TextFormField(
                                controller: login,
                                decoration: const InputDecoration(
                                  labelText: 'Имя пользователя',
                                ),
                                validator: (v) =>
                                    RegExp(
                                      r'^[A-Za-z0-9_.\-]{3,32}$',
                                    ).hasMatch((v ?? '').trim())
                                    ? null
                                    : 'От 3 до 32 символов: латиница, цифры, _ . -',
                              ),
                            if (register) const SizedBox(height: 14),
                            TextFormField(
                              controller: password,
                              obscureText: hidden,
                              autofillHints: [
                                register
                                    ? AutofillHints.newPassword
                                    : AutofillHints.password,
                              ],
                              decoration: InputDecoration(
                                labelText: 'Пароль',
                                suffixIcon: IconButton(
                                  onPressed: () =>
                                      setState(() => hidden = !hidden),
                                  icon: Icon(
                                    hidden
                                        ? Icons.visibility_outlined
                                        : Icons.visibility_off_outlined,
                                  ),
                                ),
                              ),
                              validator: (v) => (v ?? '').isEmpty
                                  ? 'Введите пароль'
                                  : register && (v ?? '').length < 8
                                  ? 'Мин. 8 символов'
                                  : null,
                              onFieldSubmitted: (_) => submit(),
                            ),
                            if (register) ...[
                              const SizedBox(height: 14),
                              TextFormField(
                                controller: password2,
                                obscureText: hidden,
                                decoration: const InputDecoration(
                                  labelText: 'Повторите пароль',
                                ),
                                validator: (v) => v == password.text
                                    ? null
                                    : 'Пароли не совпадают',
                              ),
                              const SizedBox(height: 14),
                              TextFormField(
                                controller: project,
                                decoration: const InputDecoration(
                                  labelText: 'Ваш проект (необязательно)',
                                  hintText: 'Ссылка на канал, бота или сайт',
                                ),
                              ),
                              const SizedBox(height: 14),
                              TextFormField(
                                controller: referral,
                                decoration: const InputDecoration(
                                  labelText: 'Код приглашения (необязательно)',
                                ),
                              ),
                              if (config?['registration_open'] == false)
                                const Padding(
                                  padding: EdgeInsets.only(top: 12),
                                  child: Text(
                                    'Приём новых партнёров временно закрыт.',
                                  ),
                                ),
                            ],
                            if (register)
                              CheckboxListTile(
                                contentPadding: EdgeInsets.zero,
                                value: accepted,
                                onChanged: (v) =>
                                    setState(() => accepted = v ?? false),
                                title: const Text(
                                  'Принимаю условия сервиса и политику конфиденциальности',
                                  style: TextStyle(fontSize: 12),
                                ),
                              ),
                          ],
                          const SizedBox(height: 20),
                          BusyButton(
                            verification
                                ? 'Подтвердить'
                                : register
                                ? 'Зарегистрироваться'
                                : 'Войти',
                            busy: busy,
                            onPressed:
                                oauth != null ||
                                    connecting ||
                                    connectionError != null ||
                                    (register &&
                                        config?['registration_open'] == false)
                                ? null
                                : submit,
                          ),
                        ],
                      ),
                    ),
                  ),
                  if (!verification)
                    Wrap(
                      alignment: WrapAlignment.center,
                      children: [
                        for (final item in [
                          ('/privacy', 'Конфиденциальность'),
                          ('/terms', 'Условия'),
                        ])
                          TextButton(
                            onPressed: () => Navigator.push(
                              context,
                              MaterialPageRoute<void>(
                                builder: (_) => DocumentScreen(
                                  api: widget.api,
                                  path: item.$1,
                                  title: item.$2,
                                ),
                              ),
                            ),
                            child: Text(item.$2),
                          ),
                      ],
                    ),
                  if (!verification)
                    TextButton(
                      onPressed: busy
                          ? null
                          : () => setState(() => register = !register),
                      child: Text(
                        register
                            ? 'Уже есть аккаунт? Войти'
                            : 'Нет аккаунта? Регистрация',
                      ),
                    ),
                  if (!verification)
                    TextButton(
                      onPressed: () => Navigator.push(
                        context,
                        MaterialPageRoute<void>(
                          builder: (_) => Scaffold(
                            appBar: AppBar(
                              title: const Text('Каталог Donatix'),
                            ),
                            body: CatalogScreen(api: widget.api),
                          ),
                        ),
                      ),
                      child: const Text('Посмотреть каталог без регистрации'),
                    ),
                ],
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

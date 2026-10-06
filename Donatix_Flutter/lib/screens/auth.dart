import 'dart:async';
import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:dio/dio.dart';
import '../core/api.dart';
import '../widgets/ui.dart';
import 'document.dart';
import 'catalog.dart';
import 'management.dart';
import '../core/checkout.dart';

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
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    initialize();
  }

  Future<void> initialize() async {
    try {
      final d = await widget.api.get('/api/v1/mobile/config');
      final saved = await widget.api.storage.read(key: 'oauth_pending');
      if (mounted) {
        setState(() {
          config = d;
          if (saved != null) {
            oauth = Map<String, dynamic>.from(jsonDecode(saved) as Map);
          }
        });
      }
      if (oauth != null) claim();
    } catch (e) {
      if (mounted) message(context, e);
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    oauthTimer?.cancel();
    if (state == AppLifecycleState.resumed && oauth != null) claim();
  }

  Future<void> google() async {
    if (busy) return;
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
    oauthTimer?.cancel();
    try {
      final d = await widget.api.post('/api/v1/mobile/oauth/claim', {
        'ticket': oauth!['ticket'],
        'verifier': oauth!['verifier'],
      });
      if (d['pending'] == false) {
        await widget.api.storage.delete(key: 'oauth_pending');
        oauth = null;
        await widget.api.bootstrap();
        if (mounted) widget.onDone();
      }
    } catch (e) {
      if (e is ApiFailure &&
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
    body: SafeArea(
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 480),
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Form(
              key: form,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Center(
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(22),
                      child: Image.asset(
                        'assets/logo.png',
                        width: 84,
                        height: 84,
                      ),
                    ),
                  ),
                  const SizedBox(height: 24),
                  Heading(
                    verification
                        ? 'Подтвердите вход'
                        : register
                        ? 'Создать аккаунт'
                        : 'Войти в Donatix',
                    subtitle: verification
                        ? 'Введите код из Telegram-бота администратора.'
                        : 'Игры, Telegram и сервисы в одном месте',
                  ),
                  Surface(
                    child: Column(
                      children: [
                        if (!verification &&
                            config?['google_enabled'] == true) ...[
                          OutlinedButton.icon(
                            onPressed: busy ? null : google,
                            icon: const Icon(Icons.account_circle_outlined),
                            label: Text(
                              register
                                  ? 'Регистрация через Google'
                                  : 'Войти через Google',
                            ),
                          ),
                          const Padding(
                            padding: EdgeInsets.symmetric(vertical: 10),
                            child: Text('или'),
                          ),
                        ],
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
                            onPressed: () async {
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
                            decoration: const InputDecoration(labelText: 'Код'),
                            keyboardType: TextInputType.number,
                            validator: (v) =>
                                RegExp(r'^\d{6}$').hasMatch(v ?? '')
                                ? null
                                : 'Введите 6 цифр',
                          )
                        else ...[
                          if (register)
                            TextFormField(
                              controller: login,
                              decoration: const InputDecoration(
                                labelText: 'Логин',
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
                            controller: email,
                            decoration: const InputDecoration(
                              labelText: 'Email',
                            ),
                            keyboardType: TextInputType.emailAddress,
                            autofillHints: const [AutofillHints.email],
                            validator: (v) => (v ?? '').contains('@')
                                ? null
                                : 'Введите email',
                          ),
                          const SizedBox(height: 14),
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
                          onPressed: submit,
                        ),
                      ],
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

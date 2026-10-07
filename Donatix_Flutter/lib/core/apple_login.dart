import 'dart:convert';
import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'api.dart';
import 'checkout.dart';

class AppleSignIn {
  static const channel = MethodChannel('tj.donatix.app/native');

  /// The native credential is verified and exchanged by the server, never trusted locally.
  static Future<bool> authenticate(
    DonatixApi api, {
    bool linkAccount = false,
  }) async {
    if (!Platform.isIOS) {
      throw const ApiFailure('Вход через Apple доступен на iPhone и iPad.');
    }
    final verifier = operationId() + operationId();
    final flow = await api.post('/api/v1/mobile/apple/prepare', {
      'challenge': sha256.convert(utf8.encode(verifier)).toString(),
      'link_account': linkAccount,
    });
    final credential = await channel.invokeMapMethod<String, dynamic>(
      'signInWithApple',
      {'nonce': flow['nonce'], 'state': flow['ticket']},
    );
    if (credential == null || credential['state'] != flow['ticket']) {
      throw const ApiFailure('Apple не подтвердил этот запрос входа.');
    }
    final response = await api.post('/api/v1/mobile/apple/claim', {
      'ticket': flow['ticket'],
      'verifier': verifier,
      'identity_token': credential['identityToken'],
      'authorization_code': credential['authorizationCode'],
    });
    api.csrf = response['csrf'] as String;
    await channel.invokeMethod<void>('setAppleUser', {
      'user': credential['user'],
    });
    if (response['verification'] == true) return false;
    await api.bootstrap();
    return true;
  }
}

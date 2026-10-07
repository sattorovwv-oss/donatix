import AuthenticationServices
import Flutter
import UIKit

final class AppleLogin: NSObject, ASAuthorizationControllerDelegate, ASAuthorizationControllerPresentationContextProviding {
  weak var window: UIWindow?
  private var pending: FlutterResult?
  private var controller: ASAuthorizationController?
  private var state: String?
  var revoked: (() -> Void)?

  override init() {
    super.init()
    NotificationCenter.default.addObserver(self, selector: #selector(credentialRevoked),
      name: ASAuthorizationAppleIDProvider.credentialRevokedNotification, object: nil)
  }
  deinit { NotificationCenter.default.removeObserver(self) }
  func signIn(nonce: String, state: String, result: @escaping FlutterResult) {
    guard pending == nil, window != nil, !nonce.isEmpty, !state.isEmpty else {
      result(FlutterError(code: "apple_unavailable", message: "Не удалось открыть вход через Apple.", details: nil)); return
    }
    pending = result
    self.state = state
    let request = ASAuthorizationAppleIDProvider().createRequest()
    request.requestedScopes = [.email, .fullName]
    request.nonce = nonce
    request.state = state
    let controller = ASAuthorizationController(authorizationRequests: [request])
    self.controller = controller
    controller.delegate = self
    controller.presentationContextProvider = self
    controller.performRequests()
  }
  func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor {
    // signIn rejects a missing window before any request is presented.
    return window!
  }
  func authorizationController(controller: ASAuthorizationController, didCompleteWithAuthorization authorization: ASAuthorization) {
    guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
      credential.state == state, let tokenData = credential.identityToken,
      let token = String(data: tokenData, encoding: .utf8), let codeData = credential.authorizationCode,
      let code = String(data: codeData, encoding: .utf8) else {
      finish(FlutterError(code: "apple_invalid", message: "Apple не подтвердил вход.", details: nil)); return
    }
    finish(["identityToken": token, "authorizationCode": code, "state": credential.state ?? "", "user": credential.user])
  }
  func authorizationController(controller: ASAuthorizationController, didCompleteWithError error: Error) {
    let canceled = (error as? ASAuthorizationError)?.code == .canceled
    finish(FlutterError(code: canceled ? "apple_canceled" : "apple_failed",
      message: canceled ? "Вход отменён." : "Apple сейчас не подтвердил вход. Попробуйте снова.", details: nil))
  }
  private func finish(_ value: Any) {
    let result = pending
    pending = nil
    controller = nil
    state = nil
    result?(value)
  }
  func remember(_ user: String) throws { try PrivateStore.write("apple_user", Data(user.utf8)) }
  func clear() { PrivateStore.remove("apple_user") }
  func checkCredential() {
    guard let data = PrivateStore.read("apple_user"), let user = String(data: data, encoding: .utf8) else { return }
    ASAuthorizationAppleIDProvider().getCredentialState(forUserID: user) { [weak self] state, error in
      if error == nil && (state == .revoked || state == .notFound) {
        DispatchQueue.main.async { self?.credentialRevoked() }
      }
    }
  }
  @objc private func credentialRevoked() {
    guard PrivateStore.read("apple_user") != nil else { return }
    clear()
    revoked?()
  }
}

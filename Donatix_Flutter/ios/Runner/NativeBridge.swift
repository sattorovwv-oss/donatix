import Flutter
import UIKit
import FirebaseMessaging

final class NativeBridge {
  let push = NativePush()
  let apple = AppleLogin()
  private let channel: FlutterMethodChannel
  private weak var controller: FlutterViewController?
  init(controller: FlutterViewController) {
    self.controller = controller
    channel = FlutterMethodChannel(name: "tj.donatix.app/native", binaryMessenger: controller.binaryMessenger)
    push.onLink = { [weak self] in self?.channel.invokeMethod("notificationOpened", arguments: nil) }
    apple.revoked = { [weak self] in self?.channel.invokeMethod("appleCredentialRevoked", arguments: nil) }
    channel.setMethodCallHandler { [weak self] call, result in self?.handle(call, result: result) }
  }
  private var shared: URL { FileManager.default.temporaryDirectory.appendingPathComponent("donatix-shared", isDirectory: true) }
  private func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    let args = call.arguments as? [String: Any] ?? [:]
    switch call.method {
    case "configureNotifications":
      guard let origin = args["origin"] as? String, let cookie = args["cookie"] as? String,
        let uid = args["userId"] as? Int else { result(invalid); return }
      do { try push.configure(origin: origin, cookie: cookie, userID: uid); result(nil) }
      catch { result(FlutterError(code: "secure_storage", message: "Не удалось сохранить настройки уведомлений.", details: nil)) }
    case "stopNotifications": push.stop(); result(nil)
    case "requestNotificationPermission": push.permission { result($0) }
    case "pushStatus": result(push.status())
    case "takeNotificationLink": result(push.takeLink())
    case "shareFile": share(args, result: result)
    case "clearPrivateFiles":
      do {
        if FileManager.default.fileExists(atPath: shared.path) { try FileManager.default.removeItem(at: shared) }
        // Temporary images/documents created by the picker may contain receipts.
        for file in try FileManager.default.contentsOfDirectory(at: FileManager.default.temporaryDirectory,
          includingPropertiesForKeys: nil) { try FileManager.default.removeItem(at: file) }
        URLCache.shared.removeAllCachedResponses()
        result(nil)
      } catch { result(FlutterError(code: "file_cleanup", message: "Не удалось удалить временные файлы.", details: nil)) }
    case "signInWithApple":
      guard let nonce = args["nonce"] as? String, let state = args["state"] as? String else { result(invalid); return }
      apple.window = controller?.view.window
      apple.signIn(nonce: nonce, state: state, result: result)
    case "setAppleUser":
      guard let user = args["user"] as? String, !user.isEmpty else { result(invalid); return }
      do { try apple.remember(user); result(nil) } catch { result(invalid) }
    case "clearAppleCredential": apple.clear(); result(nil)
    default: result(FlutterMethodNotImplemented)
    }
  }
  private var invalid: FlutterError { FlutterError(code: "invalid_arguments", message: "Некорректные параметры.", details: nil) }
  private func share(_ args: [String: Any], result: @escaping FlutterResult) {
    guard let controller = controller, controller.presentedViewController == nil,
      let bytes = args["bytes"] as? FlutterStandardTypedData, !bytes.data.isEmpty,
      bytes.data.count <= 32 * 1024 * 1024, let rawName = args["name"] as? String else { result(invalid); return }
    let name = String(rawName.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) }
      .map(String.init).joined().replacingOccurrences(of: "\\", with: "_").replacingOccurrences(of: "/", with: "_").prefix(180))
    guard name != ".", name != "..", !name.isEmpty else { result(invalid); return }
    do {
      let manager = FileManager.default
      try manager.createDirectory(at: shared, withIntermediateDirectories: true)
      for file in try manager.contentsOfDirectory(at: shared, includingPropertiesForKeys: [.creationDateKey]) {
        if let created = try file.resourceValues(forKeys: [.creationDateKey]).creationDate,
          created.timeIntervalSinceNow < -86400 { try? manager.removeItem(at: file) }
      }
      let file = shared.appendingPathComponent(name)
      try bytes.data.write(to: file, options: [.atomic, .completeFileProtectionUnlessOpen])
      var values = URLResourceValues(); values.isExcludedFromBackup = true
      var resource = file; try resource.setResourceValues(values)
      let activity = UIActivityViewController(activityItems: [file], applicationActivities: nil)
      if let popover = activity.popoverPresentationController {
        popover.sourceView = controller.view
        popover.sourceRect = CGRect(x: controller.view.bounds.midX, y: controller.view.bounds.midY, width: 1, height: 1)
        popover.permittedArrowDirections = []
      }
      activity.completionWithItemsHandler = { _, _, _, _ in try? manager.removeItem(at: file) }
      controller.present(activity, animated: true) { result(nil) }
    } catch { result(FlutterError(code: "share_failed", message: "Не удалось подготовить файл к отправке.", details: nil)) }
  }
}

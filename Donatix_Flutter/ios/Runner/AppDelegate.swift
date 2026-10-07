import Flutter
import UIKit
import UserNotifications

@main
@objc class AppDelegate: FlutterAppDelegate {
  private var native: NativeBridge?
  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)
    let started = super.application(application, didFinishLaunchingWithOptions: launchOptions)
    if let controller = window?.rootViewController as? FlutterViewController {
      native = NativeBridge(controller: controller)
      UNUserNotificationCenter.current().delegate = self
      if let payload = launchOptions?[.remoteNotification] as? [AnyHashable: Any] {
        native?.push.opened(payload)
      }
    }
    return started
  }
  override func applicationDidBecomeActive(_ application: UIApplication) {
    super.applicationDidBecomeActive(application)
    native?.push.resumed()
    native?.apple.checkCredential()
  }
  override func application(_ application: UIApplication, open url: URL,
    options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
    if url.scheme == "donatix" && url.host == "oauth" { return true }
    return super.application(application, open: url, options: options)
  }
  override func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
    native?.push.didRegisterAPNs(deviceToken)
    super.application(application, didRegisterForRemoteNotificationsWithDeviceToken: deviceToken)
  }
  override func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
    withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
    completionHandler(native?.push.present(notification.request.content.userInfo) == true ? [.banner, .list, .sound] : [])
  }
  override func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
    withCompletionHandler completionHandler: @escaping () -> Void) {
    native?.push.opened(response.notification.request.content.userInfo)
    completionHandler()
  }
}

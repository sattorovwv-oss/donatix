import Foundation
import UIKit
import UserNotifications
import FirebaseCore
import FirebaseMessaging

private struct PushSession: Codable, Equatable {
  let origin: String
  let cookie: String
  let userId: Int
}

// All state transitions run on the main queue. Network requests never follow redirects.
final class NativePush: NSObject, MessagingDelegate, URLSessionTaskDelegate {
  private var current: PushSession?
  private var binding: String?
  private var generation = 0
  private var registering = false
  private var retry: DispatchWorkItem?
  private var attempt = 0
  private var registered = false
  private var server = false
  private var link: String?
  private var tokenDeleting = false
  private let defaults = UserDefaults.standard
  private lazy var network: URLSession = {
    let config = URLSessionConfiguration.ephemeral
    config.timeoutIntervalForRequest = 20
    config.timeoutIntervalForResource = 40
    config.httpCookieStorage = nil
    config.urlCache = nil
    return URLSession(configuration: config, delegate: self, delegateQueue: nil)
  }()
  private(set) var configured = false
  var onLink: (() -> Void)?

  override init() {
    super.init()
    if defaults.bool(forKey: "donatix_push_enabled"), let data = PrivateStore.read("push_session") {
      current = try? JSONDecoder().decode(PushSession.self, from: data)
    } else {
      PrivateStore.remove("push_session")
      PrivateStore.remove("push_binding")
    }
    binding = PrivateStore.read("push_binding").flatMap { String(data: $0, encoding: .utf8) }
    if let path = Bundle.main.path(forResource: "GoogleService-Info", ofType: "plist"),
       let options = FirebaseOptions(contentsOfFile: path),
       options.bundleID == Bundle.main.bundleIdentifier {
      if FirebaseApp.app() == nil { FirebaseApp.configure(options: options) }
      configured = true
      Messaging.messaging().delegate = self
      Messaging.messaging().isAutoInitEnabled = current != nil
    }
  }

  private var deviceID: String {
    if let id = defaults.string(forKey: "donatix_push_device") { return id }
    let id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    defaults.set(id, forKey: "donatix_push_device")
    return id
  }
  func status() -> [String: Any] {
    ["firebase": configured, "registered": registered, "server": server, "deviceId": deviceID]
  }
  func configure(origin: String, cookie: String, userID: Int) throws {
    guard let url = URL(string: origin), url.scheme == "https", url.host != nil,
      url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
      url.path.isEmpty || url.path == "/", userID > 0, !cookie.isEmpty,
      !cookie.contains("\r"), !cookie.contains("\n"), !cookie.contains(";") else {
      throw NSError(domain: "Donatix", code: 1)
    }
    let value = PushSession(origin: origin.trimmingCharacters(in: CharacterSet(charactersIn: "/")), cookie: cookie, userId: userID)
    if current == value { register(); return }
    try PrivateStore.write("push_session", JSONEncoder().encode(value))
    defaults.set(true, forKey: "donatix_push_enabled")
    if current?.userId != userID {
      UNUserNotificationCenter.current().removeAllDeliveredNotifications()
      defaults.removeObject(forKey: "donatix_seen_notifications")
      link = nil
      binding = nil
      PrivateStore.remove("push_binding")
    }
    generation += 1
    registering = false
    retry?.cancel()
    attempt = 0
    registered = false
    server = false
    current = value
    if configured { Messaging.messaging().isAutoInitEnabled = true }
    register()
  }
  func stop() {
    let old = current
    generation += 1
    retry?.cancel()
    registering = false
    registered = false
    server = false
    current = nil
    defaults.set(false, forKey: "donatix_push_enabled")
    binding = nil
    link = nil
    PrivateStore.remove("push_session")
    PrivateStore.remove("push_binding")
    UNUserNotificationCenter.current().removeAllDeliveredNotifications()
    UNUserNotificationCenter.current().removeAllPendingNotificationRequests()
    if let old = old {
      exchange(old, path: "/api/v1/mobile-session") { [weak self] session in
        guard let self = self, let csrf = session?["csrf"] as? String else { return }
        self.exchange(old, path: "/api/v1/mobile/push/unregister", csrf: csrf,
          body: ["device_id": self.deviceID]) { _ in }
      }
    }
    if configured && !tokenDeleting {
      tokenDeleting = true
      Messaging.messaging().isAutoInitEnabled = false
      Messaging.messaging().deleteToken { [weak self] _ in
        DispatchQueue.main.async {
          self?.tokenDeleting = false
          if self?.current != nil {
            Messaging.messaging().isAutoInitEnabled = true
            self?.register()
          }
        }
      }
    }
  }
  func permission(completion: @escaping (Bool) -> Void) {
    UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
      DispatchQueue.main.async {
        if granted { UIApplication.shared.registerForRemoteNotifications() }
        completion(granted)
      }
    }
  }
  func didRegisterAPNs(_ token: Data) {
    if configured { Messaging.messaging().apnsToken = token; register() }
  }
  func messaging(_ messaging: Messaging, didReceiveRegistrationToken fcmToken: String?) {
    DispatchQueue.main.async { [weak self] in self?.registered = false; self?.register() }
  }
  func resumed() {
    retry?.cancel()
    if !registered { register() }
  }
  private func register() {
    guard configured, current != nil, !registered, !registering, !tokenDeleting else { return }
    let expected = generation
    registering = true
    UNUserNotificationCenter.current().getNotificationSettings { [weak self] settings in
      DispatchQueue.main.async {
        guard let self = self, expected == self.generation else { return }
        guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
          self.registering = false
          return
        }
        UIApplication.shared.registerForRemoteNotifications()
        guard Messaging.messaging().apnsToken != nil else { self.failed(expected); return }
        Messaging.messaging().token { token, _ in
          DispatchQueue.main.async {
            guard expected == self.generation, let session = self.current else { return }
            guard let token = token else { self.failed(expected); return }
            self.exchange(session, path: "/api/v1/mobile-session") { json in
              guard expected == self.generation else { return }
              guard let csrf = json?["csrf"] as? String, json?["user_id"] as? Int == session.userId else {
                self.failed(expected); return
              }
              self.exchange(session, path: "/api/v1/mobile/push/register", csrf: csrf,
                body: ["device_id": self.deviceID, "token": token, "platform": "ios"]) { reply in
                guard expected == self.generation else { return }
                guard reply?["ok"] as? Bool == true, let binding = reply?["binding"] as? String else {
                  self.failed(expected); return
                }
                do { try PrivateStore.write("push_binding", Data(binding.utf8)) }
                catch { self.failed(expected); return }
                self.binding = binding
                self.registered = true
                self.server = reply?["configured"] as? Bool ?? false
                self.registering = false
                self.attempt = 0
              }
            }
          }
        }
      }
    }
  }
  private func failed(_ expected: Int) {
    guard expected == generation else { return }
    registering = false
    retry?.cancel()
    guard current != nil, attempt < 6 else { return }
    attempt += 1
    let work = DispatchWorkItem { [weak self] in
      guard self?.generation == expected else { return }
      self?.register()
    }
    retry = work
    DispatchQueue.main.asyncAfter(deadline: .now() + Double(min(60, 1 << attempt)), execute: work)
  }
  private func exchange(_ session: PushSession, path: String, csrf: String? = nil,
                        body: [String: Any]? = nil, completion: @escaping ([String: Any]?) -> Void) {
    guard let url = URL(string: session.origin + path) else { completion(nil); return }
    var request = URLRequest(url: url)
    request.setValue("dx_session=\(session.cookie)", forHTTPHeaderField: "Cookie")
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    if let body = body {
      request.httpMethod = "POST"
      request.setValue("application/json", forHTTPHeaderField: "Content-Type")
      request.setValue(csrf, forHTTPHeaderField: "X-CSRF-Token")
      request.httpBody = try? JSONSerialization.data(withJSONObject: body)
    }
    network.dataTask(with: request) { data, response, _ in
      var json: [String: Any]?
      if let response = response as? HTTPURLResponse, response.statusCode == 200,
         let data = data, data.count <= 1_048_576 {
        json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
      }
      DispatchQueue.main.async { completion(json) }
    }.resume()
  }
  func urlSession(_ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }

  private func matches(_ payload: [AnyHashable: Any]) -> Bool {
    guard let current = current, let binding = binding,
      String(describing: payload["user_id"] ?? "") == String(current.userId),
      payload["binding"] as? String == binding else { return false }
    return true
  }
  func present(_ payload: [AnyHashable: Any]) -> Bool {
    guard matches(payload), let id = payload["notification_id"] as? String else { return false }
    var seen = defaults.stringArray(forKey: "donatix_seen_notifications") ?? []
    guard !seen.contains(id) else { return false }
    seen.append(id)
    defaults.set(Array(seen.suffix(200)), forKey: "donatix_seen_notifications")
    return true
  }
  func opened(_ payload: [AnyHashable: Any]) {
    guard matches(payload) else { return }
    let candidate = payload["link"] as? String ?? ""
    link = candidate.hasPrefix("/panel/") && !candidate.contains("\\") && !candidate.contains("\n")
      ? candidate : "/panel/notifications"
    onLink?()
  }
  func takeLink() -> String? { let value = link; link = nil; return value }
}

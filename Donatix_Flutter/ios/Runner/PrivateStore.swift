import Foundation
import Security

// Native session and Apple subject remain in this application's Keychain.
enum PrivateStore {
  private static let service = "tj.donatix.app.native"
  static func read(_ key: String) -> Data? {
    let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service, kSecAttrAccount as String: key,
      kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
    var value: CFTypeRef?
    guard SecItemCopyMatching(query as CFDictionary, &value) == errSecSuccess else { return nil }
    return value as? Data
  }
  static func write(_ key: String, _ value: Data) throws {
    let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service, kSecAttrAccount as String: key]
    let attributes: [String: Any] = [kSecValueData as String: value,
      kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
    let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
    if status == errSecItemNotFound {
      let added = SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil)
      if added != errSecSuccess { throw NSError(domain: NSOSStatusErrorDomain, code: Int(added)) }
    } else if status != errSecSuccess {
      throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
    }
  }
  static func remove(_ key: String) {
    SecItemDelete([kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service, kSecAttrAccount as String: key] as CFDictionary)
  }
}

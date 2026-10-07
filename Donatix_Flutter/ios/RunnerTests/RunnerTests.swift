import Flutter
import UIKit
import XCTest
@testable import Runner

class RunnerTests: XCTestCase {

  func testNativeSessionRejectsUnsafeServerOrigins() {
    let service = NativePush()
    for origin in ["http://donatix.tj", "https://user:password@donatix.tj", "https://donatix.tj/path", "https://donatix.tj?redirect=x", "https://donatix.tj#x"] {
      XCTAssertThrowsError(try service.configure(origin: origin, cookie: "example-session", userID: 1))
    }
    XCTAssertThrowsError(try service.configure(origin: "https://donatix.tj", cookie: "bad\r\ncookie", userID: 1))
    XCTAssertThrowsError(try service.configure(origin: "https://donatix.tj", cookie: "cookie", userID: 0))
  }
  func testPrivateStorageRoundTripUpdateAndErasure() throws {
    let key = "test-" + UUID().uuidString
    defer { PrivateStore.remove(key) }
    XCTAssertNil(PrivateStore.read(key))
    try PrivateStore.write(key, Data("first-test-value".utf8))
    XCTAssertEqual(PrivateStore.read(key), Data("first-test-value".utf8))
    try PrivateStore.write(key, Data("updated-test-value".utf8))
    XCTAssertEqual(PrivateStore.read(key), Data("updated-test-value".utf8))
    PrivateStore.remove(key)
    XCTAssertNil(PrivateStore.read(key))
  }

}

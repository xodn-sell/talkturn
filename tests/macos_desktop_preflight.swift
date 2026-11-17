// Read-only prerequisites. Never asks for permissions, opens a window, or reads a chat/account.
import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import SystemConfiguration

let consoleUser = SCDynamicStoreCopyConsoleUser(nil, nil, nil) as String?
var displayCount: UInt32 = 0
let displayStatus = CGGetActiveDisplayList(0, nil, &displayCount)
let kakao = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.kakao.KakaoTalkMac")
let appStore = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.AppStore")
let report: [String: Any] = [
    "system": ProcessInfo.processInfo.operatingSystemVersionString,
    "console_session_present": consoleUser != nil && consoleUser != "loginwindow" && consoleUser != "root",
    "display_query_succeeded": displayStatus == .success,
    "active_displays": Int(displayCount),
    "accessibility_trusted": AXIsProcessTrusted(),
    "screen_capture_permission": CGPreflightScreenCaptureAccess(),
    "kakaotalk_installed": kakao != nil,
    "app_store_installed": appStore != nil,
    "apple_account_authentication": "not inspected",
    "interactive_login_connection": "not configured",
    "permissions_changed": false,
    "account_or_chat_content_read": false,
    "app_installation_attempted": false
]
let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
print(String(decoding: data, as: UTF8.self))

// Siri for Scout (docs/siri-design.md), added to the app target by withSiriIntents.js.
//
// "Ask Scout" (Siri then asks for the question) and a few fixed questions in one phrase.
// Each is sent to Scout on the Mac (POST /api/phone/ask, over Tailscale) and Scout's answer
// is what Siri says. Nothing here runs JavaScript: the request is signed with the phone's
// second key, which src/siri.ts keeps in the Keychain readable after the first unlock, so
// it works with the phone locked. Scout decides what that key may do ([phone] locked_key).
//
//   sig = hex(HMAC-SHA256(base64url-decoded secret, "device|ts|nonce|scout:ask|<question>"))

import AppIntents
import CryptoKit
import Foundation
import Security

@available(iOS 17.0, *)
struct ScoutSiriKey: Decodable {
  let server: String
  let device: String
  let secret: String
}

@available(iOS 17.0, *)
enum ScoutSiri {
  // expo-secure-store's layout for keychainService "scout.siri", key "scout.siri.key".
  static let service = "scout.siri:no-auth"
  static let account = "scout.siri.key"
  static let timeout: TimeInterval = 25  // Scout answers within 20 s, or says it will notify

  static func key() -> ScoutSiriKey? {
    let name = Data(account.utf8)
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrGeneric as String: name,
      kSecAttrAccount as String: name,
      kSecMatchLimit as String: kSecMatchLimitOne,
      kSecReturnData as String: true,
    ]
    var item: CFTypeRef?
    guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else {
      return nil
    }
    return try? JSONDecoder().decode(ScoutSiriKey.self, from: data)
  }

  static func base64url(_ text: String) -> Data? {
    var t = text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    while t.count % 4 != 0 { t += "=" }
    return Data(base64Encoded: t)
  }

  static func nonce() -> String {
    var bytes = [UInt8](repeating: 0, count: 12)
    _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    return Data(bytes).base64EncodedString()
      .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
      .replacingOccurrences(of: "=", with: "")
  }

  /// Scout's answer to `question`, or what went wrong, said plainly.
  static func ask(_ question: String) async -> String {
    guard let k = key(), let secret = base64url(k.secret), let url = URL(string: k.server + "/api/phone/ask") else {
      return "Open the Scout app once to set up Siri."
    }
    let ts = Int(Date().timeIntervalSince1970)
    let n = nonce()
    let message = "\(k.device)|\(ts)|\(n)|scout:ask|\(question)"
    let mac = HMAC<SHA256>.authenticationCode(for: Data(message.utf8), using: SymmetricKey(data: secret))
    let sig = mac.map { String(format: "%02x", $0) }.joined()
    let body: [String: Any] = [
      "device": k.device, "ts": ts, "nonce": n, "sig": sig, "key": "locked", "text": question,
    ]
    var request = URLRequest(url: url, timeoutInterval: timeout)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    request.httpBody = try? JSONSerialization.data(withJSONObject: body)
    do {
      let (data, response) = try await URLSession.shared.data(for: request)
      let status = (response as? HTTPURLResponse)?.statusCode ?? 0
      if status == 401 {
        return "Scout didn't accept this phone for Siri. Open the Scout app to set it up again."
      }
      let answer = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
      if let text = answer?["text"] as? String, !text.isEmpty { return text }
      if let error = answer?["error"] as? String, !error.isEmpty { return "Scout couldn't answer: \(error)" }
      return "Scout answered with something I didn't understand."
    } catch {
      return "I can't reach Scout. Is Tailscale on?"
    }
  }
}

@available(iOS 17.0, *)
struct AskScoutIntent: AppIntent {
  static let title: LocalizedStringResource = "Ask Scout"
  static let description = IntentDescription("Ask Scout anything it knows: your tasks, meetings, Slack, the weather, the road.")
  static let openAppWhenRun = false

  @Parameter(title: "Question", requestValueDialog: IntentDialog("What do you want to ask Scout?"))
  var question: String

  func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
    let answer = await ScoutSiri.ask(question)
    return .result(value: answer, dialog: IntentDialog(stringLiteral: answer))
  }
}

@available(iOS 17.0, *)
struct ScoutBriefingIntent: AppIntent {
  static let title: LocalizedStringResource = "Scout briefing"
  static let description = IntentDescription("Today's calendar, what's due and your mail, from Scout.")
  static let openAppWhenRun = false

  func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
    let answer = await ScoutSiri.ask("brief me")
    return .result(value: answer, dialog: IntentDialog(stringLiteral: answer))
  }
}

@available(iOS 17.0, *)
struct ScoutWorkOnIntent: AppIntent {
  static let title: LocalizedStringResource = "What should I work on"
  static let description = IntentDescription("The top of your Scout task list.")
  static let openAppWhenRun = false

  func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
    let answer = await ScoutSiri.ask("what should I work on?")
    return .result(value: answer, dialog: IntentDialog(stringLiteral: answer))
  }
}

@available(iOS 17.0, *)
struct ScoutWaitingOnIntent: AppIntent {
  static let title: LocalizedStringResource = "What am I waiting on"
  static let description = IntentDescription("What others owe you, from your Scout task list.")
  static let openAppWhenRun = false

  func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
    let answer = await ScoutSiri.ask("what am I waiting on?")
    return .result(value: answer, dialog: IntentDialog(stringLiteral: answer))
  }
}

@available(iOS 17.0, *)
struct ScoutStarlinkIntent: AppIntent {
  static let title: LocalizedStringResource = "Is Starlink down"
  static let description = IntentDescription("Whether Scout's Starlink dish is online.")
  static let openAppWhenRun = false

  func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
    let answer = await ScoutSiri.ask("is Starlink down?")
    return .result(value: answer, dialog: IntentDialog(stringLiteral: answer))
  }
}

@available(iOS 17.0, *)
struct ScoutShortcuts: AppShortcutsProvider {
  static var appShortcuts: [AppShortcut] {
    AppShortcut(
      intent: AskScoutIntent(),
      phrases: ["Ask \(.applicationName)", "Ask \(.applicationName) a question"],
      shortTitle: "Ask Scout",
      systemImageName: "questionmark.bubble"
    )
    AppShortcut(
      intent: ScoutBriefingIntent(),
      phrases: ["\(.applicationName) briefing", "Brief me with \(.applicationName)"],
      shortTitle: "Briefing",
      systemImageName: "sun.max"
    )
    AppShortcut(
      intent: ScoutWorkOnIntent(),
      phrases: ["What should I work on in \(.applicationName)", "\(.applicationName) what's next"],
      shortTitle: "What's next",
      systemImageName: "checklist"
    )
    AppShortcut(
      intent: ScoutWaitingOnIntent(),
      phrases: ["What am I waiting on in \(.applicationName)"],
      shortTitle: "Waiting on",
      systemImageName: "hourglass"
    )
    AppShortcut(
      intent: ScoutStarlinkIntent(),
      phrases: ["Is Starlink down in \(.applicationName)", "\(.applicationName) Starlink status"],
      shortTitle: "Starlink",
      systemImageName: "antenna.radiowaves.left.and.right"
    )
  }
}

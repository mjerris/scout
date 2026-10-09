// scout-messages: read-only access to the Mac's Messages history (iMessage and SMS)
// and to Mail's message index, for Scout, through a narrow API on a Unix socket. It is
// the one program that needs Full Disk Access (Messages keeps chat.db behind it, Mail
// its "Envelope Index"); Scout's Python, Claude and the terminal never get it. It runs
// as its own login item (com.local.scout.messages), so macOS checks the grant against
// this binary, not whoever started it.
//
//   scout-messages serve [--home DIR] [--db PATH] [--mail-index PATH] [--socket PATH]
//                        [--token PATH] [--log PATH] [--contacts none|FILE.json] [--rate N]
//   scout-messages probe [--db PATH]     access check in a fresh process: granted|denied|missing
//   scout-messages probe-mail [--mail-index PATH]                  the same for Mail's index
//
// Protocol: one JSON object per line, one request per connection:
//   {"token": "...", "op": "status"|"recent"|"from"|"unread"|"chat"|"search"
//                          |"mail_status"|"mail_recent"|"mail_search", "args": {...}}
// The answer is one JSON line: {"ok": true, ...} or {"ok": false, "code": "...", "error": "..."}.
// The token is random per start, in DATA/state/messages_token (0600); the socket is 0600
// and only answers this user. Limits: at most 50 messages per answer, 365 days back,
// --rate requests a minute (status is free). No attachments, no sending, no SQL from
// callers: each op is a fixed query with bound parameters. Every request is logged
// (op, numbers, caller; never message text or search words) to DATA/logs/messages-access.log.
//
// The databases are opened read-only (SQLITE_OPEN_READONLY, query_only). Not `immutable`
// while Messages or Mail has one open: new rows go to the -wal file first, which that
// would skip. Mail answers are envelopes only (sender, subject, date, read, mailbox),
// never message bodies: the index doesn't hold them, and Scout reads a body through Mail.

import Contacts
import Darwin
import Foundation
import SQLite3
import Security

// MARK: - small helpers

func warn(_ message: String) {
    FileHandle.standardError.write(Data(("scout-messages: " + message + "\n").utf8))
}

func die(_ message: String) -> Never {
    warn(message)
    exit(1)
}

func jsonLine(_ obj: [String: Any]) -> Data {
    var data = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data("{}".utf8)
    data.append(0x0A)
    return data
}

func isoLocal(_ d: Date) -> String {
    let f = ISO8601DateFormatter()
    f.timeZone = TimeZone.current
    f.formatOptions = [.withInternetDateTime]
    return f.string(from: d)
}

/// Apple's message dates: nanoseconds since 2001-01-01 (seconds before macOS 10.13).
let appleEpoch: TimeInterval = 978_307_200

func appleDate(_ raw: Int64) -> Date {
    let seconds = raw > 100_000_000_000 ? Double(raw) / 1e9 : Double(raw)
    return Date(timeIntervalSince1970: seconds + appleEpoch)
}

func appleNanos(daysAgo days: Int) -> Int64 {
    let t = Date().timeIntervalSince1970 - Double(days) * 86400 - appleEpoch
    return Int64(t * 1e9)
}

// MARK: - options

struct Options {
    var db: String
    var socket: String
    var token: String
    var log: String
    var mailIndex = ""  // "" = the newest ~/Library/Mail/V*/MailData/Envelope Index
    var contacts = ""  // "" = the Contacts app (asked on first use), "none" = off, else a JSON file
    var ratePerMinute = 60
}

func parseOptions(_ args: ArraySlice<String>) -> Options {
    let home = NSHomeDirectory()
    var data = home + "/Library/Application Support/Scout"
    var pairs: [(String, String)] = []
    var it = args.makeIterator()
    while let a = it.next() {
        guard a.hasPrefix("--") else { die("unexpected argument: \(a)") }
        guard let v = it.next() else { die("\(a) needs a value") }
        let value = (v as NSString).expandingTildeInPath
        if a == "--home" { data = value } else { pairs.append((a, value)) }
    }
    var o = Options(
        db: home + "/Library/Messages/chat.db",
        socket: data + "/state/messages.sock",
        token: data + "/state/messages_token",
        log: data + "/logs/messages-access.log"
    )
    for (key, value) in pairs {
        switch key {
        case "--db": o.db = value
        case "--mail-index": o.mailIndex = value
        case "--socket": o.socket = value
        case "--token": o.token = value
        case "--log": o.log = value
        case "--contacts": o.contacts = value
        case "--rate":
            guard let n = Int(value), n > 0, n <= 10000 else { die("--rate must be 1-10000") }
            o.ratePerMinute = n
        default: die("unknown option \(key)")
        }
    }
    return o
}

// MARK: - access

enum Access: String {
    case granted, denied, missing
}

/// Can this process read the file? Without Full Disk Access, macOS refuses the open
/// with EPERM (there is no prompt for this permission).
func checkAccess(_ path: String) -> Access {
    let fd = open(path, O_RDONLY)
    if fd < 0 {
        return errno == ENOENT || errno == ENOTDIR ? .missing : .denied
    }
    var byte: UInt8 = 0
    let n = read(fd, &byte, 1)
    close(fd)
    return n >= 0 ? .granted : .denied
}

/// Where Mail keeps its index: the newest V<N> folder under ~/Library/Mail that has
/// MailData/Envelope Index (V10 on recent macOS), unless --mail-index names a file.
/// Without Full Disk Access, macOS refuses even the listing of ~/Library/Mail.
func findMailIndex(_ o: Options) -> (path: String, access: Access) {
    if !o.mailIndex.isEmpty { return (o.mailIndex, checkAccess(o.mailIndex)) }
    let root = NSHomeDirectory() + "/Library/Mail"
    guard let dir = opendir(root) else {
        return ("", errno == ENOENT || errno == ENOTDIR ? .missing : .denied)
    }
    var versions: [Int] = []
    while let entry = readdir(dir) {
        let name = withUnsafeBytes(of: entry.pointee.d_name) { raw in
            String(decoding: raw.prefix(while: { $0 != 0 }), as: UTF8.self)
        }
        if name.hasPrefix("V"), let n = Int(name.dropFirst()), n > 0 { versions.append(n) }
    }
    closedir(dir)
    for n in versions.sorted(by: >) {
        let path = "\(root)/V\(n)/MailData/Envelope Index"
        let access = checkAccess(path)
        if access != .missing { return (path, access) }
    }
    return ("", .missing)
}

/// The same check in a fresh process, so a grant made while this one runs shows up.
func probeAccess(_ path: String) -> Access {
    probe(["probe", "--db", path], fallback: { checkAccess(path) })
}

func probeMailAccess(_ o: Options) -> Access {
    probe(["probe-mail"] + (o.mailIndex.isEmpty ? [] : ["--mail-index", o.mailIndex]),
          fallback: { findMailIndex(o).access })
}

func probe(_ args: [String], fallback: () -> Access) -> Access {
    guard let exe = Bundle.main.executableURL else { return fallback() }
    let p = Process()
    p.executableURL = exe
    p.arguments = args
    let out = Pipe()
    p.standardOutput = out
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return fallback() }
    p.waitUntilExit()
    let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    return Access(rawValue: text.trimmingCharacters(in: .whitespacesAndNewlines)) ?? .denied
}

// MARK: - attributedBody

/// The plain text of a message stored only as `attributedBody`: an NSAttributedString
/// in NSArchiver's typedstream format. Its first NSString holds the text: after the
/// class name come 0x01 0x9N 0x84 0x01 '+', a length (one byte; 0x81 + 2 bytes or
/// 0x82 + 4 bytes, little-endian) and the UTF-8 bytes. Returns nil for anything else.
func decodeAttributedBody(_ blob: [UInt8]) -> String? {
    guard blob.count > 16, blob[2..<13].elementsEqual(Array("streamtyped".utf8)) else { return nil }
    let marker = Array("NSString".utf8)
    var at = -1
    var i = 0
    while i + marker.count <= blob.count {
        if blob[i] == marker[0], blob[i..<(i + marker.count)].elementsEqual(marker) {
            at = i + marker.count
            break
        }
        i += 1
    }
    guard at >= 0 else { return nil }
    var j = at
    let window = min(blob.count - 3, at + 12)
    while j < window, !(blob[j] == 0x84 && blob[j + 1] == 0x01 && blob[j + 2] == 0x2B) { j += 1 }
    guard j < window else { return nil }
    j += 3
    guard j < blob.count else { return nil }
    var length = 0
    let lead = blob[j]
    j += 1
    switch lead {
    case 0x81:
        guard j + 2 <= blob.count else { return nil }
        length = Int(blob[j]) | Int(blob[j + 1]) << 8
        j += 2
    case 0x82:
        guard j + 4 <= blob.count else { return nil }
        length = Int(blob[j]) | Int(blob[j + 1]) << 8 | Int(blob[j + 2]) << 16 | Int(blob[j + 3]) << 24
        j += 4
    case 0..<0x80:
        length = Int(lead)
    default:
        return nil
    }
    guard length >= 0, j + length <= blob.count else { return nil }
    return String(bytes: blob[j..<(j + length)], encoding: .utf8)
}

// MARK: - contact names

func normalizeHandle(_ handle: String) -> String {
    if handle.contains("@") { return handle.lowercased() }
    let digits = handle.filter(\.isNumber)
    if digits.count < 7 { return handle.lowercased() }
    return String(digits.suffix(10))  // +1 (555) 010-2000 and 5550102000 match
}

func fold(_ s: String) -> String {
    s.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
}

/// Handle (phone or email) to a person's name, from Contacts or a test file. Contacts
/// asks the user once, the first time a request needs names; until then, and when it's
/// refused, messages show the raw handle.
final class Names {
    let mode: String
    private var people: [(name: String, handles: [String])] = []
    private var byHandle: [String: String] = [:]
    private var loadedAt = Date.distantPast
    private var asked = false

    init(mode: String) { self.mode = mode }

    func status() -> String {
        if mode == "none" { return "off" }
        if !mode.isEmpty { return "file" }
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: return "authorized"
        case .notDetermined: return "not_determined"
        case .denied: return "denied"
        case .restricted: return "restricted"
        @unknown default: return "limited"
        }
    }

    private func load() {
        if Date().timeIntervalSince(loadedAt) < 600 { return }
        loadedAt = Date()
        people = []
        if mode == "none" { return }
        if !mode.isEmpty {
            guard let data = FileManager.default.contents(atPath: mode),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: [String]]
            else {
                warn("can't read contacts file \(mode)")
                return
            }
            people = obj.map { ($0.key, $0.value) }
        } else {
            loadContacts()
        }
        byHandle = [:]
        for p in people {
            for h in p.handles { byHandle[normalizeHandle(h)] = p.name }
        }
    }

    private func loadContacts() {
        let store = CNContactStore()
        if CNContactStore.authorizationStatus(for: .contacts) == .notDetermined {
            // Ask once, without waiting for the click (the server answers one request at
            // a time); this answer shows numbers, and names follow from a few seconds on.
            if !asked {
                asked = true
                store.requestAccess(for: .contacts) { _, _ in }
            }
            loadedAt = Date().addingTimeInterval(-590)
            return
        }
        guard CNContactStore.authorizationStatus(for: .contacts) == .authorized else { return }
        let keys = [
            CNContactGivenNameKey, CNContactFamilyNameKey, CNContactNicknameKey,
            CNContactOrganizationNameKey, CNContactPhoneNumbersKey, CNContactEmailAddressesKey,
        ] as [CNKeyDescriptor]
        do {
            try store.enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) { c, _ in
                var name = [c.givenName, c.familyName].filter { !$0.isEmpty }.joined(separator: " ")
                if name.isEmpty { name = c.nickname.isEmpty ? c.organizationName : c.nickname }
                if name.isEmpty { return }
                let handles = c.phoneNumbers.map(\.value.stringValue) + c.emailAddresses.map { $0.value as String }
                if !handles.isEmpty { self.people.append((name, handles)) }
            }
        } catch {
            warn("reading contacts failed: \(error.localizedDescription)")
        }
    }

    func name(for handle: String) -> String? {
        load()
        return byHandle[normalizeHandle(handle)]
    }

    /// People whose name matches every word of `query` (each a prefix of a name word).
    func matching(_ query: String) -> [(name: String, handles: [String])] {
        load()
        let words = fold(query).split(separator: " ").map(String.init)
        if words.isEmpty { return [] }
        return people.filter { p in
            let nameWords = fold(p.name).split(separator: " ").map(String.init)
            return words.allSatisfy { w in nameWords.contains { $0.hasPrefix(w) } }
        }
    }
}

// MARK: - database

struct Failure: Error {
    let code: String
    let message: String
}

enum Bind {
    case int(Int64)
    case text(String)
}

final class Database {
    private var db: OpaquePointer?
    private let what: String

    /// Read-only. While Messages runs, its -wal and -shm files exist and a read-only
    /// connection reads through them. When neither exists (nothing has the database
    /// open, so everything is in the main file) a read-only connection can't create
    /// -shm; then it opens as `immutable`, which is exact in that state.
    init(path: String, what: String = "the Messages database") throws {
        self.what = what
        try open(path, "mode=ro")
        if sqlite3_exec(db, "SELECT 1 FROM sqlite_master LIMIT 1", nil, nil, nil) == SQLITE_CANTOPEN,
           !FileManager.default.fileExists(atPath: path + "-wal")
        {
            sqlite3_close(db)
            db = nil
            try open(path, "mode=ro&immutable=1")
        }
    }

    private func open(_ path: String, _ params: String) throws {
        let encoded = path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? path
        let flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_URI | SQLITE_OPEN_NOMUTEX
        if sqlite3_open_v2("file:\(encoded)?\(params)", &db, flags, nil) != SQLITE_OK {
            let message = db.map { String(cString: sqlite3_errmsg($0)) } ?? "unknown error"
            sqlite3_close(db)
            db = nil
            throw Failure(code: "db_error", message: "can't open \(what): \(message)")
        }
        sqlite3_busy_timeout(db, 3000)
        sqlite3_exec(db, "PRAGMA query_only = 1", nil, nil, nil)
    }

    deinit { sqlite3_close(db) }

    func rows(_ sql: String, _ binds: [Bind], _ each: (OpaquePointer) throws -> Bool) throws {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let st = stmt else {
            throw Failure(code: "db_error", message: "query failed: \(String(cString: sqlite3_errmsg(db)))")
        }
        defer { sqlite3_finalize(st) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        for (n, b) in binds.enumerated() {
            switch b {
            case .int(let v): sqlite3_bind_int64(st, Int32(n + 1), v)
            case .text(let s): sqlite3_bind_text(st, Int32(n + 1), s, -1, transient)
            }
        }
        while true {
            let rc = sqlite3_step(st)
            if rc == SQLITE_DONE { return }
            guard rc == SQLITE_ROW else {
                throw Failure(code: "db_error", message: "query failed: \(String(cString: sqlite3_errmsg(db)))")
            }
            if try !each(st) { return }
        }
    }
}

func columnText(_ st: OpaquePointer, _ i: Int32) -> String {
    guard let p = sqlite3_column_text(st, i) else { return "" }
    return String(cString: p)
}

func columnBlob(_ st: OpaquePointer, _ i: Int32) -> [UInt8] {
    let n = Int(sqlite3_column_bytes(st, i))
    guard n > 0, let p = sqlite3_column_blob(st, i) else { return [] }
    return Array(UnsafeRawBufferPointer(start: p, count: n))
}

// MARK: - queries

let maxLimit = 50
let maxDays = 365
let maxText = 500
let maxScan = 20000

let selectMessages = """
    SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me, m.is_read, m.service,
           m.cache_has_attachments, COALESCE(h.id, ''), COALESCE(c.ROWID, 0),
           COALESCE(c.chat_identifier, ''), COALESCE(c.display_name, ''), COALESCE(c.style, 0)
    FROM message m
    LEFT JOIN handle h ON h.ROWID = m.handle_id
    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
    LEFT JOIN chat c ON c.ROWID = cmj.chat_id
    WHERE m.associated_message_type = 0 AND m.item_type = 0
    """

final class Reader {
    let db: Database
    let names: Names
    private var chatLabels: [Int64: String] = [:]

    init(db: Database, names: Names) {
        self.db = db
        self.names = names
    }

    func display(_ handle: String) -> String {
        names.name(for: handle) ?? handle
    }

    /// A group's own name, else its people ("Eva, Sam and 2 others").
    func groupLabel(_ chat: Int64, _ displayName: String) throws -> String {
        if !displayName.isEmpty { return displayName }
        if let l = chatLabels[chat] { return l }
        var people: [String] = []
        try db.rows(
            "SELECT h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id WHERE chj.chat_id = ? ORDER BY h.ROWID",
            [.int(chat)]
        ) { st in
            people.append(self.display(columnText(st, 0)))
            return true
        }
        let label: String
        switch people.count {
        case 0: label = "a group"
        case 1...3: label = people.joined(separator: ", ")
        default: label = people.prefix(2).joined(separator: ", ") + " and \(people.count - 2) others"
        }
        chatLabels[chat] = label
        return label
    }

    /// Messages, newest first. `keep` filters on the decoded text (search); rows it
    /// rejects still count toward the scan cap.
    func messages(
        where extra: String, _ binds: [Bind], limit: Int, keep: ((String) -> Bool)? = nil
    ) throws -> (messages: [[String: Any]], scanned: Int) {
        var out: [[String: Any]] = []
        var scanned = 0
        let sql = selectMessages + extra + " ORDER BY m.date DESC, m.ROWID DESC LIMIT ?"
        let rowCap = keep == nil ? limit : maxScan
        try db.rows(sql, binds + [.int(Int64(rowCap))]) { st in
            scanned += 1
            var text = columnText(st, 1)
            if text.isEmpty { text = decodeAttributedBody(columnBlob(st, 2)) ?? "" }
            text = text.replacingOccurrences(of: "\u{FFFC}", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
            if let keep, !keep(text) { return true }
            let fromMe = sqlite3_column_int(st, 4) != 0
            let handle = columnText(st, 8)
            let chat = sqlite3_column_int64(st, 9)
            let group = sqlite3_column_int(st, 12) == 43
            var m: [String: Any] = [
                "id": sqlite3_column_int64(st, 0),
                "date": isoLocal(appleDate(sqlite3_column_int64(st, 3))),
                "from_me": fromMe,
                "service": columnText(st, 6),
                "text": String(text.prefix(maxText)),
                "group": group,
            ]
            if text.count > maxText { m["truncated"] = true }
            if sqlite3_column_int(st, 7) != 0 { m["attachment"] = true }
            if !fromMe { m["read"] = sqlite3_column_int(st, 5) != 0 }
            if !handle.isEmpty {
                m["handle"] = handle
                if let n = names.name(for: handle) { m["name"] = n }
            }
            if group {
                m["chat"] = try self.groupLabel(chat, columnText(st, 11))
            } else if fromMe, handle.isEmpty {
                let other = columnText(st, 10)
                if !other.isEmpty { m["chat"] = self.display(other) }
            }
            out.append(m)
            return out.count < limit
        }
        return (out, scanned)
    }

    /// handle.ROWIDs for a name (via contacts), a phone number or an email address.
    func handles(for contact: String) throws -> (ids: [Int64], matched: [String]) {
        var wanted: Set<String> = []
        var matched: [String] = []
        let digits = contact.filter(\.isNumber).count
        if contact.contains("@") || digits >= 7 {
            wanted.insert(normalizeHandle(contact))
            matched.append(names.name(for: contact) ?? contact)
        } else {
            for p in names.matching(contact) {
                matched.append(p.name)
                for h in p.handles { wanted.insert(normalizeHandle(h)) }
            }
        }
        var ids: [Int64] = []
        try db.rows("SELECT ROWID, id FROM handle", []) { st in
            if wanted.contains(normalizeHandle(columnText(st, 1))) { ids.append(sqlite3_column_int64(st, 0)) }
            return ids.count < 200
        }
        return (ids, matched)
    }
}

// MARK: - Mail's index

let maxMailScan = 400_000

/// What of Mail's index this helper relies on, checked on every open (columns differ a
/// little between macOS versions). `missing` lists required columns that aren't there.
struct MailSchema {
    var missing: [String] = []
    var notes: [String] = []
    var hasDeleted = false
    var hasPrefix = false
    var hasLabels = false
}

/// "Name <address>", quoting a name that has commas or other address punctuation.
func displaySender(_ comment: String, _ address: String) -> String {
    let name = comment.trimmingCharacters(in: .whitespacesAndNewlines)
    if name.isEmpty || name.caseInsensitiveCompare(address) == .orderedSame { return address }
    if address.isEmpty { return name }
    let special = CharacterSet(charactersIn: ",;:<>@()[]\"\\")
    if name.unicodeScalars.contains(where: special.contains) {
        let escaped = name.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        return "\"\(escaped)\" <\(address)>"
    }
    return "\(name) <\(address)>"
}

/// An account's inbox: a mailbox URL whose path ends in /INBOX (any case).
func isInboxURL(_ url: String) -> Bool {
    var path = (url.removingPercentEncoding ?? url).lowercased()
    while path.hasSuffix("/") { path.removeLast() }
    return path.hasSuffix("/inbox")
}

final class MailIndex {
    let db: Database
    let schema: MailSchema

    init(path: String) throws {
        db = try Database(path: path, what: "Mail's index")
        schema = try MailIndex.check(db)
    }

    private static func columns(_ db: Database, _ table: String) throws -> Set<String> {
        var out: Set<String> = []
        try db.rows("SELECT name FROM pragma_table_info(?)", [.text(table)]) { st in
            out.insert(columnText(st, 0).lowercased())
            return true
        }
        return out
    }

    private static func check(_ db: Database) throws -> MailSchema {
        var s = MailSchema()
        let required: [(String, [String])] = [
            ("messages", ["sender", "subject", "date_received", "mailbox", "read"]),
            ("addresses", ["address", "comment"]),
            ("subjects", ["subject"]),
            ("mailboxes", ["url"]),
        ]
        var have: [String: Set<String>] = [:]
        for (table, cols) in required {
            let found = try columns(db, table)
            have[table] = found
            if found.isEmpty {
                s.missing.append("table \(table)")
                continue
            }
            s.missing += cols.filter { !found.contains($0) }.map { "\(table).\($0)" }
        }
        let messages = have["messages"] ?? []
        s.hasDeleted = messages.contains("deleted")
        s.hasPrefix = messages.contains("subject_prefix")
        let labels = try columns(db, "labels")
        s.hasLabels = labels.contains("message_id") && labels.contains("mailbox_id")
        if !s.hasDeleted { s.notes.append("no messages.deleted: deleted messages can't be left out") }
        if s.hasPrefix { s.notes.append("subjects include messages.subject_prefix") }
        if s.hasLabels { s.notes.append("inbox includes labels (Gmail)") }
        return s
    }

    /// Inbox mailboxes: ROWID to URL.
    func inboxMailboxes() throws -> [Int64: String] {
        var out: [Int64: String] = [:]
        try db.rows("SELECT ROWID, COALESCE(url, '') FROM mailboxes", []) { st in
            let url = columnText(st, 1)
            if isInboxURL(url) { out[sqlite3_column_int64(st, 0)] = url }
            return true
        }
        return out
    }

    /// The WHERE clause for "in an inbox" (its own mailbox, or a Gmail label), its binds.
    private func inInbox(_ inbox: [Int64: String]) -> (sql: String, binds: [Bind]) {
        let ids = inbox.keys.sorted().map { Bind.int($0) }
        let marks = Array(repeating: "?", count: ids.count).joined(separator: ",")
        var sql = "(m.mailbox IN (\(marks))"
        var binds = ids
        if schema.hasLabels {
            sql += " OR m.ROWID IN (SELECT message_id FROM labels WHERE mailbox_id IN (\(marks)))"
            binds += ids
        }
        sql += ")" + (schema.hasDeleted ? " AND COALESCE(m.deleted, 0) = 0" : "")
        return (sql, binds)
    }

    func inboxCount(_ inbox: [Int64: String]) throws -> Int {
        if inbox.isEmpty { return 0 }
        let w = inInbox(inbox)
        var n = 0
        try db.rows("SELECT COUNT(*) FROM messages m WHERE " + w.sql, w.binds) { st in
            n = Int(sqlite3_column_int64(st, 0))
            return false
        }
        return n
    }

    /// Inbox envelopes, newest first. With `matching`, only those whose subject, sender
    /// address or sender name contains it (ignoring case and accents); every row looked
    /// at counts toward `scanned`. Senders and subjects repeat, so each is tested once.
    func envelopes(days: Int, unreadOnly: Bool, limit: Int, matching: String?) throws
        -> (rows: [[String: Any]], scanned: Int)
    {
        let inbox = try inboxMailboxes()
        if inbox.isEmpty { return ([], 0) }
        let w = inInbox(inbox)
        let subject = schema.hasPrefix
            ? "COALESCE(m.subject_prefix, '') || COALESCE(s.subject, '')" : "COALESCE(s.subject, '')"
        let sql = """
            SELECT m.ROWID, COALESCE(m.date_received, 0), COALESCE(a.address, ''), COALESCE(a.comment, ''),
                   \(subject), COALESCE(m."read", 0), m.mailbox, COALESCE(m.sender, 0),
                   COALESCE(m.subject, 0), \(schema.hasPrefix ? "COALESCE(m.subject_prefix, '')" : "''")
            FROM messages m
            LEFT JOIN addresses a ON a.ROWID = m.sender
            LEFT JOIN subjects s ON s.ROWID = m.subject
            WHERE \(w.sql) AND m.date_received >= ?\(unreadOnly ? " AND COALESCE(m.\"read\", 0) = 0" : "")
            ORDER BY m.date_received DESC, m.ROWID DESC LIMIT ?
            """
        let since = Int64(Date().timeIntervalSince1970) - Int64(days) * 86400
        var out: [[String: Any]] = []
        var scanned = 0
        var labelled: [Int64] = []
        var senderHit: [Int64: Bool] = [:]
        var subjectHit: [Int64: Bool] = [:]
        let needle = Array((matching ?? "").lowercased().utf8)
        let asciiNeedle = needle.allSatisfy { $0 < 0x80 }
        /// Column `i` contains `matching`: bytewise for plain ASCII (most subjects and
        /// addresses), else Foundation's case- and accent-blind search.
        func has(_ st: OpaquePointer, _ i: Int32) -> Bool {
            guard let matching else { return true }
            guard let p = sqlite3_column_text(st, i) else { return false }
            let text = UnsafeBufferPointer(start: p, count: Int(sqlite3_column_bytes(st, i)))
            if asciiNeedle, text.allSatisfy({ $0 < 0x80 }) {
                if needle.isEmpty { return true }
                if text.count < needle.count { return false }
                let lower: (UInt8) -> UInt8 = { $0 >= 65 && $0 <= 90 ? $0 + 32 : $0 }
                outer: for start in 0...(text.count - needle.count) {
                    for k in 0..<needle.count where lower(text[start + k]) != needle[k] { continue outer }
                    return true
                }
                return false
            }
            return String(cString: p).range(of: matching, options: [.caseInsensitive, .diacriticInsensitive]) != nil
        }
        try db.rows(sql, w.binds + [.int(since), .int(Int64(matching == nil ? limit : maxMailScan))]) { st in
            scanned += 1
            if matching != nil {
                let sender = sqlite3_column_int64(st, 7)
                var hit = senderHit[sender] ?? {
                    let h = has(st, 2) || has(st, 3)
                    senderHit[sender] = h
                    return h
                }()
                if !hit {
                    if columnText(st, 9).isEmpty {
                        let subjectID = sqlite3_column_int64(st, 8)
                        hit = subjectHit[subjectID] ?? {
                            let h = has(st, 4)
                            subjectHit[subjectID] = h
                            return h
                        }()
                    } else {
                        hit = has(st, 4)  // with its Re: prefix
                    }
                }
                if !hit { return true }
            }
            let address = columnText(st, 2)
            let name = columnText(st, 3)
            let subj = columnText(st, 4)
            let id = sqlite3_column_int64(st, 0)
            var row: [String: Any] = [
                "id": id,
                "date": isoLocal(Date(timeIntervalSince1970: Double(sqlite3_column_int64(st, 1)))),
                "sender": displaySender(name, address),
                "subject": subj,
                "read": sqlite3_column_int64(st, 5) != 0,
            ]
            if let url = inbox[sqlite3_column_int64(st, 6)] { row["mailbox"] = url } else { labelled.append(id) }
            out.append(row)
            return out.count < limit
        }
        if !labelled.isEmpty {  // in an inbox by label: name that inbox
            var boxes: [Int64: String] = [:]
            let marks = Array(repeating: "?", count: labelled.count).joined(separator: ",")
            try db.rows(
                "SELECT message_id, mailbox_id FROM labels WHERE message_id IN (\(marks))", labelled.map { Bind.int($0) }
            ) { st in
                if let url = inbox[sqlite3_column_int64(st, 1)] { boxes[sqlite3_column_int64(st, 0)] = url }
                return true
            }
            for i in out.indices where out[i]["mailbox"] == nil {
                out[i]["mailbox"] = boxes[out[i]["id"] as? Int64 ?? 0] ?? ""
            }
        }
        return (out, scanned)
    }
}

// MARK: - request handling

func intArg(_ args: [String: Any], _ key: String, default def: Int, max hi: Int) throws -> Int {
    guard let raw = args[key] else { return def }
    // JSON true/false arrive as NSNumber too (and 0 and 1 cast to Bool): tell them apart by type.
    let isBool = CFGetTypeID(raw as CFTypeRef) == CFBooleanGetTypeID()
    guard let n = raw as? Int, !isBool, (raw as? NSNumber).map({ Double(n) == $0.doubleValue }) ?? false else {
        throw Failure(code: "bad_request", message: "\(key) must be a whole number")
    }
    guard n >= 1, n <= hi else { throw Failure(code: "bad_request", message: "\(key) must be 1-\(hi)") }
    return n
}

func boolArg(_ args: [String: Any], _ key: String) throws -> Bool {
    guard let raw = args[key] else { return false }
    guard CFGetTypeID(raw as CFTypeRef) == CFBooleanGetTypeID(), let b = raw as? Bool else {
        throw Failure(code: "bad_request", message: "\(key) must be true or false")
    }
    return b
}

func textArg(_ args: [String: Any], _ key: String, max hi: Int) throws -> String {
    guard let s = (args[key] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty else {
        throw Failure(code: "bad_request", message: "\(key) is required")
    }
    guard s.count <= hi else { throw Failure(code: "bad_request", message: "\(key) is too long (max \(hi))") }
    guard !s.unicodeScalars.contains(where: { $0.value < 32 }) else {
        throw Failure(code: "bad_request", message: "\(key) has control characters")
    }
    return s
}

let allowedArgs: [String: Set<String>] = [
    "status": [],
    "recent": ["limit"],
    "from": ["contact", "limit", "since_days"],
    "unread": ["limit", "since_days"],
    "chat": ["chat", "limit", "since_days"],
    "search": ["text", "limit", "since_days"],
    "mail_status": [],
    "mail_recent": ["limit", "unread_only", "since_days"],
    "mail_search": ["text", "limit", "since_days"],
]

/// Read access to one file, re-checked in a fresh process while it's missing (a grant
/// made in System Settings shows up there at once); at most every 2 seconds.
final class AccessWatch {
    let check: () -> Access
    let probe: () -> Access
    let socket: String
    private var access: Access = .denied
    private var accessAt = Date.distantPast

    init(socket: String, check: @escaping () -> Access, probe: @escaping () -> Access) {
        self.socket = socket
        self.check = check
        self.probe = probe
    }

    func current() -> Access {
        if access == .granted { return check() == .granted ? .granted : probeAndCache() }
        if check() == .granted {  // readable here already: no need to ask a fresh process
            access = .granted
            return access
        }
        if Date().timeIntervalSince(accessAt) < 2 { return access }
        return probeAndCache()
    }

    private func probeAndCache() -> Access {
        access = probe()
        accessAt = Date()
        if access == .granted, check() != .granted {
            // Granted, but not to this already-running process: restart (launchd's
            // KeepAlive starts it again) so the next request can read.
            warn("Full Disk Access was just granted; restarting to pick it up")
            unlink(socket)
            exit(0)
        }
        return access
    }
}

final class Server {
    let o: Options
    let token: String
    let names: Names
    private var bucket: Double
    private var bucketAt = Date()
    private let messagesAccess: AccessWatch
    private let mailAccess: AccessWatch

    init(_ o: Options, token: String) {
        self.o = o
        self.token = token
        names = Names(mode: o.contacts)
        bucket = Double(o.ratePerMinute)
        let db = o.db
        messagesAccess = AccessWatch(
            socket: o.socket, check: { checkAccess(db) }, probe: { probeAccess(db) })
        mailAccess = AccessWatch(
            socket: o.socket, check: { findMailIndex(o).access }, probe: { probeMailAccess(o) })
    }

    func currentAccess() -> Access { messagesAccess.current() }

    private func takeToken() -> Bool {
        let now = Date()
        let rate = Double(o.ratePerMinute)
        bucket = min(rate, bucket + now.timeIntervalSince(bucketAt) * rate / 60)
        bucketAt = now
        if bucket < 1 { return false }
        bucket -= 1
        return true
    }

    func handle(_ req: [String: Any]) -> (answer: [String: Any], op: String, note: String) {
        let op = req["op"] as? String ?? ""
        var note = ""
        do {
            guard let given = req["token"] as? String, constantTimeEqual(given, token) else {
                throw Failure(code: "bad_token", message: "wrong or missing token")
            }
            guard let allowed = allowedArgs[op] else { throw Failure(code: "bad_request", message: "unknown op") }
            guard req["args"] == nil || req["args"] is [String: Any] else {
                throw Failure(code: "bad_request", message: "args must be an object")
            }
            let args = req["args"] as? [String: Any] ?? [:]
            if let bad = args.keys.first(where: { !allowed.contains($0) }) {
                throw Failure(code: "bad_request", message: "unknown argument \(bad) for \(op)")
            }
            note = args.compactMap { k, v in v is Int ? "\(k)=\(v)" : nil }.sorted().joined(separator: ",")
            if op.hasPrefix("mail_") { return try mailOp(op, args, note) }
            if op == "status" {
                return (
                    [
                        "ok": true, "access": currentAccess().rawValue, "contacts": names.status(),
                        "db": o.db, "helper": Bundle.main.executablePath ?? "", "version": 1,
                    ], op, note
                )
            }
            guard takeToken() else { throw Failure(code: "rate_limited", message: "too many requests; wait a minute") }
            switch currentAccess() {
            case .granted: break
            case .missing: throw Failure(code: "missing_db", message: "no Messages database at \(o.db)")
            case .denied:
                throw Failure(code: "no_access", message: "this helper doesn't have Full Disk Access yet")
            }
            var answer = try run(op, args)
            answer["ok"] = true
            answer["timezone"] = TimeZone.current.identifier
            note += (note.isEmpty ? "" : ",") + "n=\((answer["messages"] as? [Any])?.count ?? 0)"
            return (answer, op, note)
        } catch let f as Failure {
            return (["ok": false, "code": f.code, "error": f.message], op, note + (note.isEmpty ? "" : ",") + f.code)
        } catch {
            return (["ok": false, "code": "error", "error": "\(error)"], op, note + ",error")
        }
    }

    /// mail_status (free, like status) and the two envelope lookups (rate-limited).
    private func mailOp(_ op: String, _ args: [String: Any], _ note: String) throws
        -> (answer: [String: Any], op: String, note: String)
    {
        var note = note
        if op != "mail_status" {
            guard takeToken() else { throw Failure(code: "rate_limited", message: "too many requests; wait a minute") }
        }
        let access = mailAccess.current()
        let found = findMailIndex(o)
        if op == "mail_status" {
            var answer: [String: Any] = [
                "ok": true, "access": access.rawValue, "path": found.path, "usable": false, "version": 1,
            ]
            if access == .granted, !found.path.isEmpty {
                do {
                    let index = try MailIndex(path: found.path)
                    answer["schema_ok"] = index.schema.missing.isEmpty
                    answer["missing"] = index.schema.missing
                    answer["notes"] = index.schema.notes
                    if index.schema.missing.isEmpty {
                        let inbox = try index.inboxMailboxes()
                        answer["inbox_mailboxes"] = inbox.count
                        answer["inbox_messages"] = try index.inboxCount(inbox)
                        answer["usable"] = !inbox.isEmpty
                    }
                } catch let f as Failure {
                    answer["schema_ok"] = false
                    answer["error"] = f.message
                }
            }
            return (answer, op, note)
        }
        switch access {
        case .granted: break
        case .missing: throw Failure(code: "missing_index", message: "no Mail index on this Mac")
        case .denied: throw Failure(code: "no_access", message: "this helper doesn't have Full Disk Access yet")
        }
        guard !found.path.isEmpty else { throw Failure(code: "missing_index", message: "no Mail index on this Mac") }
        let started = Date()
        let index = try MailIndex(path: found.path)
        guard index.schema.missing.isEmpty else {
            throw Failure(code: "schema", message: "Mail's index lacks " + index.schema.missing.joined(separator: ", "))
        }
        var answer: [String: Any]
        if op == "mail_recent" {
            let limit = try intArg(args, "limit", default: 10, max: maxLimit)
            let days = try intArg(args, "since_days", default: 30, max: maxDays)
            let unread = try boolArg(args, "unread_only")
            let r = try index.envelopes(days: days, unreadOnly: unread, limit: limit, matching: nil)
            answer = ["messages": r.rows, "since_days": days]
        } else {
            let text = try textArg(args, "text", max: 100)
            let limit = try intArg(args, "limit", default: 10, max: maxLimit)
            let days = try intArg(args, "since_days", default: 180, max: maxDays)
            let r = try index.envelopes(days: days, unreadOnly: false, limit: limit, matching: text)
            answer = ["messages": r.rows, "since_days": days, "scanned": r.scanned, "scan_capped": r.scanned >= maxMailScan]
        }
        answer["ok"] = true
        answer["timezone"] = TimeZone.current.identifier
        answer["query_ms"] = Int(Date().timeIntervalSince(started) * 1000)
        note += (note.isEmpty ? "" : ",") + "n=\((answer["messages"] as? [Any])?.count ?? 0)"
        return (answer, op, note)
    }

    private func run(_ op: String, _ args: [String: Any]) throws -> [String: Any] {
        let reader = Reader(db: try Database(path: o.db), names: names)
        switch op {
        case "recent":
            let limit = try intArg(args, "limit", default: 10, max: maxLimit)
            let r = try reader.messages(where: " AND m.date >= ?", [.int(appleNanos(daysAgo: maxDays))], limit: limit)
            return ["messages": r.messages]
        case "unread":
            let limit = try intArg(args, "limit", default: 20, max: maxLimit)
            let days = try intArg(args, "since_days", default: 30, max: maxDays)
            let r = try reader.messages(
                where: " AND m.is_from_me = 0 AND m.is_read = 0 AND m.date >= ?",
                [.int(appleNanos(daysAgo: days))], limit: limit)
            return ["messages": r.messages, "since_days": days]
        case "from":
            let contact = try textArg(args, "contact", max: 100)
            let limit = try intArg(args, "limit", default: 10, max: maxLimit)
            let days = try intArg(args, "since_days", default: 30, max: maxDays)
            let found = try reader.handles(for: contact)
            if found.ids.isEmpty {
                let hint = names.status() == "authorized" || names.status() == "file"
                    ? "" : " (contact names are unavailable; use a phone number or email address)"
                throw Failure(code: "not_found", message: "no conversations with \(contact)\(hint)")
            }
            let marks = Array(repeating: "?", count: found.ids.count).joined(separator: ",")
            let r = try reader.messages(
                where: " AND m.is_from_me = 0 AND m.handle_id IN (\(marks)) AND m.date >= ?",
                found.ids.map { Bind.int($0) } + [.int(appleNanos(daysAgo: days))], limit: limit)
            return ["messages": r.messages, "matched": found.matched, "since_days": days]
        case "chat":
            let chat = try textArg(args, "chat", max: 200)
            let limit = try intArg(args, "limit", default: 20, max: maxLimit)
            let days = try intArg(args, "since_days", default: maxDays, max: maxDays)
            let found = try findChat(reader, chat)
            let r = try reader.messages(
                where: " AND c.ROWID = ? AND m.date >= ?", [.int(found.id), .int(appleNanos(daysAgo: days))],
                limit: limit)
            return ["messages": r.messages, "chat": found.label, "since_days": days]
        case "search":
            let text = try textArg(args, "text", max: 100)
            let limit = try intArg(args, "limit", default: 10, max: maxLimit)
            let days = try intArg(args, "since_days", default: 90, max: maxDays)
            let r = try reader.messages(
                where: " AND m.date >= ?", [.int(appleNanos(daysAgo: days))], limit: limit,
                keep: { $0.range(of: text, options: [.caseInsensitive, .diacriticInsensitive]) != nil })
            return ["messages": r.messages, "scanned": r.scanned, "scan_capped": r.scanned >= maxScan, "since_days": days]
        default:
            throw Failure(code: "bad_request", message: "unknown op")
        }
    }

    /// The most recently active chat named `query` (its name, or a person in a one-to-one chat).
    private func findChat(_ reader: Reader, _ query: String) throws -> (id: Int64, label: String) {
        var best: (id: Int64, label: String, date: Int64)?
        let q = fold(query)
        let people = try reader.handles(for: query).ids
        try reader.db.rows(
            """
            SELECT c.ROWID, COALESCE(c.display_name, ''), c.chat_identifier, COALESCE(c.style, 0),
                   (SELECT MAX(message_date) FROM chat_message_join WHERE chat_id = c.ROWID),
                   (SELECT group_concat(handle_id) FROM chat_handle_join WHERE chat_id = c.ROWID)
            FROM chat c
            """, []
        ) { st in
            let id = sqlite3_column_int64(st, 0)
            let name = columnText(st, 1)
            let ident = columnText(st, 2)
            let members = columnText(st, 5).split(separator: ",").compactMap { Int64($0) }
            let hit = (!name.isEmpty && fold(name).contains(q)) || ident == query
                || (sqlite3_column_int(st, 3) != 43 && members.count == 1 && people.contains(members[0]))
            let date = sqlite3_column_int64(st, 4)
            if hit, best == nil || date > best!.date {
                let label = sqlite3_column_int(st, 3) == 43 ? name : reader.display(ident)
                best = (id, label, date)
            }
            return true
        }
        guard let b = best else { throw Failure(code: "not_found", message: "no conversation matches \(query)") }
        return (b.id, b.label.isEmpty ? (try reader.groupLabel(b.id, "")) : b.label)
    }
}

func constantTimeEqual(_ a: String, _ b: String) -> Bool {
    let x = Array(a.utf8), y = Array(b.utf8)
    guard x.count == y.count else { return false }
    var diff: UInt8 = 0
    for i in 0..<x.count { diff |= x[i] ^ y[i] }
    return diff == 0
}

// MARK: - access log

final class AccessLog {
    let path: String
    init(path: String) { self.path = path }

    func write(_ line: String) {
        let attrs = try? FileManager.default.attributesOfItem(atPath: path)
        if let size = attrs?[.size] as? Int, size > 1_000_000 {
            _ = try? FileManager.default.removeItem(atPath: path + ".1")
            _ = try? FileManager.default.moveItem(atPath: path, toPath: path + ".1")
        }
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0o600)
        guard fd >= 0 else { return }
        let data = Array((isoLocal(Date()) + " " + line + "\n").utf8)
        _ = data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        close(fd)
    }
}

// MARK: - socket server

func peer(_ fd: Int32) -> (uid: uid_t, pid: pid_t, exe: String) {
    var uid: uid_t = 0, gid: gid_t = 0
    if getpeereid(fd, &uid, &gid) != 0 { uid = uid_t.max }
    var pid: pid_t = 0
    var len = socklen_t(MemoryLayout<pid_t>.size)
    _ = getsockopt(fd, SOL_LOCAL, LOCAL_PEERPID, &pid, &len)
    var buf = [CChar](repeating: 0, count: 4096)
    let n = proc_pidpath(pid, &buf, UInt32(buf.count))
    let exe = n > 0 ? String(cString: buf) : "?"
    return (uid, pid, exe)
}

func readLine(_ fd: Int32, max: Int) -> Data? {
    var data = Data()
    var chunk = [UInt8](repeating: 0, count: 4096)
    while data.count <= max {
        let n = read(fd, &chunk, chunk.count)
        if n <= 0 { return data.isEmpty ? nil : data }
        if let nl = chunk[0..<n].firstIndex(of: 0x0A) {
            data.append(contentsOf: chunk[0..<nl])
            return data
        }
        data.append(contentsOf: chunk[0..<n])
    }
    return nil
}

func writeAll(_ fd: Int32, _ data: Data) {
    data.withUnsafeBytes { raw in
        var off = 0
        while off < raw.count {
            let n = Darwin.write(fd, raw.baseAddress! + off, raw.count - off)
            if n <= 0 { return }
            off += n
        }
    }
}

func randomToken() -> String {
    var bytes = [UInt8](repeating: 0, count: 32)
    guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
        die("no random bytes")
    }
    return bytes.map { String(format: "%02x", $0) }.joined()
}

/// Write the token where only this user can read it, replacing any old one atomically.
func writeToken(_ token: String, to path: String) {
    let tmp = path + ".new"
    unlink(tmp)
    let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL, 0o600)
    guard fd >= 0 else { die("can't write \(tmp): \(String(cString: strerror(errno)))") }
    writeAll(fd, Data(token.utf8))
    fsync(fd)
    close(fd)
    guard rename(tmp, path) == 0 else { die("can't write \(path): \(String(cString: strerror(errno)))") }
}

func listen(at path: String) -> Int32 {
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8)
    guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
        die("socket path too long (max \(MemoryLayout.size(ofValue: addr.sun_path) - 1) bytes): \(path)")
    }
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        raw.copyBytes(from: bytes)
        raw[bytes.count] = 0
    }
    let len = socklen_t(MemoryLayout<sockaddr_un>.size)
    // Someone already answering there? Then another copy is running: leave it be.
    let probe = socket(AF_UNIX, SOCK_STREAM, 0)
    let live = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(probe, $0, len) == 0 }
    }
    close(probe)
    if live { die("another scout-messages is already serving \(path)") }
    unlink(path)
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { die("socket: \(String(cString: strerror(errno)))") }
    let old = umask(0o077)  // the socket file is created 0600
    let ok = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, len) == 0 }
    }
    umask(old)
    guard ok else { die("bind \(path): \(String(cString: strerror(errno)))") }
    chmod(path, 0o600)
    guard Darwin.listen(fd, 16) == 0 else { die("listen: \(String(cString: strerror(errno)))") }
    return fd
}

func serve(_ o: Options) -> Never {
    for dir in [o.socket, o.token, o.log].map({ ($0 as NSString).deletingLastPathComponent }) {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    }
    let token = randomToken()
    let server = Server(o, token: token)
    let log = AccessLog(path: o.log)
    let fd = listen(at: o.socket)
    writeToken(token, to: o.token)

    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)
    signal(SIGPIPE, SIG_IGN)
    let socketPath = o.socket
    var sources: [DispatchSourceSignal] = []
    for sig in [SIGTERM, SIGINT] {
        let s = DispatchSource.makeSignalSource(signal: sig, queue: .global())
        s.setEventHandler {
            unlink(socketPath)
            exit(0)
        }
        s.resume()
        sources.append(s)
    }
    log.write("start pid=\(getpid()) db=\(o.db)")
    warn("serving \(o.socket)")

    var timeout = timeval(tv_sec: 3, tv_usec: 0)
    while true {
        let c = accept(fd, nil, nil)
        if c < 0 { continue }
        setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(c, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        let who = peer(c)
        let caller = "pid=\(who.pid) exe=\(who.exe)"
        if who.uid != getuid() {
            log.write("refused uid=\(who.uid) \(caller)")
            close(c)
            continue
        }
        guard let line = readLine(c, max: 16384),
              let req = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
        else {
            log.write("bad_request \(caller)")
            writeAll(c, jsonLine(["ok": false, "code": "bad_request", "error": "send one JSON object per line"]))
            close(c)
            continue
        }
        let (answer, op, note) = server.handle(req)
        // Logged before answering; a successful status check isn't worth a line.
        if !(op == "status" || op == "mail_status") || answer["ok"] as? Bool != true {
            log.write("op=\(op.isEmpty ? "?" : op) \(note) \(caller)")
        }
        writeAll(c, jsonLine(answer))
        close(c)
    }
}

// MARK: - main

let argv = CommandLine.arguments
guard argv.count >= 2 else { die("usage: scout-messages serve|probe [options]") }
let opts = parseOptions(argv.dropFirst(2))
switch argv[1] {
case "serve":
    serve(opts)
case "probe":
    print(checkAccess(opts.db).rawValue)
case "probe-mail":
    print(findMailIndex(opts).access.rawValue)
default:
    die("unknown command \(argv[1])")
}

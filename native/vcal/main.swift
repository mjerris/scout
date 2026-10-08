// vcal: read (and, when asked, add to) the Mac's calendars through EventKit.
// Every calendar synced to this Mac (Google, iCloud, Exchange via Internet
// Accounts) is visible; macOS handles the accounts, so no tokens live here.
//
//   vcal status                         authorization state
//   vcal request                        ask macOS for calendar access (prompts once)
//   vcal calendars                      list calendars
//   vcal events --from T --to T [--calendar ID]... [--query TEXT] [--limit N]
//   vcal create --title S --start T --end T [--calendar ID] [--location S] [--notes S] [--all-day]
//
// Times are ISO 8601; without an offset they are local time ("2026-10-08T15:00").
// Output is one JSON object on stdout; on failure {"error": "..."} and exit 1.

import EventKit
import Foundation

let store = EKEventStore()

func emit(_ obj: Any) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    exit(0)
}

func fail(_ message: String) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: ["error": message])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    exit(1)
}

func statusName(_ s: EKAuthorizationStatus) -> String {
    switch s {
    case .notDetermined: return "not_determined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .fullAccess: return "full_access"
    case .writeOnly: return "write_only"
    @unknown default: return "unknown"
    }
}

func requireAccess() {
    let s = EKEventStore.authorizationStatus(for: .event)
    if s == .fullAccess { return }
    if s == .notDetermined {
        fail("calendar access not granted yet: run `vcal request` once from the Mac and allow it")
    }
    fail("calendar access is \(statusName(s)): allow it in System Settings > Privacy & Security > Calendars")
}

let localFormats = ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm", "yyyy-MM-dd HH:mm", "yyyy-MM-dd"]

func parseDate(_ text: String, _ what: String) -> Date {
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime]
    if let d = iso.date(from: text) { return d }
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = iso.date(from: text) { return d }
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = TimeZone.current
    for fmt in localFormats {
        f.dateFormat = fmt
        if let d = f.date(from: text) { return d }
    }
    fail("\(what): not an ISO 8601 time: \(text)")
}

func isoLocal(_ d: Date) -> String {
    let f = ISO8601DateFormatter()
    f.timeZone = TimeZone.current
    f.formatOptions = [.withInternetDateTime]
    return f.string(from: d)
}

func dayString(_ d: Date) -> String {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = TimeZone.current
    f.dateFormat = "yyyy-MM-dd"
    return f.string(from: d)
}

func calendarInfo(_ c: EKCalendar) -> [String: Any] {
    [
        "id": c.calendarIdentifier,
        "title": c.title,
        "account": c.source?.title ?? "",
        "writable": c.allowsContentModifications,
    ]
}

/// Parse "--flag value" pairs (flags in `bools` take no value). Repeated flags accumulate.
func options(_ args: ArraySlice<String>, bools: Set<String> = []) -> [String: [String]] {
    var out: [String: [String]] = [:]
    var it = args.makeIterator()
    while let a = it.next() {
        guard a.hasPrefix("--") else { fail("unexpected argument: \(a)") }
        let key = String(a.dropFirst(2))
        if bools.contains(key) {
            out[key, default: []].append("true")
            continue
        }
        guard let v = it.next() else { fail("\(a) needs a value") }
        out[key, default: []].append(v)
    }
    return out
}

func calendars(matching ids: [String]) -> [EKCalendar]? {
    if ids.isEmpty { return nil }
    let all = store.calendars(for: .event)
    let found = all.filter { c in
        ids.contains { $0 == c.calendarIdentifier || $0.caseInsensitiveCompare(c.title) == .orderedSame }
    }
    if found.isEmpty { fail("no calendar matches \(ids.joined(separator: ", "))") }
    return found
}

let argv = CommandLine.arguments
guard argv.count >= 2 else { fail("usage: vcal status|request|calendars|events|create ...") }
let rest = argv.dropFirst(2)

switch argv[1] {
case "status":
    emit(["status": statusName(EKEventStore.authorizationStatus(for: .event))])

case "request":
    let done = DispatchSemaphore(value: 0)
    var granted = false
    var message = ""
    store.requestFullAccessToEvents { ok, err in
        granted = ok
        message = err?.localizedDescription ?? ""
        done.signal()
    }
    done.wait()
    emit(["granted": granted, "status": statusName(EKEventStore.authorizationStatus(for: .event)), "error": message])

case "calendars":
    requireAccess()
    let cals = store.calendars(for: .event).sorted { ($0.source?.title ?? "", $0.title) < ($1.source?.title ?? "", $1.title) }
    emit(["calendars": cals.map(calendarInfo)])

case "events":
    requireAccess()
    let o = options(rest)
    guard let from = o["from"]?.last, let to = o["to"]?.last else { fail("events needs --from and --to") }
    let start = parseDate(from, "from"), end = parseDate(to, "to")
    if end <= start { fail("--to must be after --from") }
    if end.timeIntervalSince(start) > 400 * 86400 { fail("range too long (max 400 days)") }
    let limit = Int(o["limit"]?.last ?? "200") ?? 200
    let query = o["query"]?.last?.lowercased()
    let pred = store.predicateForEvents(withStart: start, end: end, calendars: calendars(matching: o["calendar"] ?? []))
    var events = store.events(matching: pred).sorted { $0.startDate < $1.startDate }
    if let q = query, !q.isEmpty {
        events = events.filter {
            ($0.title ?? "").lowercased().contains(q) || ($0.location ?? "").lowercased().contains(q)
                || ($0.notes ?? "").lowercased().contains(q)
        }
    }
    let total = events.count
    let out: [[String: Any]] = events.prefix(max(limit, 0)).map { e in
        var d: [String: Any] = [
            "title": e.title ?? "",
            "calendar": e.calendar?.title ?? "",
            "all_day": e.isAllDay,
            "start": e.isAllDay ? dayString(e.startDate) : isoLocal(e.startDate),
            "end": e.isAllDay ? dayString(e.endDate) : isoLocal(e.endDate),
        ]
        if let l = e.location, !l.isEmpty { d["location"] = l }
        if let n = e.notes, !n.isEmpty { d["notes"] = String(n.prefix(300)) }
        if let a = e.attendees, !a.isEmpty { d["attendees"] = a.count }
        if e.hasRecurrenceRules { d["recurring"] = true }
        return d
    }
    emit(["events": out, "total": total, "timezone": TimeZone.current.identifier])

case "create":
    requireAccess()
    let o = options(rest, bools: ["all-day"])
    guard let title = o["title"]?.last, !title.isEmpty else { fail("create needs --title") }
    guard let s = o["start"]?.last, let e = o["end"]?.last else { fail("create needs --start and --end") }
    let start = parseDate(s, "start"), end = parseDate(e, "end")
    if end < start { fail("--end must not be before --start") }
    let cal: EKCalendar
    if let ids = o["calendar"], let found = calendars(matching: ids) {
        cal = found[0]
    } else if let def = store.defaultCalendarForNewEvents {
        cal = def
    } else {
        fail("no default calendar; pass --calendar")
    }
    if !cal.allowsContentModifications { fail("calendar \(cal.title) is read-only") }
    let ev = EKEvent(eventStore: store)
    ev.title = title
    ev.startDate = start
    ev.endDate = end
    ev.isAllDay = o["all-day"] != nil
    ev.calendar = cal
    if let l = o["location"]?.last { ev.location = l }
    if let n = o["notes"]?.last { ev.notes = n }
    do {
        try store.save(ev, span: .thisEvent, commit: true)
    } catch {
        fail("could not save: \(error.localizedDescription)")
    }
    emit(["created": true, "title": title, "calendar": cal.title, "start": isoLocal(start), "end": isoLocal(end)])

default:
    fail("unknown command \(argv[1])")
}

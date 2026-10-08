// vcal: read (and, when asked, add to) the Mac's calendars and reminders through
// EventKit. Every account synced to this Mac (Google, iCloud, Exchange via Internet
// Accounts) is visible; macOS handles the accounts, so no tokens live here.
//
//   vcal status [--reminders]           authorization state (calendars, or reminders)
//   vcal request [--reminders]          ask macOS for access (prompts once)
//   vcal calendars                      list calendars
//   vcal events --from T --to T [--calendar ID]... [--query TEXT] [--limit N]
//   vcal create --title S --start T --end T [--calendar ID] [--location S] [--notes S] [--all-day]
//   vcal reminder-lists                 list reminder lists
//   vcal reminders [--list NAME]... [--include-completed] [--due-before T] [--limit N]
//   vcal reminder-add --title S [--list NAME] [--due T] [--notes S]
//   vcal reminder-complete --id ID [--title S]   (--title must match: a guard against a stale id)
//
// A list NAME matches its title ignoring case, spaces, punctuation and a trailing
// "list" ("shopping list" finds "Shopping", "to-do" finds "To Do").
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

func requireAccess(_ entity: EKEntityType = .event) {
    let s = EKEventStore.authorizationStatus(for: entity)
    if s == .fullAccess { return }
    let what = entity == .event ? "calendar" : "reminders"
    let pane = entity == .event ? "Calendars" : "Reminders"
    if s == .notDetermined {
        fail("\(what) access not granted yet: run `vcal request\(entity == .event ? "" : " --reminders")` once from the Mac and allow it")
    }
    fail("\(what) access is \(statusName(s)): allow it in System Settings > Privacy & Security > \(pane)")
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

func norm(_ name: String) -> String {
    var n = name.lowercased().filter { $0.isLetter || $0.isNumber }
    if n.hasSuffix("list"), n.count > 4 { n.removeLast(4) }
    return n
}

func reminderLists(matching names: [String]) -> [EKCalendar]? {
    if names.isEmpty { return nil }
    let all = store.calendars(for: .reminder)
    let found = all.filter { c in
        names.contains { $0 == c.calendarIdentifier || norm($0) == norm(c.title) }
    }
    if found.isEmpty { fail("no reminder list matches \(names.joined(separator: ", "))") }
    return found
}

func fetch(_ pred: NSPredicate) -> [EKReminder] {
    let done = DispatchSemaphore(value: 0)
    var out: [EKReminder] = []
    store.fetchReminders(matching: pred) { r in
        out = r ?? []
        done.signal()
    }
    done.wait()
    return out
}

let timeUnits: Set<Calendar.Component> = [.hour, .minute]

/// A due date as "yyyy-MM-dd" (no time of day) or a local ISO time.
func dueString(_ c: DateComponents) -> String? {
    guard let d = Calendar.current.date(from: c) else { return nil }
    return c.hour == nil ? dayString(d) : isoLocal(d)
}

func reminderInfo(_ r: EKReminder) -> [String: Any] {
    var d: [String: Any] = [
        "id": r.calendarItemIdentifier,
        "title": r.title ?? "",
        "list": r.calendar?.title ?? "",
        "completed": r.isCompleted,
    ]
    if let c = r.dueDateComponents, let due = dueString(c) { d["due"] = due }
    if let n = r.notes, !n.isEmpty { d["notes"] = String(n.prefix(300)) }
    if r.priority > 0 { d["priority"] = r.priority }
    return d
}

func dueDate(_ r: EKReminder) -> Date? {
    r.dueDateComponents.flatMap { Calendar.current.date(from: $0) }
}

let argv = CommandLine.arguments
guard argv.count >= 2 else {
    fail("usage: vcal status|request|calendars|events|create|reminder-lists|reminders|reminder-add|reminder-complete ...")
}
let rest = argv.dropFirst(2)

switch argv[1] {
case "status":
    let entity: EKEntityType = options(rest, bools: ["reminders"])["reminders"] == nil ? .event : .reminder
    emit(["status": statusName(EKEventStore.authorizationStatus(for: entity))])

case "request":
    let entity: EKEntityType = options(rest, bools: ["reminders"])["reminders"] == nil ? .event : .reminder
    let done = DispatchSemaphore(value: 0)
    var granted = false
    var message = ""
    let answer: (Bool, Error?) -> Void = { ok, err in
        granted = ok
        message = err?.localizedDescription ?? ""
        done.signal()
    }
    if entity == .event {
        store.requestFullAccessToEvents(completion: answer)
    } else {
        store.requestFullAccessToReminders(completion: answer)
    }
    done.wait()
    emit(["granted": granted, "status": statusName(EKEventStore.authorizationStatus(for: entity)), "error": message])

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
        if e.availability == .free { d["free"] = true }  // shown as free: doesn't block time
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

case "reminder-lists":
    requireAccess(.reminder)
    let lists = store.calendars(for: .reminder).sorted { ($0.source?.title ?? "", $0.title) < ($1.source?.title ?? "", $1.title) }
    let def = store.defaultCalendarForNewReminders()?.calendarIdentifier
    emit(["lists": lists.map { c in calendarInfo(c).merging(["default": c.calendarIdentifier == def]) { a, _ in a } }])

case "reminders":
    requireAccess(.reminder)
    let o = options(rest, bools: ["include-completed"])
    let lists = reminderLists(matching: o["list"] ?? [])
    let before = o["due-before"]?.last.map { parseDate($0, "due-before") }
    let limit = Int(o["limit"]?.last ?? "200") ?? 200
    let pred = o["include-completed"] != nil
        ? store.predicateForReminders(in: lists)
        : store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: lists)
    var items = fetch(pred)
    if let b = before {
        items = items.filter { r in dueDate(r).map { $0 < b } ?? false }
    }
    // Due ones first (soonest first), then the rest in list order.
    items.sort { a, b in
        switch (dueDate(a), dueDate(b)) {
        case let (x?, y?): return x < y
        case (.some, nil): return true
        case (nil, .some): return false
        default: return (a.creationDate ?? .distantPast) < (b.creationDate ?? .distantPast)
        }
    }
    emit(["reminders": items.prefix(max(limit, 0)).map(reminderInfo), "total": items.count,
          "timezone": TimeZone.current.identifier])

case "reminder-add":
    requireAccess(.reminder)
    let o = options(rest)
    guard let title = o["title"]?.last, !title.isEmpty else { fail("reminder-add needs --title") }
    let list: EKCalendar
    if let names = o["list"], let found = reminderLists(matching: names) {
        list = found[0]
    } else if let def = store.defaultCalendarForNewReminders() {
        list = def
    } else {
        fail("no default reminder list; pass --list")
    }
    if !list.allowsContentModifications { fail("reminder list \(list.title) is read-only") }
    let r = EKReminder(eventStore: store)
    r.title = title
    r.calendar = list
    if let n = o["notes"]?.last { r.notes = n }
    if let due = o["due"]?.last {
        let date = parseDate(due, "due")
        let dateOnly = !due.contains("T") && !due.contains(" ")
        var units: Set<Calendar.Component> = [.year, .month, .day]
        if !dateOnly { units.formUnion(timeUnits) }
        r.dueDateComponents = Calendar.current.dateComponents(units, from: date)
        if !dateOnly { r.addAlarm(EKAlarm(absoluteDate: date)) }
    }
    do {
        try store.save(r, commit: true)
    } catch {
        fail("could not save: \(error.localizedDescription)")
    }
    emit(["created": true, "reminder": reminderInfo(r)])

case "reminder-complete":
    requireAccess(.reminder)
    let o = options(rest)
    guard let id = o["id"]?.last, !id.isEmpty else { fail("reminder-complete needs --id") }
    guard let r = store.calendarItem(withIdentifier: id) as? EKReminder else { fail("no reminder with id \(id)") }
    if let t = o["title"]?.last, t.lowercased() != (r.title ?? "").lowercased() {
        fail("reminder \(id) is \"\(r.title ?? "")\", not \"\(t)\"")
    }
    if !(r.calendar?.allowsContentModifications ?? false) { fail("reminder list \(r.calendar?.title ?? "") is read-only") }
    if !r.isCompleted {
        r.isCompleted = true
        do {
            try store.save(r, commit: true)
        } catch {
            fail("could not save: \(error.localizedDescription)")
        }
    }
    emit(["completed": true, "reminder": reminderInfo(r)])

default:
    fail("unknown command \(argv[1])")
}

// scout-contacts: read-only access to the Mac's Contacts (every account synced to this
// Mac: iCloud, Google, Exchange) through the Contacts framework, for Scout. It has its
// own Contacts permission (Privacy & Security > Contacts lists it as "scout-contacts"),
// asked for once with `request`; nothing here can change a contact.
//
//   scout-contacts status                         authorization state
//   scout-contacts request                        ask macOS for access (prompts once)
//   scout-contacts search --query Q [--group G] [--limit N]   people matching Q, best first
//   scout-contacts get --id ID                    one contact, every field below
//   scout-contacts groups                         contact groups, with member counts
//   scout-contacts resolve --handle H [--handle H]...   phone numbers / emails to names
//   scout-contacts cards --handle H [--handle H]...     the card each handle is on: its
//                       id, name, and every phone number and email on it that no other
//                       card has; "me": true for the user's own card
//
// --fixture FILE.json (before or after the command) reads people from a JSON file
// instead of Contacts and never touches the Contacts store (the tests use it):
//   {"contacts": [{"id", "given", "middle", "family", "nickname", "organization",
//                  "job_title", "department", "phones": [{"label", "value"}],
//                  "emails": [...], "addresses": [{"label", "street", "city", "state",
//                  "postal_code", "country"}], "birthday": {"year"?, "month", "day"},
//                  "groups": ["group id"]}],
//    "groups": [{"id", "name"}], "me": "the user's own card's id"?}
//
// Search is fuzzy-ish: each word of the query must match a word of the person's name,
// nickname or organization (exactly, as a prefix, as a common nickname: "Bob" finds
// Robert, or one letter off for longer words: "Katherine" finds Catherine). A query
// with "@" matches email addresses; one of digits matches phone numbers. With --group,
// only that group's members (an empty query lists them).
//
// A contact's notes are never read: that needs an entitlement Apple grants on request.
// Output is one JSON object on stdout; on failure {"error": "..."} and exit 1.

import Contacts
import Foundation

func emit(_ obj: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}

func fail(_ message: String) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: ["error": message])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(1)
}

/// "--flag value" pairs; repeated flags accumulate.
func options(_ args: [String]) -> [String: [String]] {
    var out: [String: [String]] = [:]
    var it = args.makeIterator()
    while let a = it.next() {
        guard a.hasPrefix("--") else { fail("unexpected argument: \(a)") }
        guard let v = it.next() else { fail("\(a) needs a value") }
        out[String(a.dropFirst(2)), default: []].append(v)
    }
    return out
}

func text(_ o: [String: [String]], _ key: String, max hi: Int, required: Bool = true) -> String {
    let s = (o[key]?.last ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    if s.isEmpty, required { fail("--\(key) is required") }
    if s.count > hi { fail("--\(key) is too long (max \(hi))") }
    if s.unicodeScalars.contains(where: { $0.value < 32 }) { fail("--\(key) has control characters") }
    return s
}

// MARK: - people

struct Labeled {
    let label: String
    let value: String
}

struct Address {
    let label: String
    let street: String, city: String, state: String, postalCode: String, country: String
}

struct Person {
    let id: String
    let given: String, middle: String, family: String, nickname: String
    let organization: String, jobTitle: String, department: String
    let phones: [Labeled]
    let emails: [Labeled]
    let addresses: [Address]
    let birthday: (year: Int?, month: Int, day: Int)?
    let groups: [String]  // group ids

    var name: String {
        let full = [given, middle, family].filter { !$0.isEmpty }.joined(separator: " ")
        if !full.isEmpty { return full }
        return nickname.isEmpty ? organization : nickname
    }
}

func fold(_ s: String) -> String {
    s.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: Locale(identifier: "en_US_POSIX"))
}

func words(_ s: String) -> [String] {
    fold(s).split(whereSeparator: { !($0.isLetter || $0.isNumber) }).map(String.init)
}

func digits(_ s: String) -> String { s.filter(\.isNumber) }

/// A phone number or email as one key: +1 (555) 010-2000 and 5550102000 match.
func handleKey(_ h: String) -> String {
    if h.contains("@") { return fold(h.trimmingCharacters(in: .whitespaces)) }
    let d = digits(h)
    return d.count >= 7 ? String(d.suffix(10)) : fold(h)
}

func cleanLabel(_ raw: String?) -> String {
    guard let raw, !raw.isEmpty else { return "" }
    return CNLabeledValue<NSString>.localizedString(forLabel: raw)
}

func personJSON(_ p: Person, groupNames: [String: String]) -> [String: Any] {
    var out: [String: Any] = [
        "id": p.id, "name": p.name, "given": p.given, "middle": p.middle, "family": p.family,
        "nickname": p.nickname, "organization": p.organization, "job_title": p.jobTitle,
        "department": p.department,
        "phones": p.phones.map { ["label": $0.label, "value": $0.value] },
        "emails": p.emails.map { ["label": $0.label, "value": $0.value] },
        "addresses": p.addresses.map {
            [
                "label": $0.label, "street": $0.street, "city": $0.city, "state": $0.state,
                "postal_code": $0.postalCode, "country": $0.country,
            ]
        },
        "groups": p.groups.compactMap { groupNames[$0] }.sorted(),
    ]
    if let b = p.birthday {
        var bd: [String: Any] = ["month": b.month, "day": b.day]
        if let y = b.year { bd["year"] = y }
        out["birthday"] = bd
    }
    return out
}

// MARK: - sources

protocol Source {
    func people() -> [Person]
    func groups() -> [(id: String, name: String)]
    func meID() -> String?  // the user's own card ("My Card"), when there is one
}

struct FixtureSource: Source {
    let obj: [String: Any]

    init(path: String) {
        guard let data = FileManager.default.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { fail("can't read the fixture file \(path)") }
        self.obj = obj
    }

    func people() -> [Person] {
        let rows = obj["contacts"] as? [[String: Any]] ?? []
        return rows.map { r in
            func s(_ k: String) -> String { r[k] as? String ?? "" }
            func labeled(_ k: String) -> [Labeled] {
                (r[k] as? [[String: Any]] ?? []).map {
                    Labeled(label: $0["label"] as? String ?? "", value: $0["value"] as? String ?? "")
                }
            }
            let addrs = (r["addresses"] as? [[String: Any]] ?? []).map { a in
                Address(
                    label: a["label"] as? String ?? "", street: a["street"] as? String ?? "",
                    city: a["city"] as? String ?? "", state: a["state"] as? String ?? "",
                    postalCode: a["postal_code"] as? String ?? "", country: a["country"] as? String ?? "")
            }
            var bday: (year: Int?, month: Int, day: Int)?
            if let b = r["birthday"] as? [String: Any], let m = b["month"] as? Int, let d = b["day"] as? Int {
                bday = (b["year"] as? Int, m, d)
            }
            return Person(
                id: s("id"), given: s("given"), middle: s("middle"), family: s("family"),
                nickname: s("nickname"), organization: s("organization"), jobTitle: s("job_title"),
                department: s("department"), phones: labeled("phones"), emails: labeled("emails"),
                addresses: addrs, birthday: bday, groups: r["groups"] as? [String] ?? [])
        }
    }

    func groups() -> [(id: String, name: String)] {
        (obj["groups"] as? [[String: Any]] ?? []).map { ($0["id"] as? String ?? "", $0["name"] as? String ?? "") }
    }

    func meID() -> String? { obj["me"] as? String }
}

final class StoreSource: Source {
    let store = CNContactStore()

    func meID() -> String? {
        (try? store.unifiedMeContactWithKeys(toFetch: [CNContactIdentifierKey as CNKeyDescriptor]))?.identifier
    }

    func groups() -> [(id: String, name: String)] {
        ((try? store.groups(matching: nil)) ?? []).map { ($0.identifier, $0.name) }
    }

    private func members() -> [String: [String]] {
        var out: [String: [String]] = [:]
        for g in groups() {
            let req = CNContactFetchRequest(keysToFetch: [CNContactIdentifierKey as CNKeyDescriptor])
            req.predicate = CNContact.predicateForContactsInGroup(withIdentifier: g.id)
            try? store.enumerateContacts(with: req) { c, _ in out[c.identifier, default: []].append(g.id) }
        }
        return out
    }

    func people() -> [Person] {
        let keys = [
            CNContactIdentifierKey, CNContactGivenNameKey, CNContactMiddleNameKey, CNContactFamilyNameKey,
            CNContactNicknameKey, CNContactOrganizationNameKey, CNContactJobTitleKey,
            CNContactDepartmentNameKey, CNContactPhoneNumbersKey, CNContactEmailAddressesKey,
            CNContactPostalAddressesKey, CNContactBirthdayKey,
        ] as [CNKeyDescriptor]
        let inGroups = members()
        var out: [Person] = []
        do {
            try store.enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) { c, _ in
                var bday: (year: Int?, month: Int, day: Int)?
                if let b = c.birthday, let m = b.month, let d = b.day {
                    bday = (b.year == NSDateComponentUndefined ? nil : b.year, m, d)
                }
                out.append(
                    Person(
                        id: c.identifier, given: c.givenName, middle: c.middleName, family: c.familyName,
                        nickname: c.nickname, organization: c.organizationName, jobTitle: c.jobTitle,
                        department: c.departmentName,
                        phones: c.phoneNumbers.map { Labeled(label: cleanLabel($0.label), value: $0.value.stringValue) },
                        emails: c.emailAddresses.map { Labeled(label: cleanLabel($0.label), value: $0.value as String) },
                        addresses: c.postalAddresses.map {
                            let a = $0.value
                            return Address(
                                label: cleanLabel($0.label), street: a.street, city: a.city, state: a.state,
                                postalCode: a.postalCode, country: a.country)
                        },
                        birthday: bday, groups: inGroups[c.identifier] ?? []))
            }
        } catch {
            fail("reading contacts failed: \(error.localizedDescription)")
        }
        return out
    }
}

func statusName(_ s: CNAuthorizationStatus) -> String {
    switch s {
    case .notDetermined: return "not_determined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .authorized: return "authorized"
    @unknown default: return "limited"  // .limited (macOS 15): the contacts the user picked
    }
}

func requireAccess() {
    let s = CNContactStore.authorizationStatus(for: .contacts)
    if s == .notDetermined {
        fail("contacts access not granted yet: run `scout-contacts request` once from the Mac and allow it")
    }
    if s == .denied || s == .restricted {
        fail("contacts access is \(statusName(s)): allow it in System Settings > Privacy & Security > Contacts")
    }
}

// MARK: - matching

/// Common English nicknames, each group one name.
let nicknameGroups: [[String]] = [
    ["robert", "rob", "robbie", "bob", "bobby", "bert"], ["william", "will", "bill", "billy", "liam", "willy"],
    ["michael", "mike", "mikey", "mick", "mickey"], ["elizabeth", "liz", "lizzie", "beth", "betsy", "betty", "eliza"],
    ["katherine", "catherine", "kathryn", "kate", "katie", "kathy", "cathy", "kat"],
    ["james", "jim", "jimmy", "jamie"], ["richard", "rick", "ricky", "rich", "dick"],
    ["thomas", "tom", "tommy"], ["joseph", "joe", "joey"], ["patricia", "pat", "patty", "trish", "tricia"],
    ["patrick", "pat", "paddy"], ["samuel", "sam", "sammy"], ["samantha", "sam", "sammie"],
    ["alexander", "alex", "xander"], ["alexandra", "alex", "lexi"], ["christopher", "chris", "topher"],
    ["christine", "christina", "chris", "tina", "chrissy"], ["daniel", "dan", "danny"],
    ["danielle", "dani"], ["david", "dave", "davey"], ["edward", "ed", "eddie", "ted", "ned"],
    ["jennifer", "jen", "jenny"], ["jonathan", "jon", "jonny"], ["john", "johnny", "jack"],
    ["margaret", "maggie", "meg", "peggy", "marge"], ["matthew", "matt"], ["nicholas", "nick", "nicky"],
    ["anthony", "tony"], ["andrew", "andy", "drew"], ["benjamin", "ben", "benny"],
    ["charles", "charlie", "chuck"], ["steven", "stephen", "steve"], ["susan", "sue", "suzy", "susie"],
    ["timothy", "tim", "timmy"], ["victoria", "vicky", "tori"], ["gregory", "greg"], ["jeffrey", "jeff"],
    ["kenneth", "ken", "kenny"], ["lawrence", "larry"], ["ronald", "ron", "ronnie"],
    ["donald", "don", "donnie"], ["deborah", "debra", "deb", "debbie"], ["rebecca", "becky", "becca"],
    ["abigail", "abby"], ["jacob", "jake"], ["nathan", "nate"], ["nathaniel", "nate", "nat"],
    ["zachary", "zach", "zack"], ["joshua", "josh"], ["anne", "ann", "annie"], ["frederick", "fred", "freddie"],
]

let nicknames: [String: Set<String>] = {
    var out: [String: Set<String>] = [:]
    for g in nicknameGroups { for n in g { out[n, default: []].formUnion(g) } }
    return out
}()

func oneEditApart(_ a: String, _ b: String) -> Bool {
    let x = Array(a), y = Array(b)
    if abs(x.count - y.count) > 1 { return false }
    var i = 0, j = 0, edits = 0
    while i < x.count, j < y.count {
        if x[i] == y[j] {
            i += 1
            j += 1
            continue
        }
        edits += 1
        if edits > 1 { return false }
        if x.count > y.count { i += 1 } else if y.count > x.count { j += 1 } else { i += 1; j += 1 }
    }
    return edits + (x.count - i) + (y.count - j) <= 1
}

/// How well one query word matches one name word: 0 exact, 1 prefix or nickname, 2 one
/// letter off; nil for no match.
func wordScore(_ q: String, _ w: String) -> Int? {
    if q == w { return 0 }
    if q.count >= 2, w.hasPrefix(q) { return 1 }
    if let group = nicknames[q], group.contains(w) { return 1 }
    if q.count >= 4, w.count >= 4, oneEditApart(q, w) { return 2 }
    return nil
}

/// Lower is better; nil = no match.
func score(_ p: Person, query: String) -> (Int, String)? {
    let q = query.trimmingCharacters(in: .whitespaces)
    if q.isEmpty { return (0, "group") }
    if q.contains("@") {
        let fq = fold(q)
        if p.emails.contains(where: { fold($0.value) == fq }) { return (0, "email") }
        if p.emails.contains(where: { fold($0.value).contains(fq) }) { return (1, "email") }
        return nil
    }
    let d = digits(q)
    if d.count >= 4, !q.contains(where: \.isLetter) {
        let key = String(d.suffix(10))
        if p.phones.contains(where: { String(digits($0.value).suffix(10)) == key }) { return (0, "phone") }
        if p.phones.contains(where: { digits($0.value).contains(d) }) { return (1, "phone") }
        return nil
    }
    let qs = words(q)
    if qs.isEmpty { return nil }
    let nameWords = words([p.given, p.middle, p.family].joined(separator: " "))
    let nick = words(p.nickname)
    let org = words(p.organization)
    var total = 0
    var how = "name"
    for w in qs {
        var best: (Int, String)?
        for (pool, kind, extra) in [(nameWords, "name", 0), (nick, "nickname", 0), (org, "organization", 1)] {
            for nw in pool {
                if let s = wordScore(w, nw) {
                    let v = s + extra
                    if best == nil || v < best!.0 { best = (v, kind) }
                }
            }
        }
        guard let b = best else { return nil }
        total += b.0
        if b.1 != "name" { how = b.1 }
        if b.0 == 2 { how = how == "name" ? "similar" : how }
    }
    // A whole-name hit ("Pat Lee") beats the same words matched piecemeal.
    if fold(p.name) == fold(q) { total -= 1 }
    return (total, how)
}

// MARK: - main

var argv = Array(CommandLine.arguments.dropFirst())
var fixture: String?
if let i = argv.firstIndex(of: "--fixture") {
    guard i + 1 < argv.count else { fail("--fixture needs a value") }
    fixture = (argv[i + 1] as NSString).expandingTildeInPath
    argv.removeSubrange(i...(i + 1))
}
guard let command = argv.first else {
    fail("usage: scout-contacts status|request|search|get|groups|resolve|cards ...")
}
let o = options(Array(argv.dropFirst()))

func source() -> Source {
    if let f = fixture { return FixtureSource(path: f) }
    requireAccess()
    return StoreSource()
}

switch command {
case "status":
    if fixture != nil { emit(["status": "fixture"]) }
    emit(["status": statusName(CNContactStore.authorizationStatus(for: .contacts))])

case "request":
    if fixture != nil { emit(["granted": true, "status": "fixture"]) }
    let done = DispatchSemaphore(value: 0)
    var granted = false
    var message = ""
    CNContactStore().requestAccess(for: .contacts) { ok, err in
        granted = ok
        message = err?.localizedDescription ?? ""
        done.signal()
    }
    done.wait()
    emit([
        "granted": granted, "status": statusName(CNContactStore.authorizationStatus(for: .contacts)),
        "error": message,
    ])

case "search":
    let query = text(o, "query", max: 100, required: false)
    let groupName = text(o, "group", max: 100, required: false)
    if query.isEmpty, groupName.isEmpty { fail("--query (or --group) is required") }
    var limit = 10
    if let l = o["limit"]?.last {
        guard let n = Int(l), n >= 1, n <= 25 else { fail("--limit must be 1-25") }
        limit = n
    }
    let src = source()
    let groups = src.groups()
    let names = Dictionary(groups.map { ($0.id, $0.name) }, uniquingKeysWith: { a, _ in a })
    var wanted: String?
    if !groupName.isEmpty {
        guard let g = groups.first(where: { fold($0.name) == fold(groupName) || $0.id == groupName }) else {
            fail("no contact group named \(groupName) (groups: \(groups.map(\.name).sorted().joined(separator: ", ")))")
        }
        wanted = g.id
    }
    var hits: [(Person, Int, String)] = []
    for p in src.people() {
        if let w = wanted, !p.groups.contains(w) { continue }
        if p.name.isEmpty { continue }
        if let (s, how) = score(p, query: query) { hits.append((p, s, how)) }
    }
    hits.sort { $0.1 != $1.1 ? $0.1 < $1.1 : fold($0.0.name) < fold($1.0.name) }
    emit([
        "contacts": hits.prefix(limit).map { h -> [String: Any] in
            var j = personJSON(h.0, groupNames: names)
            j["match"] = h.2
            j["score"] = h.1
            return j
        },
        "total": hits.count,
    ])

case "get":
    let id = text(o, "id", max: 200)
    let src = source()
    let names = Dictionary(src.groups().map { ($0.id, $0.name) }, uniquingKeysWith: { a, _ in a })
    guard let p = src.people().first(where: { $0.id == id }) else { fail("no contact with id \(id)") }
    emit(["contact": personJSON(p, groupNames: names)])

case "groups":
    let src = source()
    let people = src.people()
    emit([
        "groups": src.groups().map { g in
            ["id": g.id, "name": g.name, "count": people.filter { $0.groups.contains(g.id) }.count] as [String: Any]
        }
    ])

case "resolve":
    let handles = o["handle"] ?? []
    if handles.isEmpty { fail("--handle is required") }
    if handles.count > 200 { fail("at most 200 handles") }
    var byKey: [String: Person] = [:]
    for p in source().people() where !p.name.isEmpty {
        for h in p.phones.map(\.value) + p.emails.map(\.value) where byKey[handleKey(h)] == nil {
            byKey[handleKey(h)] = p
        }
    }
    var out: [String: Any] = [:]
    for h in handles {
        if let p = byKey[handleKey(h)] { out[h] = ["name": p.name, "id": p.id] }
    }
    emit(["names": out])

case "cards":
    let handles = o["handle"] ?? []
    if handles.isEmpty { fail("--handle is required") }
    if handles.count > 200 { fail("at most 200 handles") }
    let src = source()
    let all = src.people().filter { !$0.name.isEmpty }
    var byKey: [String: Person] = [:]
    var cardsWith: [String: Set<String>] = [:]  // handle key -> the cards it's on
    for p in all {
        for h in p.phones.map(\.value) + p.emails.map(\.value) {
            let k = handleKey(h)
            if byKey[k] == nil { byKey[k] = p }
            cardsWith[k, default: []].insert(p.id)
        }
    }
    let me = src.meID()
    func only(_ xs: [Labeled]) -> [String] {
        var seen: Set<String> = []
        return xs.map(\.value).filter {
            let k = handleKey($0)
            return cardsWith[k]?.count == 1 && seen.insert(k).inserted
        }
    }
    var out: [String: Any] = [:]
    for h in handles {
        // A handle on several cards (a shared office line) says no one in particular.
        guard let p = byKey[handleKey(h)], cardsWith[handleKey(h)]?.count == 1 else { continue }
        var card: [String: Any] = [
            "id": p.id, "name": p.name, "emails": only(p.emails), "phones": only(p.phones),
        ]
        if p.id == me { card["me"] = true }
        out[h] = card
    }
    emit(["cards": out])

default:
    fail("unknown command \(command); usage: scout-contacts status|request|search|get|groups|resolve|cards")
}

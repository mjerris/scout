# Contacts

Scout reads the Mac's Contacts (iCloud, Google, Exchange: every account synced to this
Mac in System Settings → Internet Accounts with Contacts on) to look people up and to
put names on numbers and addresses. It never adds, changes or deletes a contact.

## The helper and its permission

One small program has the Contacts permission: `bin/scout-contacts` in Scout's data
folder (`~/Library/Application Support/Scout`), built from `native/contacts` by
`scripts/build-contacts.sh`. The installer builds it when swiftc is there, and rebuilds
it only when its source changes.

- It's a command-line helper like the calendar's (`vcal`): Scout runs it for each
  lookup and it answers with JSON. Scout's Python, Claude and the terminal never get
  the permission.
- Signed ad hoc as `com.local.scout.contacts`, with the hardened runtime (nothing can
  be injected into it) and the Contacts entitlement. Its embedded Info.plist carries
  the text macOS shows in the prompt.
- It reads names, nickname, organization, job title and department, phone numbers and
  email addresses with their labels, postal addresses, the birthday, and which groups
  a contact is in. A contact's **notes are never read**: macOS only lets apps read
  them with an entitlement Apple grants on request.

Why its own helper, not a command of `vcal` or the Messages helper: macOS ties a
permission to the exact binary, so adding Contacts code to either would rebuild it and
ask for its calendar, reminders or Full Disk Access grants all over again. A separate
helper asks once for Contacts alone, and turning it off touches nothing else.

## Granting and revoking

The first contacts question ("what's Pat's number", or Claude calling
`contacts_search`) shows macOS's prompt on the Mac's screen, asking whether the
helper (named from its Info.plist, "scout contacts") may access your contacts, with
this text under it:

> scout looks people up in your contacts to answer questions like "what's Pat's
> number" and to show names instead of phone numbers and email addresses. It only
> reads; it never changes a contact.

Click **OK** (or **Allow**). It's then listed under **System Settings → Privacy &
Security → Contacts**; turn it off there to take it back (Scout
then says where to turn it on again). `tccutil reset AddressBook com.local.scout.contacts`
forgets the choice, so the prompt appears again next time. `scripts/scoutctl.sh
uninstall` leaves the switch alone; remove the entry in that list too. The prompt
waits for an answer: the first question is answered once you click.

Names in Messages and Mail listings never prompt by themselves: until the permission
is given they show numbers and addresses as before. (The Messages helper has a
separate Contacts permission of its own, `scout-messages`, for the names it shows;
either one is enough for names in texts.)

## What uses it

- **Tier 0** (plain code, no Claude): "what's Pat's number" (or "Pat's cell / work
  number", "the phone number for Pat"), "what's Pat's email", "when is Sam's birthday"
  (with how many days away, and the age when the year is known), "what's Pat's
  address". Phone numbers are spoken in groups of digits ("5 5 5, 0 1 0, 2 0 0 0").
  A name that fits several people gets "which one?"; a name nobody in Contacts has
  ("my wife's number") goes to Claude.
- **Tools** for the room, the phone and other Claude sessions (MCP): `contacts_search`
  (query: a name, nickname, organization, email address or phone number; or `group`
  for a group's members; best first, at most 25) and `contact_get` (one contact by id).
- **Messages**: handles the Messages helper shows bare get their names; "texts from
  Pat" and "my conversation with Pat" find Pat through Contacts when the Messages helper
  doesn't know the name.
- **Mail**: a sender shown as a bare address gets its name. `mail_draft` takes names
  ("Pat") and uses that person's email address; `mail_send` doesn't (the spoken yes is
  to an exact address): it answers with the address to send to. By phone, `mail_send`
  takes names: its read-back says the address, and the caller's yes is to that.

Searching is fuzzy-ish: each word must match a word of the name, nickname or
organization exactly, as a prefix ("Pat" finds Patricia), as a common nickname ("Bob"
finds Robert, "Liz" Elizabeth), or one letter off for longer words (what speech
recognition mishears). Accents don't matter. The best match comes first; a request for
"Pat" means Pat Lee when she's the only exact match.

## Privacy

Contacts are private data; `privacy.mode` decides what Claude sees (privacy.py):

| mode | search results | `contact_get` |
|---|---|---|
| strict | names, organization and title, phones, emails | the same |
| balanced (default) | + birthdays and groups | + postal addresses |
| open | everything | everything |

What's left out is said ("Addresses left out (balanced privacy mode)..."). Tier 0
answers see everything, since nothing leaves the Mac; a spoken birthday or address
counts as private, so in strict mode it isn't passed on to Claude with a follow-up.
Answers built from the contacts tools are journaled as private, like mail and texts.
`[contacts] enabled = false` in config.toml turns all of it off.

## Next

- Link a Slack user or meeting attendee to their contact by email (a `same_person`
  link in the store), so "what did Pat say in Slack" and "Pat's number" are one Pat.
- "Call Pat" and "text Pat" as actions: Scout can't place calls or send texts yet; once
  it can, they resolve Pat the same way mail drafts do.

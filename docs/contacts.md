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

## Text

"Text Pat I'm running late" (the room, the phone, or another Claude session) is two
tools, like Slack's (`messages_send.py`):

- `message_draft {to, text}`: `to` is a name from Contacts, a phone number or an email
  address. A name is looked up as above: several matches come back as the choices
  ("Pat Doe (Acme); Pat Lee"), for Claude to ask which; nobody, and it says so. Sends
  nothing. The draft names the handle and service it will use.
- `message_send {draft}`: asks every time (`asks=True`, `trust.NEVER`: "yes, always"
  never applies). The room reads it back, "Text Pat Doe: I'm running late?", and sends
  only on a spoken or clicked yes; another session's call is asked in the room; the
  phone does it in two steps (the read-back, then `confirm_action` after the caller's
  yes). A draft is sent at most once, and is gone after 30 minutes.

**Which handle.** Of the person's numbers and addresses, the one with the newest
one-to-one conversation in Messages (asked of the Messages helper's `chat`, one message;
it never starts the Full Disk Access setup), on the service that conversation used:
iMessage, or SMS (an RCS conversation too) through the iPhone's Text Message Forwarding.
With no history: their iPhone or mobile number, else another number (never a fax or
pager), else an email address, over iMessage. Numbers are sent as `+<digits>` (a
10-digit number is taken as North American).

**How it's sent.** AppleScript to Messages.app, run with `osascript` from the app, the
way Mail is driven: `send` the text to the `participant` (the handle) of the iMessage or
SMS `account`. The script is a constant; the handle, service and text are its arguments.
Why this route: it's the only supported way to send from a Mac (Messages has no other
API, and chat.db is read-only by design); the Shortcuts "Send Message" action needs a
shortcut made by hand first; an `sms:` URL only opens a compose window. Everything before the `send` is checked inside the script, so a missing
account or permission says "nothing was sent" and the draft can be sent after all; an
error from the `send` itself or a timeout says it may have gone out and the draft is
used up, so it's never sent twice.

**The permission.** The first send shows macOS's Automation prompt on the Mac's screen,
"“Scout” wants access to control “Messages”" (named after the app Scout runs as, as
Mail's prompt is). Click **OK**. Until then nothing is sent and Scout says where to allow
it: **System Settings → Privacy & Security → Automation**, Messages under Scout. Turning
it off there takes it back; `tccutil reset AppleEvents` forgets every Automation choice.

## Call

"Call Pat" (`calls.py`):

- `call_prepare {to}`: a name from Contacts or a number. Their iPhone or mobile number
  first, else another number; with no number but an email address, a FaceTime audio
  call. Several matches: the choices. Starts nothing.
- `call_start {call}` (room and other sessions; asks every time, never promoted):
  "Call Pat Doe at 5 5 5, 0 1 0, 2 0 0 0, mobile?" and after a yes opens
  `tel:+15550102000` on the Mac. macOS shows its call prompt (a second click, Call), and
  the call goes through the user's iPhone (Continuity: FaceTime → Settings → Calls from
  iPhone on the Mac, and "Calls on Other Devices" on the iPhone). No permission prompt:
  opening a URL needs none.
- **On the phone** Scout's SignalWire agent can't hand the call over to the iPhone, and
  starting it on the Mac would ring an empty room, so `call_start` isn't offered there.
  The phone agent offers `call_link {call}` instead: a reminder "Call Pat Doe" in the
  Scout list with the `tel:` link (its URL, and a line of its notes), which Reminders on
  the iPhone shows tappable. Scout's own list, so no spoken yes, like `send_link`; the
  same number isn't added twice within an hour.

## People

A contact is one person across Scout's sources (`people.py`, docs/links.md
`same_person`). Every six hours (and two minutes after the app starts) the app:

1. asks the store which email addresses it has seen: mail senders, meeting attendees
   (Granola's attendee list, kept with each note from now on);
2. reads Slack's members (`users.list`; a profile's email needs the `users:read.email`
   scope, else Slack is left out);
3. asks Contacts which contacts have those addresses (the helper's `resolve`, 200 at a
   time, at most 800 a pass; it never asks for the permission: without it nothing
   happens);
4. hands the store one person per contact: `person:<contact id>` (kind `person`,
   private like mail) with the addresses found, the Slack user whose profile email is
   theirs (by the name their messages are stored under; a name two Slack users share is
   left out), and the names they go by.

The store links each person `same_person` to their records: mail from one of their
addresses, meetings that list one of their addresses (or, for a meeting without
addresses, their full name), Slack messages and threads they wrote in, the newest 200 of
each. It's exact (a name alone never links mail, and a meeting by name only by a full
name) and idempotent: each person's links are set to what matches now, and the store's upkeep
relinks 20 people a minute, so new mail and messages join them. A person no longer
found is removed with their links (unless Slack couldn't be read that pass).

Where it's used:

- `local_search` with `person` ("what did Pat say about the launch": query "the launch",
  person "Pat"): only Pat's records, by words and meaning. When the store has nothing
  linked to Pat yet, it says so and searches for the words with Pat's name.
- `tasks_list` with `owner` ("what does Pat owe me"): tasks whose owner is any name Pat
  goes by (the contact's, a nickname, Slack's). A bare first name another contact
  shares ("Pat") counts only when the task was found in a record of Pat Doe's.
- The record page of a person lists the names, addresses and Slack user ("Also known
  as") and their records; a record of theirs shows "Same person".

On a copy of the live store (2026-10-15, counts only; Contacts and Slack aren't readable
there, so stand-ins were the 50 most frequent mail senders): 79 sender addresses among 172
mails, 230 Granola notes (none stored with attendee addresses yet: they come with each
note's next sync), 4,370 Slack records. 50 stand-ins made 143 `same_person` links to mail
in 0.47 s, the same 143 again on a second pass; an upkeep share of 20 took 0.18 s. Named
by their senders' full names, 41 stand-ins also linked 7 meetings (3 people) by name.

## Next

- Tier 0 for "text Pat ..." and "call Pat" (today Claude does both with the tools).
- Granola notes stored before attendee addresses were kept are linked by full name
  until they're synced again.

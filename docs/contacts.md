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

A person is one real person across Scout's sources (`people.py`; docs/links.md, People):
a record of kind `person` (private like mail), found only by stable ids: their Slack user
id, their email addresses, their contact, their GitHub login. Names are kept (every name
Slack has shown them by, so a renamed user stays one person) but a name alone never makes
a person.

**Linked as it's stored.** Every mail, meeting, Slack message and GitHub item is linked,
record to person, to the people in it (one link per person, the first rel that fits):

| rel | who |
|---|---|
| `with` | a DM's other party (by the DM's user id, also when only you wrote), a group DM's members, a meeting's attendees |
| `from` | a channel message's authors (by user id), a mail's sender, a GitHub item's author |
| `to` | a mail's To and Cc, a GitHub item's assignees |
| `mentions` | someone a Slack message @-mentions |

Someone seen for the first time gets a record: `person:slack:<user id>`,
`person:email:<address>`, `person:github:<login>`. A machine's address (no-reply,
notifications, billing and the like) or a bulk sender makes nobody, nor does an app's
message in a channel. A meeting attendee without an address, and a Slack message stored
before messages kept their authors' ids, link by name only to the one person who goes
by it (the names Slack showed, from state/slack.json, for old Slack messages). You are
never a person: `[tasks] me` (names and addresses), your Slack id, your GitHub login.

**Joined by the app's pass.** Every six hours (and two minutes after the app starts) the
app reads Slack's members (`users.list`; a profile's email needs the `users:read.email`
scope), asks Contacts which contacts have the addresses Scout has seen (the helper's
`cards`, 200 at a time, at most 800 a pass; it never asks for the permission) and hands
the store what it found. The store merges the records those identities join into one:
`person:<contact id>` when it's a contact, else the Slack user's record; every link moves
to it. A Slack user Scout has no record with makes no record. A contact no longer in
Contacts' answer loses the contact and keeps the rest.

**The whole card.** `cards` gives, for each address it finds, the card it's on: its id,
name, and every email address and phone number on it. All of them become the person's
keys at once (phones as E.164 where the country can be told: `+` numbers, and North
American 10 digits; `contacts_mac.dial_number`), so mail from an address of theirs Scout
has never seen links to them the moment it's stored, not after a later pass. An address or
number on two cards (a shared office line) is neither's, and an address that is on two
cards finds no one. Your own card ("My Card" in Contacts, which the helper marks `me`) is
never a person, nor is any card with one of your addresses. The pass's log line counts the
keys the cards gave: "people: N addresses, N people known, N links to them, N keys from
their contact cards". A helper built before `cards` still answers with names (`resolve`)
until the installer rebuilds it.

**GitHub logins** (`github_people.py`, run by scout-memory's GitHub sync every six hours,
the first time at its start; `people.slack_github` in the app's pass). A login joins the
rest of its person by an address or an account, never by a name:

- **Commit emails.** For each repo the sync tracks (state/github.json's repos), the commit
  list (`/repos/<o>/<r>/commits`, GET): five pages of 100 the first time, then only what came
  since the newest commit seen (its date; the commits at that date are kept, so none is
  counted twice). Each commit gives its author's and committer's (login, address). Skipped:
  noreply addresses (`*@users.noreply.github.com`, `noreply@github.com`), bots (`[bot]`,
  type Bot, web-flow, github-actions...), automation addresses (ci@, build@, deploy@... and
  the machine addresses above), a commit without a GitHub user, and you (a login that commits
  with one of your addresses is taken for yours too). An address is a login's only when no
  other login used it, and only when it's that login's most used address or on two commits
  or more, so one stray commit doesn't hand a login someone else's address. 60 requests a
  pass at most (commits and profiles together); a rate limit waits for GitHub's reset.
  The counts are kept in state/github_people.json.
- **The public profile** (`/users/<login>`, with its ETag, read again weekly) for the logins
  Scout has people for and the ones commits showed: its email, when it shows one, is a key
  like a commit's. Its name is not.
- **Slack's GitHub field.** When the workspace has a custom profile field labelled GitHub
  (`team.profile.get`, checked daily), each member's (`users.profile.get`, 100 a pass, read
  again weekly) gives their login, which joins their Slack user. Both calls need the
  `users.profile:read` scope; without it (or without such a field) that's recorded in
  state/people.json ("missing_scope", "none") and nothing more is asked that day. The
  SignalWire workspace's token (2026-10-16) has `users:read` and `users:read.email` but not
  `users.profile:read`, so whether it has a GitHub field can't be read yet: add the scope
  to the Slack app and it's used from the next pass.
- **Told.** "Brian's GitHub is briankwest" (also "Brian is briankwest on GitHub", "Brian's
  GitHub username is @briankwest", a github.com link, or the login spelled out with spaces
  as it's heard) is a fact like any other (memory.py; kept by tier 0 without "remember",
  and by the remember tools), and the store gives the login to the one person who goes by
  that name (`Index.apply_told`, whenever facts change and after each pass): the login's own
  record merges into theirs. With no one or several people by that name, it waits for a
  later pass. Forget it ("forget Brian's GitHub") and the login comes off them again; its
  GitHub items link to the login's own record.
- **Same name, asked.** A login whose GitHub name (the profile's, else the commit author
  name it used most) is another person's full name, with no key shared, is a suggestion,
  not a merge (`Index.people_suggestions`). When a request names them, Claude's context
  carries the question ("Is Brian West on GitHub the same as briankwest?"), asked once when
  it bears on the request. A yes is remembered as "Brian West's GitHub is briankwest"
  (merged, as told); a no as "Brian West's GitHub is not briankwest", which keeps it from
  being asked again (forget it to be asked again).

What these find goes to `put_people` as the other identities do (`complete` false: nobody
made, nobody's contact dropped). Any entry holding one of your identities is left out, so a
contact card with your address or your GitHub login never becomes a person. The log line is
counts only: "people: github N commits read, N logins seen, N mapped to an email, N merged,
N suggestions".

On a copy of the live store (2026-10-16, read-only GitHub calls, counts only): the first
pass read 4,048 commits from the 9 tracked repos in 60 requests and saw 50 logins, all 50
with an address that's theirs alone; two more passes read 60 profiles (18 show an email, 49
a full name). It merged 10 records (170 people to 160), and 20 people now hold a GitHub
login with an address or Slack user (14 of them with the same surname on both sides, none
different, 6 with no name to compare). 8 same-name suggestions; no address two logins used.

**The backfill.** scout-memory links what was stored before, at startup, in the
background: 500 records a step from where the last step stopped (kept in the store's
settings, so a restart resumes), in two passes (the second finds the people named only by
name in the first), progress in the log as counts, each line naming its pass ("people:
backfill pass 2: 500 of 5108 records", each pass counting from 0). It sets each record's links exactly,
so it's idempotent; its first step drops the `same_person` links the old matcher made.

Where it's used:

- `local_search` with `person` ("what did I talk about with Joao", "messages with
  Joao": person "Joao", query empty or the topic): only their records, by words and
  meaning, with their parts. The person is the contact the name means, else the one
  person Scout's records know by that name (several: it asks which); with nothing linked
  it searches for the words with their name.
- `tasks_list` with `owner` ("what does Pat owe me"): tasks whose owner is any name Pat
  goes by (the contact's, a nickname, Slack's). A bare first name another contact
  shares ("Pat") counts only when the task was found in a record linked to Pat Doe.
- The record page of a person lists the names, addresses, Slack user and GitHub login
  ("Also known as") and their records ("With them", "From them", "To them", "Mentioned
  by"); a record shows its people ("With", "From", "To", "Mentions").

On a copy of the live store (2026-10-16, counts only, offline: no Contacts or Slack
directory, so no merges): the backfill read 5,086 records in 16 s and made 174 people (86
Slack users, 63 addresses, 24 GitHub logins, the 1 contact) and 2,508 links (1,412 from,
1,070 with, 18 to, 8 mentions): 1,624 Slack records, 143 mails, 165 meetings, 44 GitHub
items. 2,900 Slack records are apps' messages and link nobody. Run again it made the same
links. "Joao Santos" is one person (his Slack user), linked to all 28 DMs with him.

## Next

- Tier 0 for "text Pat ..." and "call Pat" (today Claude does both with the tools).
- Granola notes stored before attendee addresses were kept are linked by full name
  until they're synced again.
- A record that names someone only by name, stored before anyone went by that name, is
  linked by the next backfill (it runs once; `restart_people_backfill` runs it again).
- Slack's GitHub field once the token has `users.profile:read`.
- The commit history before the first pass's 500 commits a repo isn't read.

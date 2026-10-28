# Messages (iMessage and SMS)

Scout reads your texts through one small helper with Full Disk Access, and sends one
only after your yes to the read-back.

Scout reads your texts through this helper; it sends one only when you ask and say
yes to the read-back (`message_draft` / `message_send`, through Messages.app: see
[contacts.md](contacts.md), Text). Messages keeps its history in
`~/Library/Messages/chat.db`, which macOS guards with Full Disk Access. That
permission goes to one small helper, `bin/scout-messages` (`native/messages`),
not to Scout's Python, Claude or a terminal:

- It runs as its own login item, `com.local.scout.messages`, so macOS checks the
  permission against the helper itself. The installer builds it (only when its
  source changed: a rebuilt helper is a new program to macOS and needs the
  switch again) and `scripts/scoutctl.sh uninstall` removes it.
- It opens the database read-only and answers a few fixed questions on a Unix
  socket in `state/` (owner-only, with a fresh random token in
  `state/messages_token` on every start): the newest messages, messages from one
  person, unread messages, one conversation, and a text search. At most 50
  messages and 365 days per answer, 60 lookups a minute, message text only (no
  attachments), no sending, no SQL from callers.
- It also answers five questions about Mail's index and files (`mail_status`,
  `mail_recent`, `mail_search`, `mail_sent_to`: whether you've sent mail to
  one address, a count and a date, and `mail_local`: which messages Mail has
  downloaded, by file name; see [mail.md](mail.md)), under the same token, limits
  and log. Rebuilding the helper for a source change needs the switch again for
  both.
- Every lookup is logged to `logs/messages-access.log` (what kind, how many,
  which program asked; never message text or search words).
- Names: it maps phone numbers and addresses to names from Contacts. macOS asks
  once, on the first real lookup; without it you see numbers.

The tools (`messages_recent`, `messages_from`, `messages_unread`,
`messages_search`) are read-only and run without asking, for the room and other
sessions alike. Text messages are written by other people and reach the agent
marked as untrusted, like email. `messages.enabled = false` in `config.toml`
turns them off.

**First use.** Full Disk Access has no "allow" prompt, so the first messages
question sets it up: Scout opens System Settings at Privacy & Security → Full
Disk Access and a Finder window with `scout-messages` selected, and says what to
do. Drag `scout-messages` into the list (or click +, press Cmd-Shift-G and paste
`~/Library/Application Support/Scout/bin/scout-messages`), turn it on, and
Scout says "Got it" a few seconds later. To take it back, turn it off or remove
it in the same list.

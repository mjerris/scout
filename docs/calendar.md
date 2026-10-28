# Calendar, reminders, the briefing and the pulse

Scout reads the Mac's own calendar and Reminders stores, so every account synced to the
Mac works, and speaks up on its own for what's about to start or come due.

## Calendar

The assistant reads the Mac's own calendar store, so any account synced to
this Mac works: add Google (or iCloud, Exchange) in System Settings → Internet
Accounts with Calendars on. macOS holds the account credentials; the app never
sees a token.

- `scripts/build-vcal.sh` builds the helper (`native/vcal`, EventKit).
- The first calendar question shows a macOS "allow calendar access" prompt on
  the Mac's screen. Allow it once; it's listed afterwards under System Settings →
  Privacy & Security → Calendars.
- Reading (`calendar_events`, `calendar_list`) runs without asking. Adding an
  event (`calendar_create_event`) is confirmed by voice every time ("Add
  Dentist, Friday October 9, 3:00 PM, to Home?"); "yes, always" doesn't apply.
  There is no edit or delete.
- Free time is worked out in plain code, no model: `calendar_free` (start, end,
  min_minutes) lists each day's free gaps and busy blocks inside working hours
  (`[calendar] work_start`/`work_end`, default 9:00 to 17:00). Overlapping
  events merge; all-day events and events shown as free don't block time.
- Answered instantly without Claude (tier 0): "am I free at 3 / tomorrow at 3 /
  Thursday at 3pm" (a time without am or pm from 1 to 6 means the afternoon),
  "when am I free tomorrow", "what's my first free hour tomorrow", "how busy is
  my day / tomorrow".

## Reminders

Reminders come from the Mac's Reminders store through the same helper, so
iCloud and other synced lists work.

- Reminders have their own macOS permission: the first reminders question
  shows an "allow access to Reminders" prompt on the Mac's screen (System
  Settings → Privacy & Security → Reminders). Rebuild the helper
  (`scripts/build-vcal.sh`) first so it carries the reminders usage text.
- Reading (`reminders_list`: open reminders, due ones first, by `list`, with
  `include_completed` or `due_before`; `reminder_lists`) runs without asking.
  Adding (`reminder_add`: title, list, due, notes) and completing
  (`reminder_complete`: id and title, which must match, so a stale id can't
  finish the wrong one) are confirmed by voice every time ("Add milk to
  Shopping?", "Mark milk done?"); "yes, always" doesn't apply. No delete.
- A list name matches ignoring case, spaces, punctuation and a trailing "list":
  "shopping list" finds Shopping, "to-do" finds To Do.
- Tier 0 only reads ("what's on my shopping list", "what are my reminders
  today", "what's due tomorrow"). "Add milk to my shopping list" goes to Claude
  on purpose: its `reminder_add` call is the one that asks you first.

## Briefing

"Brief me", "morning briefing" or "what's my day look like" gets one short
spoken summary, built in plain code: today's calendar (how many events, the
next one, the longest free stretch), reminders due today or overdue, and how
many unread emails there are (from the inbox's last 30 days) and who they're
mostly from. No subjects or message text are read. A source that isn't
available is named ("I couldn't check mail.") and the rest still plays.

Set `[briefing] at = "07:30"` to hear it every day at that time; it waits its
turn for the floor like a timer does.

## Pulse

Every 15 minutes Scout checks, in plain code, for an event about to start, a timed
reminder coming due, and (once each morning) today's overlapping events, and says each
once. Quiet hours (22:00-08:00) only log. `[pulse]` tunes or turns it off.

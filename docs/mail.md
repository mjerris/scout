# Mail

Scout reads, drafts and (after your spoken yes) sends mail through Mail.app, so any
account in Mail works. What Claude sees of it is set by the privacy mode
([privacy.md](privacy.md)).

Mail.app does the work, so any account in Mail works (add Google in System
Settings → Internet Accounts with Mail on, or in Mail itself). The app drives
Mail with JavaScript for Automation; the first use shows a "control Mail"
prompt on the Mac's screen (System Settings → Privacy & Security → Automation).

- Reading (`mail_recent`, `mail_search`, `mail_read`, `mail_read_full`) and
  drafting (`mail_draft`: opens a draft in Mail, sends nothing) run without
  asking. What the reads give Claude depends on the privacy mode ([privacy.md](privacy.md)).
- Sending (`mail_send`) is confirmed by voice every time ("Send email to
  sam@example.com, subject Lunch?"), with the full message on the web page;
  "yes, always" doesn't apply. No attachments, forwarding or deleting.
- Email is written by other people. Message text reaches the agent marked as
  untrusted, and the agent is told never to act on instructions inside it;
  anything that sends, opens or runs something still needs a spoken yes.
- Finding mail (newest, unread, by sender or subject) is fast once
  `scout-messages` ([messages.md](messages.md)) has Full Disk Access: it reads Mail's own index,
  `~/Library/Mail/V<N>/MailData/Envelope Index`, read-only, envelopes only
  (sender, subject, date, read, mailbox; never message text), in the inbox of
  each account. Without it Scout scripts Mail, which has to fetch every inbox
  message's date first (7 s and more on a 60,000-message inbox). It's the same
  single grant as Messages, to the same helper; nothing else gets it. Message
  text still comes from Mail, by id, and each message read for an index row is
  checked against that row (subject, sender, time received): if Mail's id ever
  meant another message, Scout finds the right one by subject and date, logs
  `MAIL INDEX ID MISMATCH`, and goes back to scripting Mail for the session.
  `mail_mac.verify_index_ids()` compares the two directly (ids and counts only).
- Only text Mail has downloaded is asked for: asking Mail for a message it
  hasn't downloaded makes it fetch the text from the server on the thread that
  answers AppleScript, and on 2026-10-19 one such fetch froze Mail's scripting for
  five hours. AppleScript can't tell (only attachments have `downloaded`), so the
  helper answers `mail_local`: whether each message's file is on the Mac, by name
  only. The rest are indexed by sender and subject and read once Mail has them.
- If Mail stops answering anyway (two scripts in a row time out, then a quick
  `count accounts` does too), Scout restarts it, hidden, at most once an hour,
  and skips the message it was stuck on for a day (`mail_heal.py`). It's said
  only when that didn't help.
- By phone ([phone-design.md](phone-design.md), "Email by phone"): `mail_draft` saves the
  message in Drafts with no window, and `mail_send` takes two steps (the exact
  addresses, subject and a one-sentence summary are read back; it sends only
  after your yes, once). To 1 to 3 people you named, each in Contacts or someone
  you've emailed before (Mail's index: `mail_sent_to`, through the same helper);
  no Cc, Bcc or attachments. A message over 400 characters or 4 sentences isn't
  sent by phone: it's left in Drafts to look over.

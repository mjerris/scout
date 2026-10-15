# Links: from what Scout said to the record it came from

Ask "give me a link to that" right after Scout mentions a task found in a meeting, and
the meeting's link lands in your Scout reminders list. Two halves make that work: the
store links records to each other and to where they open (the store part below), and
the conversation keeps the ids of the records behind everything Scout says (this part).

## Conversation

Code: `src/scout/refs.py`, with the tier 0 phrasings in `local_intents.py`.

**Refs on everything said.** Each thing Scout says carries the ids of the store records
it was about (`task:15`, `granola:<note>`, `mail:<n>`, `note:3`, `slack:<channel>:<ts>`;
a chunk such as `granola:<note>#3` counts as its record):

| what spoke | where its refs come from |
|---|---|
| tier 0 answers | the handler sets `Context.refs`: the tasks it named (matched by their spoken titles), the note it read; a memory answer (`memory.answer`) the facts it said |
| the pulse and the briefing | their sentences are `refs.Spoken` strings: "Pat owes you the vendor quote" carries `task:15` |
| tier 1 | `decision["refs"]`: the record a recall answer quoted, the tasks named, the emails spoken about |
| a Claude turn | its tools' results (`refs.from_tool`): task numbers in `tasks_list`, `task_why` and the task tools, mail ids in `mail_recent`/`mail_search`/`mail_read`, note ids, and the `(ref ...)` that every `local_search` result now ends with, as do the facts `remember` kept and `recall` found |
| phone requests | the same, kept with the phone as their channel |

The app keeps the last 30 things said with their refs (`refs.Log`). "That" means the
last thing said with refs, or the one before when the last had none (an "okay" or the
time in between), within ten minutes. "Say that again" repeats without replacing it,
so its refs stay. The journal entry for each turn gets a `refs:` line (ids only, kept
in every privacy mode), which the store turns into `about` links from the turn to
those records.

**What Claude sees.** The request context lists the last few things said that had
refs, each with its time, who said it (the pulse check-in, the briefing, or the answer
to a request) and its refs with their titles, so "that" resolves to an id. The voice
prompt tells Claude to pass a ref to `open_link` or `send_link` and never to read a
link aloud or guess one.

**Facts.** A fact ("Pat is my manager") is the record `memory:<id>`. Answers from facts
carry it: "who is my dentist" and "what do you know about Pat" in plain code, a local
recall that quoted a fact, and the remember and recall tools. Each fact in Claude's
context ends with its `(ref memory:<id>)`. "Where did that come from" after a fact
answer: "You told me on October 12, in the room." (the turn it was told in, below), or
"It's a fact you saved on October 12." when no turn is known (a line written into
facts.md by hand). The link is that turn's page, else the fact's own page; with neither,
"It has no link." Several facts in one answer ("You told me Pat is your manager; Pat
likes oat milk") go to Claude, as with tasks.

**Tools** (the room, the phone, and other Claude sessions through the MCP server):

- `open_link {ref?, about?}`: the record (its kind and title), its link, Scout's page
  for it, and where it came from. `about` is "that" or a few words matched against what
  was said recently.
- `send_link {ref?, about?}`: puts the link on the phone. A task whose reminder exists
  already has its link there (the task mirror sets it), so nothing is added; otherwise a
  reminder titled "Link: <title>" goes into the Scout list with the URL (also in its
  notes). Scout's own list, so no spoken yes. The same link isn't added twice within an
  hour.

**Tier 0** answers in plain code from the last spoken refs, when they name exactly one
record:

- "give me a link to that", "send me that link", "can I get a link to that", "what's
  the link": "It's from your meeting Vendor sync, on October 13. I put the link in
  your Scout reminders."
- "where did that come from", "what meeting was that from", "where's that from": "It's
  from your meeting Vendor sync, on October 13."

With no refs, or several ("Due today: the quote and the slides"), tier 0 passes the
request to Claude, which has the refs in its context and can ask which one. Nothing is
invented.

**Privacy.** Spoken answers use real titles, as tier 0 always does: the room and the
phone are yours. What Claude sees follows `privacy.mode`. In strict mode a record from
mail, a meeting or Slack, or a task found in one, is named only by its kind ("a task
from a meeting") in the context and in `open_link`'s result, and the text of something
said from private data is withheld. Its link is still given and still sent: a link is
your own pointer to your own record, opening it shows Claude nothing, and refusing it
would leave strict mode unable to answer "send me that link". In balanced mode titles
are shown, as `local_search` shows them; bodies never are.

## Store

Code: `src/scout/links.py` (the rules), `index.Index` (the table and the lookups), and
`recordpage.py` (the page on the web UI).

**Records and links.** A record is anything in the store, by its id: `task:15`,
`note:3`, `journal:<time>` (kind `turn`), `memory:<hash>` (a fact), `granola:<note>`
(and its transcript parts `granola:<note>#<n>`), `mailmeeting:<n>` (a meeting summary
that came by email), `mail:<n>`, `slack:<channel>:<ts>`. Links are rows of the `links`
table in `state/memory.db`, `(src, dst, rel)`, read either way:

| rel | from → to | made by |
|---|---|---|
| `found_in` | a task → the record it was found in | its file's `- source:` line, whenever the file is read |
| `merged_from` | a task → a source merged into it | each "From ... (<source>)" paragraph the farm adds when a repeat is folded in |
| `same_as` | an emailed meeting summary → the Granola note of the same meeting | the store, every minute (below) |
| `about` | a journal turn → the records the answer was about | the turn's `refs:` line |
| `based_on` | a turn or answer → a record it used | `POST /v1/links/add` |
| `told_in` | a fact → the journal turn it was told in | the turn's `refs:` line, or the upkeep's match (below) |

The task files stay the truth for their own sources: the `found_in` and `merged_from`
rows are re-derived from the file each time it's read, so an edit by hand moves them.
A task's source key is the farm's: `meeting:<granola note>`, `meeting:mail:<n>` (an
emailed summary), `mail:<n>`, `slack:<channel>@<newest ts>` (found by its conversation
and newest message). A deleted record takes its own links with it; a link to it from a
task stays (the task still names it) and is left out of `resolve`.

**Where a fact was told.** The turn that told Scout a fact lists it on its `refs:`
line. When that turn says remember (its remember tool ran, or the request says
"remember", "note that", "keep that" and isn't a question) and came within 15 minutes
after the fact was added, the store links the fact `told_in` to it. Asking about the
fact later lists it too, but doesn't move the link. Facts told before this, or on a
phone call (its journal entries have no refs), are matched by the upkeep: a turn on the
fact's day, not before its time, whose tools line shows remember keeping those words,
else whose request says remember and has most of the fact's words. Only a single match
links; two that fit equally get none. A fact edited by hand is a new fact with no turn.

**Where a record opens** (`open_url`): a Granola note (or any part of it) its
notes.granola.ai page; a Slack message its permalink; a mail `message://%3C<Message-ID>%3E`,
which opens it in Mail on the Mac, iPhone and iPad; an emailed meeting summary the
email's link; Scout's own records (tasks, notes, journal turns, facts) their page on
the web UI, `<web base>/r/<id>`. Mail without a known Message-ID has no link yet.

**`resolve(id)`**: `{id, kind, title, url, page, private, related: [{id, rel, dir,
kind, title, url}]}`. A task's `url` goes through to its source (the meeting it came
from) when that has a link, else its `- link:` line, else its own page; a fact's
`url` is the page of the turn it was told in, else its own page. `page` is a Scout
record's page (none for records from outside). `private` is true for mail,
meetings and Slack, a private journal turn, and a task found in one of them.
Over HTTP: `GET /v1/resolve?id=<id>` (404 when there's no such record),
`POST /v1/links/add {src, dst, rel}`, `/v1/links/list {id}`, `/v1/links/url {id}`;
`memory_client.Store` has `resolve`, `link`, `links` and `open_url`.

**Mail's Message-ID.** Mail.app's message id changes between Macs and mailboxes; the
Message-ID header doesn't, and it's what `message://` names. It's read with each
message's text as mail is indexed (`meta.message_id`), and mail indexed before that is
given its Message-ID by the mail pass, the newest 20 a pass, in one call to Mail with no
text read (`ids_s` in the pass's log line is what it took). A message Mail no longer has
gets an empty one, so it isn't asked for again.

**Task links.** A task added from a source with a link gets a `- link: <url>` line in
its file; a task whose source has no link yet (a mail waiting for its Message-ID) gets
it on a later pass of the store. Its reminder carries the same link (the task's
`resolve` url): the Reminders helper sets the reminder's URL and puts it on a line of
its notes, which Reminders on the iPhone shows with the link tappable. `reminder_url` in
the task file records that its reminder has it; reminders mirrored before links get
theirs on the farm's next pass (a helper built before `--url` is left alone for six
hours: rebuild it with `scripts/build-vcal.sh`).

**Same meeting, two records.** An emailed summary (Gemini, Zoom...) and a Granola note
are linked `same_as` when the note's meeting started within six hours before the email
(or 15 minutes after it), the titles share most of their words (Jaccard 0.6, ignoring
"meeting", "notes", "sync" and the like), and no other note fits as well. Two that fit
equally get no link: a missing link costs less than a wrong one. On the store as of
2026-10-15, none of the nine emailed summaries had a Granola note within that window
(they're of other meetings), so there are no `same_as` links yet.

**The record page** (`/r/<id>` on the web UI, token-gated like the rest: the phone's
browser keeps the cookie once the page is opened with `?token=`): the title, when and
who, a task's fields, the text (Scout's own records in full, a few lines of a meeting,
email or Slack message), an Open button to where it lives, and the linked records, each
to its own page. No scripts; everything from the store is escaped. Links never carry
the token. The base the links use (`links.web_base`): `[web] public_url` when set, else
the Tailscale Serve name that forwards to the web port (`scripts/tailscale.sh enable`,
`https://<mac>.<tailnet>.ts.net`), else the tailnet address, the home-network address
or localhost at the web port.

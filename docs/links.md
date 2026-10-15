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
| tier 0 answers | the handler sets `Context.refs`: the tasks it named (matched by their spoken titles), the note it read |
| the pulse and the briefing | their sentences are `refs.Spoken` strings: "Pat owes you the vendor quote" carries `task:15` |
| tier 1 | `decision["refs"]`: the record a recall answer quoted, the tasks named, the emails spoken about |
| a Claude turn | its tools' results (`refs.from_tool`): task numbers in `tasks_list`, `task_why` and the task tools, mail ids in `mail_recent`/`mail_search`/`mail_read`, note ids, and the `(ref ...)` that every `local_search` result now ends with |
| phone requests | the same, kept with the phone as their channel |

The app keeps the last 30 things said with their refs (`refs.Log`). "That" means the
last thing said with refs, or the one before when the last had none (an "okay" or the
time in between), within ten minutes. "Say that again" repeats without replacing it,
so its refs stay. The journal entry for each turn gets a `refs:` line (ids only, kept
in every privacy mode) when the store's `journal_add` takes `refs`.

**What Claude sees.** The request context lists the last few things said that had
refs, each with its time, who said it (the pulse check-in, the briefing, or the answer
to a request) and its refs with their titles, so "that" resolves to an id. The voice
prompt tells Claude to pass a ref to `open_link` or `send_link` and never to read a
link aloud or guess one.

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

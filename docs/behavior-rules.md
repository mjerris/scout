# Behavior rules

How the user wants Scout to behave, learned over time and given to every session:
"from now on, call me Mike", "always read ticket numbers digit by digit", "stop
saying great question". Code: `src/scout/behavior.py` (the store, detection, what's
injected, upkeep) and `src/scout/corrections.py` (the nightly pass).

## Not facts, not approvals

| | what it is | where | how Claude sees it |
|---|---|---|---|
| **Facts** (`memory.py`) | what's true in the user's world ("my dentist is Dr. Lee") | `memory/facts.md` | only the ones that share words with a request |
| **Approvals** (`rules.py`, `trust.py`) | which actions run without a spoken yes ("yes, always") | `state/voice_allow.json`, `state/trust.json` | never: they gate tool calls in code |
| **Behavior rules** (`behavior.py`) | how Scout should act every time | `memory/behavior.md` | all of them, in every session's prompt |

"Always allow adding reminders" and "stop asking me before adding events" stay
approvals (trust.py); "remember to call Sam" stays a reminder and "remember that ..."
a fact. Detection leaves those alone.

## The store: `memory/behavior.md`

One rule per line, in Scout's data folder beside `facts.md`; edit it by hand and the
change is picked up (sessions read it fresh, the store re-indexes it):

```
- Call me Mike · told · added 2026-10-15 · confirmed 2026-10-15
- Always read ticket numbers digit by digit · from corrections · added 2026-10-16 · confirmed 2026-10-16 · turns: journal:2026-10-15T09:01:00 journal:2026-10-15T09:31:00
- Read ticket, issue, PR and order numbers digit by digit · from a fix · added 2026-10-15 · confirmed 2026-10-15 · enforced: speech.speak_ids
```

After the rule, ` · ` fields: the origin (`told`, `set by Mike`, `from a fix`,
`from corrections`, or anything typed), the dates it was added and last confirmed
(said again, or kept at a review), `turns:` for a suggested rule (the journal turns it
came from), and `enforced: <code>` for a rule the code keeps itself. Enforced rules are
listed (rules_list, the record page, "what are my rules") but never injected: the
code already does it, and prompts stay small. A missing file is seeded with the fixes
made on 2026-10-15, all enforced: meetings "today" (now, what's left, the last hour),
ticket and order numbers digit by digit, phone numbers as people say them, Slack tasks
only from conversations involving the user, facts stated on calls remembered.

Records: each rule is `behavior:<hash of its words>` in the store (kind `behavior`,
indexed from the file by `Index.sync_files`), with a page on the web UI (`/r/behavior:...`).
A told rule links `told_in` to the journal turn that set it (the turn carries the rule's
ref and its request set a rule: `links.says_rule`); a suggested rule links `based_on`
to the turns it came from.

## Injected everywhere

The rules not enforced in code, at most `MAX_INJECTED` (40): past that the most
recently confirmed are used, the prompt says how many aren't, and adding one says
"say review my rules to merge some".

| session | where | when it's read |
|---|---|---|
| the room (`brain.Brain`) | the system prompt's append | each session start (it rotates: `session_idle_min`, `session_max_tokens`); a change mid-session comes once with the next request, "(Your rules for how to behave changed; these replace the earlier list.)", so the conversation isn't dropped |
| Claude for phone requests (`brain.PhoneBrain`) | the same | the same |
| the phone agent (`phone_service`) | a "Rules" prompt section, no heading | each call (per-call config); none when there are no rules |
| desk sessions (`mcp_server.instructions()`) | the MCP server's instructions | each desk session's start (one server per session) |

The session relay (`sessions.py`) gets none: it only forwards messages word for word
and never talks to the user, so a rule there could only change what it forwards.

Size: the block is a two-line lead (~120 characters, ~30 tokens) plus each rule (~30-50
characters, ~8-12 tokens). With today's seed (all enforced) it is empty: 0 tokens. At
the cap, about 500 tokens. In the room it's in the cached system prompt (cache reads
after the first turn).

## Detection

**Said to Scout (tier 0, no tokens).** `behavior.told` finds "from now on ...", "going
forward ...", "... from now on", "always ...", "never ...", "remember to always ...",
"make sure you always ...", "remember not to ...", "stop <saying, reading, telling,
apologizing, ...>", and "don't ..." when it says it's for good ("anymore", "again",
"ever", "every time"). It normalizes the rule ("stop apologizing so much" -> "Don't
apologize so much") and reads it back: "Rule: call you Mike. Keep it?"

- In the room: kept on a spoken yes; "no" keeps nothing.
- By phone: nobody in the room is asked. The phone agent has rule_add in its two steps
  (read back, then confirm_action after the caller's yes); a rule that reaches
  ask_scout is handed back to those two steps.
- Desk sessions: rule_add through the MCP server, asked in the room by voice.

Not caught (tests: `tests/test_behavior.py`): questions ("do you always do that?",
"why do you always say that"), one-offs ("don't forget the milk", even with "again";
"don't remind me about that", "don't do that again"), "never mind", "never heard of
it", "always the same thing", "I always forget my keys", "stop", "stop the timer",
"stop playing music", approvals ("always allow ...", "always ask me before ...", "stop
asking me about ..."), facts and reminders ("remember that ...", "remember to call Sam").

**Corrections, nightly (`corrections.py`).** At `[behavior] nightly_at` (default 03:30)
the app reads yesterday's journal. Plain code finds the corrections: said outright ("no,
...", "that's wrong", "not what I asked", "I already told you"), a request asked again
within 10 minutes in nearly the same words, and the turn after a barge-in over an
answer (barge-ins are kept in memory while the app runs). Only when there are some does
the local model (tier 1's LocalModel: Qwen on this Mac, never Claude) read up to 12 of
them, each with the answer it corrected, and draft at most 3 rules as JSON lines, each
with the item numbers it came from. Malformed lines, rules already kept and rules
suggested before (kept or declined) are dropped. They wait in
`state/behavior_candidates.json`; the morning briefing (`[briefing] at`) offers them one
at a time: "Twice yesterday you corrected how I read ticket numbers. Should I always
read ticket numbers digit by digit?" A yes keeps it (`from corrections`, with its
turns); a no is never offered again; no answer offers it again at the next briefing,
for 7 days. "Review my rules" offers them too.

**From fixes.** `scoutctl.sh rules add "<rule>" --enforced <code ref>` (origin "from a
fix"; without `--enforced`, "set by Mike"), `scoutctl.sh rules list`, `... drop <words>`
(shows what it would drop; `--yes` drops it), `... prompt` (what sessions get). Same as
`python -m scout.behavior ...`.

## Upkeep

`behavior.review` finds rules that say the same thing (the same topic words, both
"do" or both "don't"), rules more than 30 days old that never came up in the last 30
days of conversation, and the enforced ones. "Review my rules" (and the first briefing
of each month, when there are rules) asks about each, three questions at most:
"These two rules say the same thing: ...; and .... Merge them?", "... hasn't come up in
a month. Drop it?" Nothing is merged or dropped without a yes; a rule kept at a review
is confirmed today. The monthly review says nothing when there's nothing to ask.

Tools (shared: the room, the phone, desk sessions): `rules_list` (read-only),
`rule_add` (asks: the rule is read back), `rule_drop` (asks: what it would drop is read
back; rules enforced in code are never dropped this way). "What are my rules" is
answered in plain code.

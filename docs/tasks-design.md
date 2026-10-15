# Tasks: what you owe, what you're owed, and in what order (design)

Scout keeps a task list, fills it from your meetings and email, and tells you what to do
next. The ideas borrowed from mjerris/operator's design: code watches and AI judges
rarely; AI fills in structured fields and code does the ordering; extract once, keep the
result; a periodic deterministic check, with interruptions only for what's urgent.

## Decisions (2026-10-13)

| | |
|---|---|
| The record | Markdown files, `memory/tasks/0012-send-budget-to-pat.md`, one per task, indexed (kind `task`) like notes and the journal |
| On your phone and watch | Open tasks are mirrored to the Mac's Reminders: what you owe in the list **Scout**, what others owe you in **Waiting On** (each made when missing). Completing one in Reminders completes the task |
| Found tasks | Added directly (no suggested inbox). Each one names its source, so a wrong one is quick to drop |
| Who reads | Claude (Haiku, `[tasks] extract_model`) reads meetings (Granola notes, meeting summaries that arrive by email); the local model reads other email and Slack (they stay on the Mac) |
| What's tracked | What you owe; what others owe you (waiting on); due dates mentioned in passing |

## A task file

```markdown
# Send the revised travel budget to Pat
- status: open
- owner: me
- due: 2026-10-20
- importance: high
- urgency: normal
- effort: small
- source: meeting:not_1d3tmYTlCICgjy
- link: https://notes.granola.ai/d/1d3tmYTlCICgjy
- found: 2026-10-13 14:02
- reminder: x-apple-reminder://...
- from: meeting
- reminder_title: Send the revised travel budget to Pat

Pat asked for the revised numbers before Monday's review ("can you get me the travel
cut by Friday?").
```

`status` is open, done or dropped. `owner` is `me` or the person who owes it (waiting on);
a name or address from `[tasks] me` (or just your first name) is stored as `me`. `from`
(mail, meeting or slack) marks a task the farm found; `reminder_title` is the title its reminder
was made with (the helper checks it when completing). `link` is where its source opens
(the Granola note, the email in Mail, the Slack thread; docs/links.md), written when the
task is added or, for a source without one yet, on a later pass; `reminder_url` is the
link its reminder carries. An owner nobody can name is `unknown`. You can edit any line, add tasks by
hand, or delete a file; the index follows within a minute.

## Finding tasks (the farm, in the Scout app)

Every few minutes the farm looks at items new to the store since its checkpoint
(`state/tasks_farm.json`):

1. **Filter, no AI.** Mail from no-reply senders, newsletters, receipts and notifications
   is skipped, as is anything already read for tasks. Mail without an action cue ("can
   you", "please", "by Friday", "follow up", "I'll", "action item", "waiting on") is
   skipped too, and so is mail that is also stored as a meeting summary (it's read once,
   as the meeting). Meetings go through once all their parts have been stored for five
   minutes (a Granola note's transcript arrives in pieces). Slack (kind `slack`, synced
   by scout-memory, docs/slack.md): bot messages and messages without an action cue are
   skipped, and a conversation's new messages are read together, up to 3,000 characters
   per read.
2. **Extract.** Meetings go to Claude (Haiku; see "Which model reads meetings"), email and Slack to the local model, with one prompt; in
   strict privacy mode, meeting summaries that came by email go to the local model too.
   It returns JSON: one item per task with title, owner (`me` or a name), due date,
   importance, urgency, effort, and a short quote as evidence. Only commitments count,
   not ideas or topics. You are identified by `[tasks] me` (names and addresses), and
   in Granola transcripts by the lines marked "Me:". The owner must be "me" or a
   person's name, never a pronoun or a group ("them", "they", "the team"; Granola labels
   speakers it can't name "Them"): the prompt asks the model to work out who from the
   meeting, and plain code then takes the one other attendee (or the email's sender)
   when there's exactly one, else `unknown`, which is said as "Someone owes you ..."
   (`tasks.owes_you`), never "Them owes you". Dates in titles are written out from the
   day of the meeting or email, not the day it's read: the prompt says what day the text
   is from and asks for "the Oct 14 meeting", not "today's meeting", and plain code
   (`taskfarm.absolute_dates`) rewrites what's left ("tomorrow", "by Friday", "next
   week") the same way, leaving dates already given alone. A task whose quote isn't in the
   text (case and spacing aside) is dropped, and at most five come from one item. An
   answer that isn't valid JSON (cut short, say) isn't taken as "no tasks": the item is
   tried again next pass, three times in all, then left. Claude reads up to three
   meetings at a time; the local model's reads go one at a time (it has a single worker).
3. **Dedupe.** A found task that matches an open one closely, by meaning and the same
   owner (first names compared: "Pat" is "Pat Doe"), adds its source and quote to that
   task instead of making a new one, along with an earlier due date or a higher
   importance or urgency.
4. **Write.** The task goes into its file and Reminders. The farm doesn't write to the
   journal; the Scout app's log says what it found.

Backfill: on the first run, meetings and mail from the last `[tasks] backfill_days`
(14) are read. Older ones are left alone, since what they asked for is likely done.

## Which model reads meetings

Measured on 26 real meetings (scripts/bench_taskfarm.py, 2026-10-13). Each found task was
judged by Sonnet, with the readers unlabelled, as a real commitment with the right owner or
not; the judge was run twice and the ranges are the two runs:

| Reader | Precision | Real tasks found | Cost per meeting (list) | Time |
|---|---|---|---|---|
| Haiku 5.5, summary + transcript | 72–76% | 42–47% | $0.003 | 16 s |
| Sonnet 5.5, summary + transcript | 61–79% | 44–45% | $0.02–0.04 | 6 s |
| Sonnet, summary + action lines only | 61–74% | 41–43% | $0.02 | 6 s |
| Qwen3-4B (local), summary | 52–63% | 34–35% | 0 | 14 s |
| Qwen3-8B (local), summary | 50–70% | 21–25% | 0 | 18 s |
| Qwen3-14B (local), summary | 65% | 31% | 0 | 27 s, 9.3 GB |

Haiku is as good as Sonnet within the judge's noise (Sonnet also agrees with itself only
about 72% from run to run) at about a tenth of the price, so it's the default. The local
models find fewer real tasks; the 4B, already loaded, is the reader in strict privacy
mode, and the larger ones gain nothing worth their memory.
The CLI runs each read without saving the session (`--no-session-persistence`), so meeting
text isn't left in Claude Code's session files.

## Meeting summaries that arrive by email

Google Meet's Gemini notes ("Notes: …" or "… Notes by Gemini" from
gemini-notes@google.com, meetings-noreply@google.com or meet-recordings-noreply@google.com),
Zoom AI summaries ("Meeting summary"), Otter, Fireflies, Fathom, tl;dv, Read and Grain
(summary, notes, recap or transcript) and Teams recaps are recognized by the sender's
address (the one in `<...>`, never the display name; the service's domain or a subdomain
of it) and the subject. They're stored as kind `meeting` (source `email`), like
Granola notes, so they're searched as meetings and read by Claude for tasks. Only what's
in the email is kept: a transcript that only lives in a linked Google Doc stays out of
reach without Google Drive access. A one-time Mail search brings in the last
`backfill_days` of them, and tries again until every one found was read.

## Granola notes

A note Granola lists but won't give yet (not processed) is kept with when it was first
seen and asked for by id on later passes, so newer notes aren't downloaded again while
it waits; after seven days it's left. A note deleted in Granola is removed from the store.

## GitHub follow-ups

When the farm reads a meeting, email or Slack conversation that names GitHub items
(their URLs, `owner/repo#123`, or "#123" with one repo named; docs/links.md), a found task
about one of them gets a `- github: owner/repo#123` line, in addition to its source:
the item its title or quote names ("Review PR 117", "I'll review #117"), else the only
item the source names, when the title or quote talks about a PR, an issue, a review or
a merge ("can you look at the PR", "waiting on Luca to merge"). A task about two items,
or one that isn't about the item ("send the budget", in a message that also links a
PR), gets none. The task links `mentions` to the item's record, and its `- link:` (its
reminder's URL) is the item's page: that's what to act on. A repeat folded into an open
task brings its `github` line when the task had none.

The item's state then moves the task, in plain code, only when the task's own title is
about it (its number, most of its title's words, or "review / merge / fix ... the PR"):

- your task (owner me): when the item is closed or merged, the farm's next pass marks it
  done (and its reminder), with a line "GitHub: PR 117 in api was merged (2026-10-14),
  so this is done.";
- a waiting-on task: the pulse says it once, "PR 117 in api, Fix the jitter buffer, was
  merged on October 14. You were waiting on Luca for it." (`- github_said: merged`
  keeps it from being said again, across restarts), and it isn't offered as an overdue
  follow-up after that. You close it ("mark task 12 done") when you agree.

Asked out loud (tier 0, from the records): "what's the status of PR 117", "is issue 42
closed", "has PR 117 been merged": "PR 117 in api, Fix the jitter buffer, was merged on
October 14." (two repos with a 117: both); "what GitHub issues am I waiting on", "what
PRs am I waiting on": the open items your waiting-on tasks are about, then your own open
PRs. An item Scout has no record of goes to Claude. Claude has `github_items`.

## Order (plain code)

`score` = importance (high 3, normal 2, low 1) × 10
        + urgency from the due date: overdue 30, today 25, tomorrow 18, this week 10, later 3
        + urgency field (urgent 8)
        + small effort, when nothing is due soon: 3 (quick wins)
        + someone you've told Scout about (facts: "Pat is my manager"), named in the title
          or as the owner (whole words): 5
        − age without a due date, beyond two weeks: 1 per week (stale things sink)
        − waiting on someone else: 15

What you owe comes first; waiting-on items sink by 15, so they surface when their due
date is near or past (follow up). A pinned priority from you ("make the budget top
priority") overrides the score. `tasks_list` gives each task with one reason.

## Tools (room, phone and other sessions)

`tasks_list` (who: mine, waiting or all; query; limit) · `task_add` · `task_done` (drop) ·
`task_update` (title, due, importance, urgency, effort, owner, pin, context) · `task_why`
(the source and evidence). None of them asks first: they change only Scout's own list
(and its reminders).

What a found task holds came from someone's email, meeting or Slack message, so Claude sees it the way
the privacy mode says (privacy.py): in open mode, everything, with the quotes marked as
data, not instructions; in balanced mode, titles but not the quoted context; in strict
mode, "a task from an email", "a task from a meeting" or "a task from a Slack message"
with its number and due date.
The same goes for tasks in `local_search`. Tasks you add are shown as they are.

## Reminders

Only the Mac's Reminders is mirrored to, never Scout's own lists (`[lists] backend =
"scout"`, or Reminders unavailable: no mirroring). A task that couldn't be mirrored is
tried again on each farm pass. Completions are read by reminder id (the helper's
`reminder-get`), so they sync however long the lists are. A renamed task or a new due
date changes its reminder (`reminder-update`); a task moved between yours and waiting on
finishes its old reminder and gets one in the other list. Each reminder carries the
task's link (its source's, else its page on Scout's web UI) as its URL and on a line of
its notes, so it opens from the phone; reminders made before links get theirs on the
next farm pass. Found tasks whose owner was a pronoun are repaired on each farm pass
(their owner field only, by the rule above).

## Asking out loud (tier 0, no Claude)

Plain code answers these from the list, in a sentence or three:

| Asked | Said |
|---|---|
| "What should I work on?", "what's next?", "what should I do next?", "what are my top priorities?" | The top three in order, one reason each: "Start with send Pat the budget, due today. Then review the deck, overdue since October 9. After that, follow up with Pat Doe on the vendor quote, overdue since October 9." |
| "What's on my list?", "what are my tasks?", "what's on my plate?" | "You have four tasks, one overdue: A, B, C, and one more. You're waiting on three things from others." |
| "How many tasks do I have?" | Counts only: open, overdue, due today, waiting on others |
| "What am I waiting on?", "who owes me anything?" | Grouped by person, overdue first: "Pat Doe owes you two: the vendor quote, overdue since October 9, and the slides, due Friday. Sam owes you the contract." At most three people, three items each |
| "What does Pat owe me?", "what is Pat working on for me?", "what am I waiting on from Pat?" | The same for one person ("Pat" is "Pat Doe"). Someone with nothing on the list goes to Claude, so "what does the bank owe me" isn't answered with "nothing" |

The patterns match whole requests only: "what should I make for dinner", a bare "what
should I do", "what's next on my calendar" and "what's on my grocery list" (a Reminders
list) are left to their own answers or to Claude. These answers are spoken here and use
the real titles, even of found tasks; an answer that says a found task's title is marked
private (`Context.private`), so in strict privacy mode it's withheld from Claude like
other private local answers.

## The briefing and the pulse

The morning briefing says your top three tasks and how many more are overdue. Waiting-on
items past their date are offered as follow-ups ("Sam owes you the vendor quote, due
Friday, October 9; maybe follow up").

The pulse (pulse.py), at its first check of the day (the first after quiet hours end),
says what you owe that's due today ("Due today: send Pat the budget.") and up to three
waiting-on items that are overdue ("Pat owes you the vendor quote, overdue since October
9; maybe follow up."). Each is said once that day; nothing is said in quiet hours, and
the follow-ups come again the next day while they're overdue.

## Not now

Effort estimates feeding a calendar plan, and dependencies between tasks.

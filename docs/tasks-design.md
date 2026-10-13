# Tasks: what you owe, what you're owed, and in what order (design)

Scout keeps a task list, fills it from your meetings and email, and tells you what to do
next. The ideas borrowed from mjerris/operator's design: code watches and AI judges
rarely; AI fills in structured fields and code does the ordering; extract once, keep the
result; a periodic deterministic check, with interruptions only for what's urgent.

## Decisions (2026-10-13)

| | |
|---|---|
| The record | Markdown files, `memory/tasks/0012-send-budget-to-pat.md`, one per task, indexed (kind `task`) like notes and the journal |
| On your phone and watch | Open tasks are mirrored to Reminders: what you owe in the list **Scout**, what others owe you in **Waiting On**. Completing one in Reminders completes the task |
| Found tasks | Added directly (no suggested inbox). Each one names its source, so a wrong one is quick to drop |
| Who reads | Claude reads meetings (Granola notes, meeting summaries that arrive by email); the local model reads other email (it stays on the Mac) |
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
- source: granola:not_1d3tmYTlCICgjy
- found: 2026-10-13 14:02
- reminder: x-apple-reminder://...

Pat asked for the revised numbers before Monday's review ("can you get me the travel
cut by Friday?").
```

`status` is open, done or dropped. `owner` is `me` or the person who owes it (waiting on).
You can edit any line, add tasks by hand, or delete a file; the index follows within a
minute.

## Finding tasks (the farm, in the Scout app)

Every few minutes the farm looks at items new to the store since its checkpoint
(`state/tasks_farm.json`):

1. **Filter, no AI.** Mail from no-reply senders, newsletters, receipts and notifications
   is skipped, as is anything already read for tasks. Mail without an action cue ("can
   you", "please", "by Friday", "follow up", "I'll", "action item", a question to you)
   is skipped too. Meetings always go through.
2. **Extract.** Meetings go to Claude, email to the local model, with one prompt. It
   returns JSON: one item per task with title, owner (`me` or a name), due date,
   importance, urgency, effort, and a short quote as evidence. Only commitments count,
   not ideas or topics. You are identified by `[tasks] me` (names and addresses), and
   in Granola transcripts by the lines it marks as yours.
3. **Dedupe.** A found task that matches an open one closely, by meaning and the same
   owner, adds its source and context to that task instead of making a new one.
4. **Write.** The task goes into its file and Reminders, and into the journal ("found 2
   tasks in Budget review").

Backfill: on the first run, meetings and mail from the last `[tasks] backfill_days`
(14) are read. Older ones are left alone, since what they asked for is likely done.

## Meeting summaries that arrive by email

Google Meet's Gemini notes ("Notes: …" from gemini-notes@google.com, or from
meetings-noreply@google.com), Zoom AI summaries, Otter, Fireflies, Fathom and tl;dv are
recognized by sender and subject. They're stored as kind `meeting` (source `email`), like
Granola notes, so they're searched as meetings and read by Claude for tasks. Only what's
in the email is kept: a transcript that only lives in a linked Google Doc stays out of
reach without Google Drive access. A one-time Mail search brings in the last
`backfill_days` of them.

## Order (plain code)

`score` = importance (high 3, normal 2, low 1) × 10
        + urgency from the due date: overdue 30, today 25, tomorrow 18, this week 10, later 3
        + urgency field (urgent 8)
        + small effort, when nothing is due soon: 3 (quick wins)
        + from someone you've told Scout about (facts: "Pat is my manager"): 5
        − age without a due date, beyond two weeks: 1 per week (stale things sink)

What you owe comes first; waiting-on items surface when their due date passes (follow
up). A pinned priority from you ("make the budget top priority") overrides the score.
"What should I work on?" returns the top three with one reason each.

## Tools (room, phone and other sessions)

`tasks_list` (filter: mine, waiting, due before, query) · `task_add` · `task_done` ·
`task_update` (due, importance, owner, pin, drop) · `task_why` (the source and evidence).
None of them asks first: they change only Scout's own list.

## The pulse and the briefing

The pulse (every 15 minutes, plain code) says what's due within the hour, once. The
morning briefing adds the top three and anything overdue. Waiting-on items past their
date are offered as follow-ups ("Pat owes you the vendor quote since Tuesday").

## Not now

Slack (the next source, same farm), effort estimates feeding a calendar plan, and
dependencies between tasks.

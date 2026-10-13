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
- source: meeting:not_1d3tmYTlCICgjy
- found: 2026-10-13 14:02
- reminder: x-apple-reminder://...
- from: meeting
- reminder_title: Send the revised travel budget to Pat

Pat asked for the revised numbers before Monday's review ("can you get me the travel
cut by Friday?").
```

`status` is open, done or dropped. `owner` is `me` or the person who owes it (waiting on);
a name or address from `[tasks] me` (or just your first name) is stored as `me`. `from`
(mail or meeting) marks a task the farm found; `reminder_title` is the title its reminder
was made with (the helper checks it when completing). You can edit any line, add tasks by
hand, or delete a file; the index follows within a minute.

## Finding tasks (the farm, in the Scout app)

Every few minutes the farm looks at items new to the store since its checkpoint
(`state/tasks_farm.json`):

1. **Filter, no AI.** Mail from no-reply senders, newsletters, receipts and notifications
   is skipped, as is anything already read for tasks. Mail without an action cue ("can
   you", "please", "by Friday", "follow up", "I'll", "action item", "waiting on") is
   skipped too, and so is mail that is also stored as a meeting summary (it's read once,
   as the meeting). Meetings go through once all their parts have been stored for five
   minutes (a Granola note's transcript arrives in pieces).
2. **Extract.** Meetings go to Claude, email to the local model, with one prompt; in
   strict privacy mode, meeting summaries that came by email go to the local model too.
   It returns JSON: one item per task with title, owner (`me` or a name), due date,
   importance, urgency, effort, and a short quote as evidence. Only commitments count,
   not ideas or topics. You are identified by `[tasks] me` (names and addresses), and
   in Granola transcripts by the lines marked "Me:". A task whose quote isn't in the
   text (case and spacing aside) is dropped, and at most five come from one item. An
   answer that isn't valid JSON (cut short, say) isn't taken as "no tasks": the item is
   tried again next pass, three times in all, then left.
3. **Dedupe.** A found task that matches an open one closely, by meaning and the same
   owner (first names compared: "Pat" is "Pat Doe"), adds its source and quote to that
   task instead of making a new one, along with an earlier due date or a higher
   importance or urgency.
4. **Write.** The task goes into its file and Reminders. The farm doesn't write to the
   journal; the Scout app's log says what it found.

Backfill: on the first run, meetings and mail from the last `[tasks] backfill_days`
(14) are read. Older ones are left alone, since what they asked for is likely done.

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

What a found task holds came from someone's email or meeting, so Claude sees it the way
the privacy mode says (privacy.py): in open mode, everything, with the quotes marked as
data, not instructions; in balanced mode, titles but not the quoted context; in strict
mode, "a task from an email" or "a task from a meeting" with its number and due date.
The same goes for tasks in `local_search`. Tasks you add are shown as they are.

## Reminders

Only the Mac's Reminders is mirrored to, never Scout's own lists (`[lists] backend =
"scout"`, or Reminders unavailable: no mirroring). A task that couldn't be mirrored is
tried again on each farm pass. Completions are read by reminder id (the helper's
`reminder-get`), so they sync however long the lists are. A renamed task or a new due
date changes its reminder (`reminder-update`); a task moved between yours and waiting on
finishes its old reminder and gets one in the other list.

## The briefing

The morning briefing says your top three tasks and how many more are overdue. Waiting-on
items past their date are offered as follow-ups ("Sam owes you the vendor quote, due
Friday, October 9; maybe follow up"). The pulse has no task code.

## Not now

Slack (the next source, same farm), effort estimates feeding a calendar plan, and
dependencies between tasks.

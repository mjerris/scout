# Tasks

What you owe and what others owe you, filled from your meetings, mail and Slack and
kept in order. How it was designed and why: [tasks-design.md](tasks-design.md).

Scout keeps a task list of what you owe and what others owe you, and fills it from your
meetings and email. Each task is a markdown file in
`memory/tasks/` (`0012-send-the-budget-to-pat.md`): a title, `- key: value` fields
(status, owner, due, importance, urgency, effort, pin, source) and its context. Edit,
add or delete files and the list follows. Open tasks are mirrored to Reminders (yours in
**Scout**, others' in **Waiting On**); completing one there completes the task.

The order is plain code, so it's stable and says why: a pin from you, then overdue, due
today or tomorrow, important, urgent, quick wins when nothing presses, people you've
told Scout about; undated tasks sink with age, and what others owe you comes up when
it's due. Ask "what should I work on?", "what am I waiting on?", "add a task...",
"mark task 3 done", "make the budget top priority". The morning briefing names your
top three and follow-ups that are overdue.

**Found tasks.** Every 10 minutes while Scout is idle it reads what's new (the last two
weeks, the first time), once each. Meetings, Granola notes and meeting notes that come
by email, are read by Claude (`[tasks] extract_model`), a few at a time. Email and Slack
are read by the local model and stay on the Mac; no-reply senders, newsletters and mail without an action
cue are skipped without any AI. Found tasks are added directly, each with the quote
that shows it and its source (`task_why`). A task that repeats an open one adds to it.
Put your names and addresses in `[tasks] me` so it can tell your tasks from others'.

**Meeting notes by email.** Google Meet's Gemini notes, Zoom, Otter, Fireflies, Fathom,
tl;dv and Teams recaps are recognized by sender and subject and stored as meetings, in
full, so they're searched and read for tasks like Granola notes. Only what's in the
email: a transcript that lives only in a linked Google Doc isn't reachable.

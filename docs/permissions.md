# Permissions: what runs, what asks

What Scout and its Claude agent may do on their own, what asks you out loud first, and
how a yes can be made to stick.

## Approval policies

- **Permissions:** `claude.approval_policy` picks who decides what runs
  without asking:
  - `strict` (default): read-only tools and a short list of harmless commands
    (`date`, `ls`, ...) run on their own; everything else is asked out loud.
  - `settings`: your `~/.claude` allow rules and hooks apply; you're asked
    only where Claude Code would normally prompt.
  - `settings_no_hooks`: your allow rules apply but your hooks are off, since a
    hook that answers "allow" would skip every prompt.

  Under every policy: **shell commands** always ask (except the harmless list
  and exact commands you've said "yes, always" to; your `~/.claude` Bash allow
  rules don't apply to the voice agent), **fetching a page or opening a URL**
  asks once per site, **secrets** (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`,
  `~/.config/gh`, keychains, this app's `state/`) can't be read at all, and
  this app's own files can't be changed. `claude.always_ask` rules (git push,
  PR merge/create) are always asked, even with a saved rule, in any spelling
  (`git -C ~/x push`, `env git push`).

  Spoken prompts are short ("Run git push?", "Edit config.py in src?"); the web
  page shows the full command first. Answers are strict: only a clear yes or no
  counts ("yes", "sure, go ahead", "no", "don't"); anything unclear ("okay, hold
  on", "what does it do?", "I'm not sure") gets "yes or no?" again. A question
  asked only on your phone can't be answered by the room's mic. No answer within
  30 s counts as a skip.

## "Yes, always"

- **"Yes, always":** answer a prompt with "yes, always" (or "yes, don't ask me
  again", or tap **Always** on the web page) and it stops asking for that exact
  shell command, that site (for page fetches and URLs), or that tool (for MCP
  tools that only read). File edits, writes, always-ask commands and MCP tools
  that send, post, create or delete always ask. "Yes, but always ask me" is a
  one-time yes. Saved rules are in `state/voice_allow.json` and can be removed
  from the web page.

## Trust: earning "just do it"

Everything that changes something asks by voice first. One action at a time can earn
"just do it": answer a confirmation with "yes, always", or say "always allow adding
reminders"; "ask me before adding reminders" takes it back, and "what do you do
without asking" lists them. Adding and completing reminders, adding calendar events
and deleting notes can be promoted; sending email always asks. After five yeses in a
row Scout mentions the option once. Every decision is logged in `state/trust.json`.

## Typed approval

Typed approval (opt-in): when you approve a send in a Claude Code session's chat, you
can skip the spoken question. With

```toml
[confirm]
typed = true
```

the session's `mail_send`, `slack_send`, `message_send` and `call_start` take
`approved: "typed"`, which the tool descriptions allow only when you explicitly approved
that exact message (or call) in your own words in that chat. Scout logs that the
approval was typed, sends, and says one line ("Sent to Joao Santos."). Nothing else
takes it, the phone and the room never do, and without the key `approved: "typed"`
still asks aloud.

## Changing the app by voice

- **Changing the app by voice:** the voice agent can't edit this project. When
  you ask it to change how it listens, talks or asks, it calls its
  `request_app_change` tool, which appends to `state/change-requests.jsonl`
  and logs `change request: ...` for the Claude session that maintains the app.

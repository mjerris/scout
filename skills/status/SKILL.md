---
name: status
description: Show whether Scout's background app is set up and running (setup progress, login item, who has the floor, voice layer). Use when the user asks about Scout's status, or when a Scout tool says the app isn't running.
allowed-tools: Bash("${CLAUDE_PLUGIN_ROOT}/scripts/scoutctl.sh" status)
---

Run this and summarize the result in a sentence or two (setup state, whether the
app is running, and anything that needs the user):

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/scoutctl.sh" status
```

If setup is `blocked` because the old claude-voice app is running, say so: it has to
be retired before Scout can take the microphone. If it's `error`, show the last lines
of `"${CLAUDE_PLUGIN_ROOT}/scripts/scoutctl.sh" logs 20`.

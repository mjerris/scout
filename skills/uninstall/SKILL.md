---
name: uninstall
description: Stop Scout's background app and remove its login item (keeps config, rules and models unless asked to purge).
disable-model-invocation: true
---

Removing the plugin (`claude plugin uninstall scout`) doesn't stop the background
app, so do this first. Confirm with the user, then run:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/scoutctl.sh" uninstall
```

Only if the user explicitly asks to delete Scout's data too (config, saved rules,
models, logs), add `--purge`. Then tell them they can remove the plugin with
`claude plugin uninstall scout`.

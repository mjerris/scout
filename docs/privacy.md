# Privacy: what Claude sees

What of your mail, calendar, messages, meetings and memories reaches Claude, and what
stays on the Mac.

Scout keeps your private data on the Mac where a local model can do the job.
Email is summarized by the tier 1 model (`claude.local_model`) on this Mac, and
`privacy.mode` in config.toml (enforced in code, `src/scout/privacy.py`) decides
what reaches Claude, for the room and every other session alike:

| | `strict` | `balanced` (default) | `open` |
|---|---|---|---|
| Email listings | sender, subject | sender, subject, one-line local summary | sender, subject |
| One email (`mail_read`) | sender, recipients, subject | same, plus a few-sentence local summary | full text |
| Exact text (`mail_read_full`) | refused | full text (for quoting in a reply) | full text |
| Calendar | titles, times, places | titles, times, places | plus notes and attendees |
| Memories | none | the ones that bear on the request | the ones that bear on the request |
| Local answers from private data | withheld | shared | shared |

In strict mode Claude says plainly when a task needs more. Asked directly, Scout
itself always answers locally: "what did the Rover email say" and "summarize my
unread mail" are read and summarized on the Mac and spoken, without Claude.

Emails that address an AI, try to give instructions, or are password or prize
lures are caught in code before the model sees them and described as "looks
like a scam or a manipulation attempt" (small models relay injected claims as
fact). Other summaries are attributed to the sender ("Pat asks you to...").

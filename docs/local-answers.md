# Answered on the Mac: the local model, follow-ups and cost

Plain code (tier 0) and a small local model (tier 1) answer what they can before Claude
is asked; this is what they answer, how they're kept from answering wrong, and what a
Claude turn costs when they don't.

## The local model (tier 1)

Before Claude, a small local model (`claude.local_model`, Qwen3 4B on MLX) gets one
chance at a request plain code didn't answer. It only picks a read-only lookup or
"Claude", and plain code decides first: anything with an action word (send, reply,
add, change, order...), judgment ("should", "why", "what do you think"), or more than
one step or sentence ("...and text it to Carol", "...? Reply that it's fine", "... - do
it") goes straight to Claude without asking the model. A verb asked about in the past is
a question, not an action: "when did I last change the oil" and "what did Pat tell me"
pass; "did you send it" (Scout's own doing) goes to Claude.

What it answers: the calendar, the inbox (what an email says is summarized on the Mac),
and with `claude.local_recall` (on by default) the weather, the task list, and
**recall**: questions about what's in Scout's records: "what did Pat say about the Q4
budget", "when did I last change the generator oil", "what was decided in the vendor
meeting", "what's my wifi password". A recall searches the local store ([memory.md](memory.md)) by the
question and by the model's few words, shows the model the best five records, and takes
its answer only if it names one record, quotes it word for word, and passes checks in
plain code. A record found by meaning is shown only if it's close enough
(`claude.recall_min_similarity`, 0.64); with none that close the model isn't asked and
Claude answers. The answer's refs (what "send me a link to that" opens) are the one
record it quoted, never the others it was shown. The checks: the record has the question's topic words (a note about the truck's tires
doesn't answer "when did I rotate the RV tires"), every number in the answer is in the
record, and nearly every other word too. Anything else, including "not found", goes to
Claude, which still has the request's context. Emails that address an AI or are lures
are never shown to the model. An answer that read mail, a meeting or Slack is private
like other local answers (withheld from Claude in strict mode). The phone's "ask Scout"
goes through the same tiers, so these are answered on the Mac by phone too.

Measured with `scripts/bench_recall.py`, on a synthetic store (21 records, one a lure,
among 300 distractors) and 100 labelled requests: 22 answerable recall questions, 15
with no answer in the store, 10 weather and task lookups, 27 actions or judgment phrased
as questions, and the 41 routing requests of `scripts/bench_tier1.py`:

| Qwen3 4B (Instruct 2507, 4-bit) | |
|---|---|
| Answered locally when it should have gone to Claude | 0 of 60 (0 of 27 adversarial, 0 of 15 with no answer) |
| Answerable recall questions answered right locally | 16 of 22, none wrong (2 more went to a mail search; the rest to Claude) |
| Routing (weather, tasks, the original 41) | 10 of 10, 41 of 41 |
| Time (median, on the Mac mini with Scout running) | routing 1.5 s, a recall's search and answer 2.4 s: 3.8 s in all. Claude needs at least two round trips for the same (a search, then the answer; ~1.5-2.5 s each) |

### Follow-ups

"What did he say about it?", "and when is that due?" and "what about the other one?"
name no subject. A search by their own words finds nothing, or the wrong thing. With
`claude.local_rewrite` (on by default), such a question is first rewritten as one that
stands alone, from what was said in the last ten minutes.

- Plain code spots a follow-up: a short question with a pronoun that points back, or
  one that starts "and ..." or "what about ...". Other questions pay nothing (a few
  microseconds to check).
- When the last answer was about one record and one person, code replaces the pronouns:
  after Pat's budget email, "what did she say about hiring" becomes "what did Pat say
  about hiring".
- Otherwise the local model writes it from the last three turns (what you asked, what
  Scout said, the titles of the records): JSON only, at most 40 tokens, two seconds at
  most, and only words from the conversation. Anything else and Claude takes it.
- The records just spoken of are read first. The answer prompt shows your own words.
  Every topic word of the follow-up must be in the record: "what about my Chicago
  flight?" isn't answered from the Denver one.
- A follow-up that acts or asks for judgment ("and send it to Sam", "should we switch")
  goes to Claude before any rewrite.

All of this stays on the Mac. Claude gets the request as you said it, under the same
privacy rules.

Measured with `scripts/bench_followup.py`: two-turn conversations over the same
synthetic store, each follow-up run as asked, rewritten, and rewritten by the model
alone. The held-out set was written after tuning, and run once.

| Qwen3 4B (Instruct 2507, 4-bit) | As asked | Rewritten |
|---|---|---|
| Follow-ups answered right locally | 1 of 22, 2 wrong | 20 of 22, none wrong |
| Held-out follow-ups answered right locally | 0 of 10 | 4 of 10, none wrong (5 in an earlier run) |
| Answered locally when it should have gone to Claude | 0 of 12; held out 1 of 5 | 0 of 12; held out 0 of 5 |
| Rewrite time on follow-ups | | p50 0.40 s, p95 0.57 s (by code alone: under 10 ms) |

A rewritten follow-up about the records just spoken of skips the routing call, so a
follow-up takes about as long as before. Of `bench_recall.py`'s 115 requests, plain code
takes 8 for follow-ups; all 8 act or judge, so they go to Claude with no rewrite. Most
misses are a word the record lacks ("when is Luis coming", "where is it flying from"),
and those go to Claude.

## Usage: what answered

Every request is logged in `state/ledger.jsonl`: which tier answered (0 plain code,
1 the local model, or Claude), how long it took, and for Claude the turn's own cost
(the session reports a running total; Scout records the difference), turns and tokens
(uncached input, cache writes, cache reads, output). "How much have you spent today /
this week?" answers from it locally, tokens included.

### What a Claude turn costs

The room assistant and the phone's "ask Scout" are lean by default: they load no
`~/.claude` settings files (`claude.setting_sources = []`), so your CLAUDE.md, auto
memory, personal skills, hooks and other plugins aren't sent with every request. Only
Scout's own skills load (`claude.plugins = "scout"`), and `claude.inherit_settings`
copies just the parts of `~/.claude/settings.json` the room uses: your permission rules
(so the `settings` approval policies behave as before), model and effort. The `settings`
policy brings your hooks along too. `setting_sources = ["user"]` with `plugins =
"enabled"` restores the old full load.

`scripts/bench_brain.py` measures it with the real room options (a few synthetic
questions, one calling the weather tool; about $0.15 a variant on Opus). On Opus, a
first turn went from about 29k input tokens to 22k and a turn with one tool call from
97k to 72k. Sonnet costs about 40% less a turn than Opus, with a slower
first word.

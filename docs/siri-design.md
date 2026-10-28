# Scout and Siri: proposal (draft)

Reach Scout through Siri wherever there's an iPhone and no Scout mic: in the truck over
CarPlay, on AirPods, on a walk. Scout's room voice stays as it is; this adds a door, not a
second assistant.

**Status:** step 1 is built, except "Call Scout": the Mac's `/api/phone/key` and
`/api/phone/ask` (`web.py`), the two keys (`phone_devices.py`), and the app's intents
(`app/plugins/ScoutIntents.swift`, added by `withSiriIntents.js`; `app/src/siri.ts`).
Still to do: the checks on a device below.

**Recommendation:** add App Intents to the phone app, in three steps. First "Ask Scout"
plus a few one-phrase questions, answered through Scout's own tiers. Then actions, with
Siri's confirmation and the Mac's one-time codes. Last, and only if it earns its
privacy cost, Scout's tasks and notes in the iPhone's Spotlight index. Model delegation
(Scout as Siri's brain) isn't open to apps; watch for it, build nothing for it.

## What Apple offers (iOS 27, WWDC26)

Checked against Apple's own pages, unless marked otherwise:

- **App Intents are the way in.** Apple Intelligence "uses the app intents, app entities,
  and app enums you define" to power Siri AI ([Apple Intelligence and Siri AI][siri-ai]).
  An intent can answer in words: `ProvidesDialog` returns a full and a supporting string,
  and Siri reads the full one "on voice-only devices like AirPods"; it can ask for a
  missing value mid-intent (`requestValue`) ([WWDC26 343][343]).
- **Schemas** let Siri run an app's action from everyday speech with no phrases to
  register: Siri "handles the language understanding. Your app focuses on the action"
  ([WWDC26 240][240]). The domains: audio, calendar, camera, clock, mail, maps, messages,
  notes, phone, photos, reminders, search, and a few Shortcuts-only ones
  ([App schema domains][domains]).
- **No general "ask my assistant" schema.** The one `.assistant` schema, `activate`,
  puts a voice app on the iPhone's side button, and "is available only in Japan"
  ([Assistant domain][assistant]). Outside schemas, an app's own intent is reached by an
  App Shortcut phrase that names the app ("Ask Scout").
- **Confirmation:** Siri confirms intents "with meaningful side effects" by itself, and
  assumes an entity is private unless the app says otherwise ([WWDC26 343][343]).
- **Semantic index:** entities indexed in Spotlight (`IndexedEntity`) can be found by
  Siri "even when someone describes it vaguely" ([Apple Intelligence and Siri AI][siri-ai]).
- **Not public:** a third-party model or agent behind Siri. A widely shared demo of
  Claude in Siri's "Ask…" menu uses a private entitlement,
  `com.apple.developer.model-delegation` ([post][pdfu]). Apple's newsroom names Claude and
  Gemini only for the Foundation Models framework, inside an app's own code
  ([newsroom][news]). Reports of system-wide MCP in Siri contradict each other; Apple
  mentions MCP only for Xcode 27 plug-ins.
- **Unverified, from third-party write-ups:** `LongRunningIntent` for work over 30
  seconds; SiriKit deprecated in favor of App Intents. Check both on a device before
  relying on them.

## What Siri already reaches without any of this

Scout keeps much of its work in Apple's own apps, and Siri already speaks to those:

- **Tasks:** open tasks are mirrored to Reminders (yours in **Scout**, others' in
  **Waiting On**), and completing one there completes the task. "Hey Siri, what's on my
  Scout list" and "mark send the budget done" work today.
- **Notes:** with `[notes] apple = true` they're copied to an Apple Notes folder called
  Scout, which Siri can read and search.
- **Mail, calendar, messages, contacts:** Scout uses the Mac's own stores, the same
  accounts Siri uses on the phone.

So schemas for reminders, notes, mail or calendar in Scout's app would compete with
Apple's own apps for the same data, and Siri would have to pick. They're left out.

## What's worth building

| Integration | What it gives Scout | Cost | Verdict |
|---|---|---|---|
| "Ask Scout" (an App Shortcut, free text) | Everything Scout answers (recall from meetings, Slack and mail, tasks, Starlink, routes, Claude) from CarPlay, AirPods, Watch | Swift module in the app; one signed endpoint on the Mac | **Build (step 1)** |
| One-phrase shortcuts ("Scout briefing", "what should I work on in Scout", "is Starlink down in Scout") | No second prompt: a shortcut with no free-text parameter runs from one sentence | A few lines each, on top of the above | **Build (step 1)** |
| "Call Scout" | Hands a Siri question over to a full phone conversation | Opens the app and dials; the call already exists | **Build (step 1)** |
| Actions through Siri ("tell Scout to add a task", "remember…") | Writes without opening the app | Two-step codes, as the phone does, plus Siri's own confirmation | **Build (step 2)** |
| Answer an alert ("reply to that" after a Scout notification) | Siri acts on what Scout just pushed | Entity ids on the push notifications | Step 2, if alerts become notifications people answer |
| Scout's tasks and notes in Spotlight (`IndexedEntity`) | Siri AI finds Scout records by meaning, offline | A copy of private records on the phone; sync | **Later, opt-in**, and only tasks, notes and facts |
| Schemas for reminders, notes, mail, calendar, messages | Little: Siri already reaches the same data in Apple's apps | Competes with Apple's apps | **No** |
| `.assistant` side button | Scout on the side button | Japan only | **No** |
| Scout as Siri's model (model delegation) | Every Siri question through Scout | Private entitlement | **Watch only** |
| Siri on the Mac running Scout | Little: Scout already listens in the room | A Swift helper with its own intents | **No** |

## Step 1: Ask Scout

**Phrases.** App Shortcut phrases must name the app, and their parameters can be only
entities or enums, not free text (true through iOS 26; check iOS 27). So "Hey Siri, ask
Scout" is two turns: Siri says "What do you want to ask?" (`requestValue`) and you say
the question. Fixed questions are one turn each: "Scout briefing", "what should I work
on in Scout", "what am I waiting on in Scout", "is Starlink down in Scout", "Scout sync
status".

**The app.** A small Swift module (an Expo config plugin adds the files to the app
target, as `plugins/` does for the scene lifecycle now) defines the intents and one
`ScoutClient` that signs requests the way `sign.ts` does: device, timestamp, nonce, HMAC
with the device secret, for a purpose that names the request. Siri runs the intent in
the background; the app's UI isn't opened. The answer comes back as an `IntentDialog`,
spoken in full on CarPlay and AirPods.

**The Mac.** One new route, `POST /api/phone/ask`, signed by a paired device (no web
token, which stays on the Mac), answered by `assistant.ask(PHONE, text)`: the same tiers
and the same rules as the phone's `ask_scout`. Nothing is said in the room and a room
conversation is left alone. Read-only, like `ask_scout`: a request that would change
something gets "Open Scout to do that" until step 2.

**Slow answers.** A Claude answer can take 10 to 30 seconds; Siri may not wait that long
(to be measured). `ask()` already keeps a late answer for 10 minutes. The intent waits up
to its limit, and past it says "Still working; I'll send it to your phone." Scout then
pushes the answer as a notification (push already exists for pairing and rings).

**Locked phone: two keys, each with its own abilities.** The pairing secret is in the
Keychain as `WHEN_UNLOCKED_THIS_DEVICE_ONLY`, so with the phone locked (in a pocket, on
CarPlay) it can't sign. So a phone has a second key, kept
`AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY`, which the app asks Scout for with the pairing key
(`POST /api/phone/key`) on every launch. A request signed with it says `key: "locked"`.
What each key may do is config:

```toml
[phone]
unlocked_key = ["call", "ask", "push", "pair", "ring"]   # the default
locked_key = ["ask"]                                     # the default
```

So by default a stolen, locked phone could ask Scout questions, but not call, pair
another device or act. `locked_key = []` turns Siri off with the phone locked;
`scoutctl phone forget` revokes both keys. Only the unlocked key can get a new locked key.

**Privacy.** Through Siri, the question and Scout's answer pass through Apple: speech
recognition, Siri AI, and possibly Apple's servers. That's the same position as
SignalWire on a phone call, so the same rules apply: the phone path's privacy mode, and
docs that say so plainly. In `strict` mode an answer built from mail, meetings or Slack
stays withheld, as it is for Claude.

## Step 2: Actions

"Tell Scout to add a task to call the dealer", "remember the tire pressure is 80": the
Mac answers with what it would do and a one-time code, as the phone's two steps do; the
intent shows it with `requestConfirmation` ("Add task: call the dealer?"); a yes sends
the code back and the Mac does it once. Sending anything under your name (Slack, mail,
texts) stays out of Siri: Siri's yes is a tap or a word Scout never hears, and those
always ask by voice in the room or the read-back on a call.

## Step 3 (optional): Scout's records in Spotlight

Index tasks, notes and remembered facts as `IndexedEntity` on the phone, so Siri AI and
Spotlight find them by meaning, offline and with no request to the Mac. Mail, meetings
and Slack stay off the phone. It needs a sync from scout-memory to the app, and it puts
private records into the phone's index. Worth it only if step 1 shows the round trip to
the Mac is the problem.

## To check on a device first

1. How long Siri waits for an intent on iOS 27, on the phone, CarPlay and AirPods.
2. Whether iOS 27 lets an App Shortcut take free text in one sentence.
3. Whether an intent runs while the phone is locked, and which Keychain access it needs.
4. Whether `LongRunningIntent` exists, and what Siri says while it runs.
5. Whether the Expo build keeps the App Intents metadata Xcode extracts at build time.

## Cost

Step 1: the Swift module and config plugin (a few hundred lines), the signed route and
its tests on the Mac, the push for late answers, and a device test pass. Step 2 reuses
the phone's two-step codes. No new services, accounts or costs.

[siri-ai]: https://developer.apple.com/documentation/AppIntents/apple-intelligence-and-siri-ai
[domains]: https://developer.apple.com/documentation/AppIntents/app-schema-domains
[assistant]: https://developer.apple.com/documentation/AppIntents/app-schema-domain-assistant
[240]: https://developer.apple.com/videos/play/wwdc2026/240/
[343]: https://developer.apple.com/videos/play/wwdc2026/343/
[news]: https://www.apple.com/newsroom/2026/06/apple-aids-app-development-with-new-intelligence-frameworks-and-advanced-tools/
[pdfu]: https://x.com/itspdfu/status/2099122424209916015

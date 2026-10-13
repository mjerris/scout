# Scout by phone: design (draft)

Talk to Scout from a phone app, anywhere. SignalWire carries the call and does the
speech (ASR, TTS) and the conversational agent; the Mac never receives audio. Scout
receives only signed webhooks (tool calls and events) through a Tailscale Funnel URL,
and pushes late answers back over SignalWire's REST API (outbound, so CGNAT is fine).

```
phone app ──WebRTC──▶ SignalWire (call, ASR/TTS, AI agent)
                          │  SWML fetch, SWAIG tool calls, events (HTTPS, signed)
                          ▼
              Tailscale Funnel  https://<mac>.ts.net:8443
                          ▼
              scout-phone (127.0.0.1:8768) ──local API──▶ Scout (tools, memory, Claude)
                          │
                          └── REST ai_message / ai_unhold ──▶ SignalWire (late answers)
```

## Pieces

**scout-phone**: a separate local service, like scout-memory (its own LaunchAgent, off
by default behind `[phone] enabled`). It runs a `signalwire.AgentBase` agent (FastAPI and
uvicorn, kept out of the voice process) on 127.0.0.1:8768. Funnel publishes only this
port; the web UI, MCP and memory service stay private. Its tool handlers call Scout's
existing shared tools through Scout's local API, so trust, the audit log, privacy modes
and the ledger apply the same as at home.

**The agent**: SignalWire's model holds the conversation; its tools are:

| Tool | What it does |
|---|---|
| Read tools (calendar, reminders, notes, weather, places, route, Starlink) | Scout's shared tools, as SWAIG functions |
| `ask_scout(request)` | Anything that needs Scout's own brain (memory, mail, Claude): Scout's tiers 0/1/Claude answer it as text |
| Action tools (reminder add/complete, calendar event, note delete) | Two steps: the first call returns what would happen and a one-time code; the agent reads it back; a second call with the code and the user's yes does it |
| Never by phone | sending email |

Long answers: a tool returns at once with a hold ("Let me check that") and `wait_file`
or fillers. When Scout has the answer it sends it with REST `calling.ai_message` and
`ai_unhold`. That is outbound from the Mac, so there are no long-held webhooks.

**Events**: `on_summary` (post-prompt), `on_call_end` (call log), and the address's
status callback go to Scout's transcript and ledger (tier "phone").

## Who is calling

A voiceprint is not used for access. The app proves it with a secret it got at pairing,
sent with each call in `userVariables` (the data `@signalwire/js` passes with `dial()`).

1. **Pairing, once, at home.** Scout's local web UI (tailnet only) shows a QR code. The app
   scans it and stores what it carries in the Keychain, behind Face ID:
   - `device`: an id for this phone;
   - `secret`: 32 random bytes, shared only by this app install and Scout;
   - the agent's address, and SignalWire subscriber credentials (a token plus a refresh
     token, `scope="sat:refresh"`), so the app can sign in to SignalWire without reaching
     the Mac.
2. **Each call.** The app dials the agent's address with
   `userVariables = {device, ts, nonce, sig}`, where `sig = HMAC-SHA256(secret,
   "device|ts|nonce|address")`.
3. **Check.** On the SWML fetch, a per-call config callback (`add_per_call_config`) checks
   four things before the agent gets any tools:
   - the device is paired and not revoked;
   - `ts` is within 60 s;
   - the nonce hasn't been seen;
   - the signature matches (constant-time).

   It then binds the `call_id` to the device. Anything else gets an agent with no tools,
   which says goodbye and hangs up.
4. **Every tool call re-checks** that its `call_id` is bound to a verified device, because
   `global_data` comes from the call.
5. **Revoking.** "Hey Scout, forget my phone" (or the web UI) deletes the device's secret,
   so a lost phone stops working at once.

`userVariables` are set by the caller, which is why they carry a signature, not a claim.
Only the paired app and Scout hold the secret. The timestamp and nonce stop a captured
setup from being replayed.

Request authentication, in layers:
- SignalWire's request signature (`X-SignalWire-Sha256-Signature`, HMAC-SHA256 with the
  space's signing key). `SWML_PROXY_URL_BASE` is set to the Funnel URL so signatures
  match behind the proxy.
- Basic auth embedded in the webhook URLs.
- Per-call SWAIG function tokens. `swaig_secret` is kept in `state/phone.secret` (0600)
  so tokens survive restarts.

## Privacy

On the phone path, what Scout says passes through SignalWire's ASR, LLM and TTS. The
privacy mode applies as it does at home: in strict mode, mail and messages give
summaries only. The phone path is opt-in, and README states this plainly.

## Apps

1. **Browser first**: `@signalwire/react`, pairing secret in browser storage. This proves pairing, signing,
   the agent and the webhooks end to end.
2. **React Native**: `@signalwire/react-native` (github.com/lpradovera/signalwire-react-native-unofficial)
   on the same hooks, with CallKit and Bluetooth routing. In the truck, Scout is a
   hands-free call through the Kenwood.
3. **Later, Scout calls you**: inbound push (PushKit and CallKit) for alerts that matter
   when nobody is in the rig.

## Setup the user does

- Allow Funnel for this Mac in the tailnet policy (the `funnel` node attribute). Then
  `scripts/tailscale.sh funnel` publishes :8443 → 8768 only.
- Put the SignalWire space, project ID, API token and signing key in Scout's config.
- A setup script creates the agent's Fabric address over REST.

## SDK work (our PRs; IM Luca about RN changes when needed)

- signalwire-python: a documented way to reject a call from the per-call config callback
  (hang up without an agent), and an `on_start` wrapper for `startup_hook`.
- signalwire-python: document the SWAIG webhook timeout; and an aiohttp mount, or a
  public framework-free SWAIG dispatch (today `_execute_swaig_function` is private).
- RN library (PR to Luca's repo): voice calls start on speakerphone (audio mode follows the app's
  video capability); iPhone hardware testing; set `userVariables` at dial from the hooks.

## Phases

1. scout-phone with read tools, `ask_scout` and events. Test by dialing from the
   SignalWire dashboard, with Funnel on.
2. Pairing (QR code, device secret), signed userVariables and the per-call check.
3. The browser app.
4. Two-step action tools.
5. The React Native app with CallKit and Bluetooth.
6. Scout calls you.

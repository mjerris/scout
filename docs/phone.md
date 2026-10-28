# Phone calls (SignalWire)

Talk to Scout from the phone app, anywhere. This is how to turn it on, pair a phone and
test it; the design and its reasoning are in [phone-design.md](phone-design.md).

Off unless turned on (design: [phone-design.md](phone-design.md)). A paired
phone app calls a SignalWire AI agent; SignalWire does the speech and the conversation,
and the Mac only receives signed webhooks through Tailscale Funnel. **What Scout says on
the phone passes through SignalWire's speech recognition, model and text-to-speech**; the
privacy mode applies as it does at home.

```toml
[phone]
enabled = true
signing_key = "<signing key>"                        # SignalWire Dashboard, API Credentials
public_url = "https://<mac>.<tailnet>.ts.net:8443"   # the Funnel base
address = "<the agent's address>"                    # what the app dials
```

`scout-phone` (its own login item, `com.local.scout.phone`, started by the installer
only when `enabled = true`; `scripts/run-phone.sh` runs it in the foreground) listens on
127.0.0.1:8768. It refuses to start without `signing_key` and `public_url`. On a call it
offers the read-only shared tools (calendar, reminders, notes, mail, texts, weather,
places, routes, Starlink, local search) and `ask_scout`, which answers through Scout's
own tiers (local intents, memory, the local model with its recall from Scout's records,
Claude on a session of its own) and
returns text: nothing is spoken in the room and a room conversation is left alone.
Actions (adding a reminder or calendar event, completing a reminder, deleting a note) take
two steps: the tool says what it would do and gives a one-time code, the agent reads it
back, and `confirm_action` does it only if a SignalWire semantic gate (a separate
decision model reading the conversation) finds that you clearly said yes; a yes read out
of an email or message doesn't count. Email works the same way, with tighter limits (see
[mail.md](mail.md)): short messages to people you know, read back first; anything longer is left in
your Drafts.

Pairing the phone app, in three steps (nothing to type, and the web token stays on the Mac):

1. Turn on Tailscale on the iPhone.
2. Run `scripts/scoutctl.sh phone pair` (or **Pair the phone app** on the `/phone` page)
   and point the iPhone Camera at the QR code; tap the Scout banner. Or, at home, open the
   app: it finds Scout on the network ("Found <your Mac>") and asks to pair. Nothing pairs
   until you say yes from something you already have: a phone already paired (its app
   shows the request while open), Scout's web page, the alert on the Mac, the room ("A new
   device calling itself <phone> wants to pair with Scout, code 47. Should I allow it?"),
   or `scoutctl phone pair approve <id>`; with push set up, the paired phones also get it
   as a notification with Approve and Deny. Approve only if the phone shows the same code.
3. Tap **Pair** in the app.

```sh
scripts/scoutctl.sh phone pair               # a one-time code (10 minutes) as a QR code and scout:// link
scripts/scoutctl.sh phone pair list          # devices asking to pair now (id, name, code, Tailscale login)
scripts/scoutctl.sh phone pair approve <id>  # or deny <id>; the first answer from anywhere wins
scripts/scoutctl.sh phone list
scripts/scoutctl.sh phone forget <device>    # a lost phone stops working at once
```

**Scout calls you.** With an APNs key in `[phone]` ([app/README.md](../app/README.md), "Push": the Apple
portal steps), an alert listed in `[phone] ring_for` ("pulse", "slack", "starlink",
"session") also rings the phone as a call from Scout when nobody has spoken in the room
for `ring_after_quiet_min` minutes; answer, and Scout tells you. Off by default
(`ring_for = []`). `scoutctl phone ring [text]` rings it by hand, `scoutctl phone rings`
lists how recent rings went. The key file stays on the Mac, mode 600.

The link is `scout://pair?server=<[web] public_url, else the Tailscale Serve name>&code=<code>`.
A code works once; only its hash is stored, and wrong codes are rate-limited. Scout
advertises itself as `_scout._tcp` over Bonjour (with its https address, never a token)
for the app to find. The app's manual form takes the address and code, or a pasted link.
`[phone] pair_mode` is `"tailnet"` (default) or `"approve"` (a request waits for a yes, as
above) or `"code"` (QR codes only); `[phone] owners` adds Tailscale logins that count as
you, and `[phone] pair_alert = false` turns the Mac's alert off. The prompt says which
Tailscale login asked, as tailscaled reports it (`tailscale whois` for the address the
request came from), never as the request's own headers claim; it labels the request
and never pairs it by itself. Scout's web server answers only to this Mac's own names
(localhost, its LAN and tailnet addresses at its port, its Tailscale name), so a web page
that points its own name at 127.0.0.1 (DNS rebinding) is refused.

Each call carries `userVariables = {device, ts, nonce, sig}` with
`sig = hex(HMAC-SHA256(secret, "device|ts|nonce|address"))` (the secret base64url-decoded
to its 32 bytes, `ts` in Unix seconds). A call whose device is unknown or forgotten, whose
timestamp is more than `call_skew_s` off, whose nonce was used before or whose signature
doesn't match gets no tools: it hears a short goodbye and is hung up. Every tool call is
checked again against its call. Basic-auth credentials and the SWAIG token secret are
kept in `state/phone.auth` and `state/phone.secret`, the paired phones in
`state/phone_devices.json` (all mode 600).

## Test call from a browser

`/phone` on the web UI (tailnet only, never through Funnel) places a verified call from
the browser. Besides `address` it needs `space`, `project_id` and `api_token` in
`[phone]`: Scout mints a one-hour SignalWire subscriber token for the page
(`POST /api/fabric/subscribers/tokens` as subscriber `[phone] subscriber`, default
`scout-owner`; SignalWire creates that subscriber the first time, and bills subscribers
monthly).

1. Open `https://<mac>.<tailnet>.ts.net/phone?token=<the contents of state/web_token>`
   (HTTPS, so the page may use the microphone).
2. **Pair this browser** with a name. The secret stays in this browser's storage;
   `scoutctl.sh phone forget <device>` revokes it.
3. **Call Scout**. The page loads `@signalwire/js` 4.0.0-rc.4 from jsDelivr and dials
   `address` with signed `userVariables`, as the app will.

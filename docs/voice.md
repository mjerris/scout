# Voice: listening, talking and the room

How Scout hears you and talks back: the audio engine, the wake word and follow-ups, the
local gate in front of Claude, and the knobs that tune them. The code is
`src/scout/assistant.py`, the audio backends (`audio_webrtc.py`, `audio_apple.py`,
`audio_plain.py`), `addressed.py` (follow-ups) and `tts.py`.

```
mic ─┐                                                    ┌→ Silero VAD + smart-turn → MLX Whisper → gate → "hey Claude …" → Claude
     ├─ voice layer: one duplex engine, echo cancelled ───┤
spk ←┘   (WebRTC AEC3, or Apple voice processing)         └← Kokoro TTS ← markdown → speech ←────────────────────────────────┘
```

## The voice layer

- **Voice layer:** one audio engine owns the mic and the speakers, so the
  echo canceller knows exactly what was played and removes the assistant's
  own voice before Whisper hears it. `audio.backend`: `auto` (= `webrtc`,
  WebRTC AEC3 + noise suppression; cross-platform), `apple` (macOS voice
  processing in a small Swift helper, `scripts/build-voiceio.sh`), or `plain`
  (no canceller; the app then strips its own words from transcripts
  instead). Measured on the Mac mini with `python -m scout.measure`:
  WebRTC removed 27.7 dB of the assistant's voice (Whisper could recover 2%
  of its words, vs 97% with no canceller); Apple 15 dB / 6%, with ~100 ms
  more latency, so WebRTC is the default.
- **Echo path, per speaker and mic:** AEC3 finds an echo only up to ~500 ms
  behind what was played; a TV over HDMI is further behind than that. The webrtc
  backend measures each output and input pair's echo delay and level from
  Scout's own speech (cross-correlating what it played with the raw mic), or
  with a short quiet sweep when the room is quiet (`audio.calibrate_chirp`), and
  holds the canceller's reference back when the path needs it. Results are kept
  in `state/audio_devices.json`, used at startup, and re-checked as it talks: a
  drift past 50 ms, or echo left over, measures again. Headphones are recorded
  as having no echo path. `voice_status` shows the pair's entry (`echo_path`);
  `scoutctl.sh echo show|reset` lists or clears them. `audio.echo_calibration`
  turns it off; `audio.output_delay_ms` is only the first guess for a new pair.
- **Barge-in:** with a canceller, talking over the assistant stops it. Over
  the answer to your request, what you say becomes the follow-up (held to the
  follow-up rules below); over a desk session's `discuss` message, it becomes
  the session's answer; over an announcement (the pulse, a timer, a Slack
  alert) it only stops it. `audio.barge_in`.
- **End of turn:** Silero VAD finds speech; after a short pause the
  smart-turn model judges from how you sounded whether you're finished. A
  finished sentence ends the turn ~0.2 s after you stop; "…, um" waits up to
  `audio.max_pause_ms` (1.5 s). `audio.turn_detection = "simple"` goes back
  to a fixed silence (`audio.silence_ms`).

## Talking to Scout

- **Wake word:** say "Hey Scout, …" (or just "Scout, …") with the request in
  the same breath, or say "Hey Scout", wait for the chime, then speak.
- **Follow-ups without the wake word:** for 8 seconds (`wake.follow_up_seconds`)
  after Scout answered a request you made in the room, a follow-up needs no
  wake word. Only after such an answer: announcements (the pulse, the briefing,
  Slack alerts, timers), a desk session's message that wants no reply, a reply
  that only went to your phone, and startup open no window. And no chains: the
  answer to a follow-up asked without the wake word opens none, so the next
  request takes "Hey Scout" again. With `wake.follow_up = "strict"` (the
  default) the follow-up must also look addressed to Scout (`addressed.py`,
  plain code): a question, a command, a short fragment of the exchange ("and
  Saturday?", "the second one"), a request ("I meant oat milk"), an answer to
  a question Scout just asked, or anything naming Scout. Statements ("We
  shipped that last week"), unfinished speech ("It's asking me my..."), talk
  to someone else ("…, by the way, Brian", "You might have to ask Brian",
  "Can you hear me?", "everyone") and acknowledgements ("Oh, I know") are
  ignored, logged as `ignored request (<why>)`. `"open"` takes any speech that
  passes the gate; `"off"` always needs the wake word. A request a pause split
  in two ("what time do those winds…" / "…forecast to start") is asked whole
  when the second part starts within 2.5 s of the first and itself passes the
  follow-up checks; a request joins one such part only.

  Measured on synthetic room conversation and real follow-ups
  (`tests/test_addressed.py`): on the set the rules were written from, 40 of
  40 follow-ups taken and 0 of 35 conversation lines; on a second set, tuned
  on after one measurement (precision 0.69, recall 0.96 before), 25 of 25 and
  7 of 25 (precision 0.78); on a fresh set written after that, 25 of 25 and 9
  of 25 (precision 0.74; 0.71 at its first measurement). What still gets
  through is conversation that reads like a request: questions and commands
  to the people on a call ("Next slide please", "Does that make sense?").
  That is why the window opens only after an answer to you and never chains;
  for a call in the room, `"off"`.
- **Stop:** "Scout, stop" (also "Stop, Claude", "stop, stop", "that's enough")
  interrupts speech and the running request, cancels a pending question, ends a
  session's listen or floor hold, and drops anything queued. "Claude, new
  conversation" starts a fresh session.
- **Busy:** a "Hey Scout …" request while one is running is queued ("Okay, I'll
  do that next.") and runs when the current one ends.
- **Local gate:** every transcript passes deterministic checks before it can
  reach Claude, so noise and hallucinations cost no tokens. The checks use
  Whisper's confidence and no-speech estimate, repetition, words per second
  of actual speech, a list of known noise phrases, and loudness. Follow-ups
  without the wake word are held to a stricter standard: at least two words,
  well above the room's noise floor, and not much quieter than your last
  "Hey Scout". Rejections are logged as `ignored ... (reason)` and shown
  struck through on the web page. Thresholds are under `[gate]` in the config.

## While Scout works

- **"Hang on":** end with "hang on", "give me a sec" or ", wait" and it keeps
  listening (20 s) and joins what you say next onto the request ("tell them not
  to wait" is an ordinary request). Saying "never mind" or "stop" instead
  cancels it.
- **Working sounds:** a soft tick every few seconds while tools run and nothing
  is being said, so a quiet stretch doesn't sound like a hang. When a lookup
  (Claude's or a local one like the weather or a route) has said nothing for
  2 s, it says what it's doing ("Checking your mail.", "Getting the weather.",
  "One moment."), and "Still working on it." 12 s later if it's still going.
  Turn it off with `tts.progress_speech = false`; the timing is
  `tts.progress_delay_s` and `tts.progress_again_s` (0 = no second line).
- **Pronunciation:** regex rules fix how words are spoken ("config.toml" →
  "config dot toml") and common mishearings ("Claud" → "Claude"). Built-in
  defaults are in `src/scout/pronounce_default.txt`; add your own in
  `pronounce.txt` in Scout's data folder (`TTS|STT  pattern  replacement`), read
  again whenever it changes. Or just tell Scout: "you're saying Andre wrong, it's
  AHN-dray" adds the rule for you (the `pronounce_set` tool).
  Ticket, issue, PR, case, order and similar numbers, and bare integers of five or
  more digits, are said digit by digit ("ticket 61723" → "ticket 6 1 7 2 3", "#117" →
  "number 1 1 7"); money, quantities ("61,000", "12000 steps"), years, times and short
  numbers ("PR 6", "Highway 61") stay numbers (`speech.speak_ids`). On the phone the
  agent is told the same, and answers Scout pushes into a call are put that way.

## Tuning

`uv run scout --list-devices` lists audio devices for `[audio]
input_device` and `output_device`. Other useful knobs in `config.toml`:

| Key | Effect |
|---|---|
| `claude.model = "sonnet"` | faster, cheaper replies |
| `claude.cwd` | the directory the agent works in |
| `asr.model = "mlx-community/whisper-small-mlx"` | less RAM and faster, slightly less accurate |
| `tts.voice` | e.g. `af_bella`, `am_michael`, `bf_emma`, `bm_george` |
| `audio.silence_ms` | how long a pause ends your sentence |
| `wake.follow_up = "off"` | always require the wake word (`"open"`: any follow-up that passes the gate) |

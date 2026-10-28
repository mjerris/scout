# Mac controls, weather, maps, Starlink and the offline reference

Built-in tools that answer or act without any account: most are answered on the Mac
without Claude.

## Mac controls and timers

Search Netflix, YouTube, Google, Amazon, Wikipedia or
Maps; open an app; see which app is in front and which are open; list and
switch Chrome tabs; full screen on/off; play/pause/next/previous (Spotify or
Music, or the video in the front browser); volume get/set/up/down/mute;
timers that announce themselves; web search. These never ask. Opening a URL
or a new tab asks once per site. Each tool's AppleScript is fixed in
`mac.py`, and your words are passed in only as checked arguments, so these
can't be used to run arbitrary scripts. Timers live in memory and are lost
on restart.

## Offline reference

Scout keeps Wikipedia, WikiMed, iFixit and other Kiwix archives on the Mac and searches
them in place through Kiwix's kiwix-serve, which it runs on 127.0.0.1. Claude gets
`reference_search` and `reference_read`; with the internet down Scout answers "look up
X" from them itself, with a link to the article. Details: [reference.md](reference.md).

## Weather

A built-in `weather` tool (Open-Meteo: free, no API key, nothing to sign in to): current
conditions, 24 hourly steps and 7 days, with wind speed, gusts and direction. Everyday
questions ("what's the weather in Luther, Michigan", "... tonight / tomorrow / in the
morning", "what's the wind forecast for tonight", "will it rain tomorrow") are answered
locally without Claude. `[weather] home` sets the place used when none is named.

## Maps

Nearby places and driving distances from OpenStreetMap (free, no key, no sign-in):
"where's the nearest dump station near Cadillac", "how far is Traverse City from
Luther" are answered locally; Claude gets `places_near` and `drive_route`. Searches are
cached for a day; the public map server is sometimes busy, and Scout says so. Drive
times are for a car: they don't know an RV's height or weight.

## Starlink

Off unless turned on; it reads the dish's own local API (no account, no sign-in):

```toml
[starlink]
enabled = true
quiet_hours = "22:00-08:00"   # announcements are only logged in these hours
```

"Is Starlink down?", "why is the internet so slow?" and "how much data have we used
today?" are answered locally. Scout announces outages longer than a minute, when it's
back, and new dish alerts; it tallies data used per day. Claude and other sessions get
the read-only `starlink` tool with exact values.

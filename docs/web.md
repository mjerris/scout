# The web page and your other devices

Scout serves a web page on the Mac: a live transcript, typed requests, approvals and
push-to-talk from a phone or laptop over Tailscale.

## The web page

Open `http://<mini-ip>:8765/?token=<token>` (the token is in
`state/web_token`; the log doesn't print it). The page drops the token from
the address bar and keeps a cookie. It shows a live transcript and lets you
type requests, approve or deny actions, stop, mute the mic and reset the
session. Delete `state/web_token` and restart the app to rotate the token.
Over the LAN address the page is plain HTTP; use the Tailscale HTTPS address
(below) on networks you don't trust, or drop "lan" from `web.hosts`.

## Phone and other devices (Tailscale)

```sh
scripts/tailscale.sh enable     # https://<this-mac>.<tailnet>.ts.net, tailnet-only
scripts/tailscale.sh disable | status
```

Over HTTPS the web page can use the device's microphone: **Hold to talk**
sends a clip through the same gate (no wake word needed), and **play here**
plays replies on that device. Install Tailscale on your phone, sign in to the
same tailnet, and open the printed link. The first time, Tailscale asks you
to enable Serve for the tailnet.

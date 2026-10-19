# Scout phone app (iOS)

React Native app for talking to Scout by phone (docs/phone-design.md, phase 5), built on
`@signalwire/react-native` (github.com/lpradovera/signalwire-react-native-unofficial).

**Why Expo (prebuild) and not bare React Native:** the library is built and verified on
Expo SDK 54 with a generated native project, and every native piece it needs (WebRTC,
CallKit through SignalWire's react-native-callkeep fork, InCallManager, the library's own
iOS code) autolinks as an ordinary module. The app runs on React Native's New
Architecture, which the library requires.
With continuous native generation the iOS project is not committed: `app.config.js` and
the plugins in `plugins/` say everything that differs from the template, so upgrading
React Native is a regeneration rather than a merge. No Expo services are used.

**The library is vendored** (`vendor/`), because it isn't published to npm yet: the two
tarballs `npm pack` makes of `@signalwire/react` and `@signalwire/react-native` at
`vendor/UPSTREAM_COMMIT` (the `combined/all` branch of SignalWire's fork, PR #54 on
lpradovera/signalwire-react-native-unofficial). A tarball is what npm will publish: the
built JavaScript, the iOS and Android code, the podspec and the Expo config plugin, which
TypeScript sources alone no longer cover. npm installs it as an ordinary copy, so Metro
needs no symlink settings, and moving to the npm package later is a one-line change.
`scripts/vendor-signalwire.sh <checkout> [commit]` builds and packs a clean checkout;
`--check <checkout>` rebuilds `UPSTREAM_COMMIT` and compares (the build is reproducible).
callkeep is SignalWire's fork, `@signalwire/react-native-callkeep` 4.4.1, from GitHub at a
fixed commit until it is on npm.

## Run

    cd app && npm ci
    npm run check                       # lint, typecheck, unit tests (also the APP gate in scripts/run-ci.sh)
    bash scripts/build-sim.sh           # Release build for the Simulator; prints the .app path
    xcrun simctl install booted <that .app> && xcrun simctl launch booted com.example.scoutphone

For a Debug build with live reload: `npx expo run:ios` (starts Metro).

For an iPhone: `SCOUT_IOS_BUNDLE_ID=<an id your team owns> SCOUT_IOS_TEAM_ID=<your Apple team id> npx expo prebuild --platform ios --clean`,
then `cd ios && pod install`, open `ios/Scout.xcworkspace` and run on the device. With
`SCOUT_IOS_TEAM_ID` set, the regenerated project keeps automatic signing with your team
(neither id is committed).

## Pairing

The app never needs Scout's web token. Three steps:

1. Turn on Tailscale on the iPhone.
2. On the Mac, `scripts/scoutctl.sh phone pair` (or **Pair the phone app** on Scout's
   `/phone` page) shows a QR code: point the iPhone Camera at it and tap the Scout banner.
   It opens `scout://pair?server=<Scout's https address>&code=<one-time code>` here.
   At home the app can also find Scout on the network (Bonjour, `_scout._tcp`): tap
   **Pair** next to "Found <your Mac>": from your own Tailscale login it pairs at once;
   otherwise say yes when Scout asks out loud (the phone and Scout show the same two-digit
   number). `[phone] pair_mode` in Scout's config can require the yes, or codes only. **No code? Ask Scout to approve** does the same
   with a typed address.
3. Tap **Pair** ("Pair with <host>?").

The manual form takes Scout's address (`https://your-mac.your-tailnet.ts.net`) and the
code, or the whole pasted link. The pairing (device id, secret, agent address) stays in
the Keychain; token requests are signed with the device secret. Errors say what to do:
Scout unreachable (turn Tailscale on), code expired or already used (make a new one).
Finding Scout uses react-native-zeroconf 0.14.0 (MIT, a small Objective-C module over
NSNetServiceBrowser) and the Local Network permission (asked the first time); a build
without the module just finds nothing.

## Push (Scout calls you; pairing requests)

Push needs an Apple Developer Program team with Push Notifications on the app's App ID (a
free personal team can't sign `aps-environment`; build for one with `SCOUT_IOS_PUSH=0`).
What Scout does with it is in docs/phone-design.md, "Scout calls you". Once, in the Apple
Developer portal for the team:

1. **Identifiers → +, App IDs, App**: Bundle ID (explicit) = the id you build with
   (`SCOUT_IOS_BUNDLE_ID`). Under Capabilities tick **Push Notifications**. VoIP needs
   nothing more there: PushKit uses the same capability, with the `voip` background mode
   the app declares.
2. **Keys → +**: tick **Apple Push Notifications service (APNs)** (environment Sandbox &
   Production, key restriction Team Scoped), register, and download the `.p8` (once only).
   Note its **Key ID**. A key the team already has for APNs works too; one key serves
   every app of the team, alert and VoIP pushes, sandbox and production.
3. Put the key on the Mac, readable by you only, and tell Scout (its config is mode 600):

       mkdir -p ~/Library/Application\ Support/Scout/apns
       mv ~/Downloads/AuthKey_<KEYID>.p8 ~/Library/Application\ Support/Scout/apns/
       chmod 600 ~/Library/Application\ Support/Scout/apns/AuthKey_<KEYID>.p8

       [phone]
       apns_team_id = "<team id>"
       apns_key_id = "<key id>"
       apns_key_file = "apns/AuthKey_<KEYID>.p8"
       apns_topic = "<bundle id>"          # alert pushes; VoIP pushes go to "<bundle id>.voip"
       ring_for = ["slack", "pulse"]       # which alerts ring the phone; [] (the default): never

   Scout refuses a key file others can read, and never logs or sends the key.
4. Build for the device with that team: `SCOUT_IOS_BUNDLE_ID=<bundle id>
   SCOUT_IOS_TEAM_ID=<team id> npx expo prebuild --platform ios --clean`, then
   `cd ios && pod install`, and run it from Xcode. `SCOUT_IOS_APS_ENV=production` for a
   TestFlight or Ad Hoc build (the app tells Scout which environment its token is for).
5. Open the app once (it asks to send notifications): it registers its tokens with Scout
   (`scoutctl phone list` shows `push: alert, voip`). `scoutctl phone ring "Testing"` rings
   it; `scoutctl phone rings` shows how each ring went.

## Patches (patch-package, applied on install)

- `patches/@signalwire+js+4.0.0-rc.5.patch`: our SDK fixes that are open upstream
  (signalwire/signalwire-typescript-web #142 a dial that times out after its invite went out
  is hung up; #143 hold stops local media, with `hold()`, `unhold()` and `held$`; #144 no
  ICE-restart recovery on a held call; #145 a call whose first ICE gathering finds no
  candidates fails; #146 a session whose token expired connects fresh instead of resuming),
  built from rc.5 with the five PRs merged (they merge cleanly; a plain rc.5 build is
  byte-identical to npm's dist, so the patch is exactly the PRs). It replaces the library's
  own hold patch, which #143 and #144 cover, plus a small shim (`_holdState`,
  `_pauseLocalMediaForHold`) the library's hold code reads. Drop it once rc.6 has them.
- `patches/react-native-incall-manager+4.3.0.patch`: iOS `chooseAudioRoute`,
  `getAudioDeviceStatus` and `onAudioDeviceChanged` (proposed upstream), the library's
  `docs/patches` copy. The library's route control and `useAudioDevices` use them.

The four patches the app used to carry on the library (voice calls start on the receiver,
"Scout" in CallKit and Recents, Bluetooth and earpiece routes, the audio-session timer)
are all in the library now.

Text fields are uncontrolled (`defaultValue`): on the New Architecture a controlled field
can drop keys typed while its JS echo is applied (react-native#58904), and building React
Native from source for the fix isn't worth it for three pairing fields nothing rewrites.

## Simulator

Everything up to the call runs against a stand-in for Scout (the iOS 27 Simulator also
runs a CallKit call, given a stand-in that mints a real SignalWire token, and the Mac's
microphone):

    node scripts/mock-scout.mjs          # prints a scout://pair link with a one-time code
    xcrun simctl launch booted com.example.scoutphone -ScoutTestLink '<that link>' -ScoutTestPair 1
    xcrun simctl launch booted com.example.scoutphone -ScoutTestCall 1   # stops at Scout's token (409)

The Simulator gets no real push, but takes a notification by hand. With
`node scripts/mock-scout.mjs --ask` (a made-up device asks to pair after 5 s, and the app
asks to send notifications: Allow), this shows the pairing notification with Approve and
Deny (long-press it), as Scout sends it:

    xcrun simctl push booted com.example.scoutphone scripts/pair-push.apns

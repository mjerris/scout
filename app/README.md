# Scout phone app (iOS)

React Native app for talking to Scout by phone (docs/phone-design.md, phase 5), built on
`@signalwire/react-native` (github.com/lpradovera/signalwire-react-native-unofficial).

**Why Expo (prebuild) and not bare React Native:** the library is built and verified on
Expo SDK 54 with a generated native project, and every native piece it needs (WebRTC,
CallKit through react-native-callkeep, InCallManager) autolinks as an ordinary module.
With continuous native generation the iOS project is not committed: `app.config.js` and
the plugins in `plugins/` say everything that differs from the template, so upgrading
React Native is a regeneration rather than a merge. No Expo services are used.

**The library is vendored** (`vendor/`, TypeScript sources at `vendor/UPSTREAM_COMMIT`),
because it isn't published to npm. `scripts/vendor-signalwire.sh <checkout>` refreshes
it and re-applies our patches in `vendor/patches/` (each one is also an upstream PR).

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
   **Pair** next to "Found <your Mac>", and say yes when Scout asks out loud (the phone and
   Scout show the same two-digit number). **No code? Ask Scout to approve** does the same
   with a typed address.
3. Tap **Pair** ("Pair with <host>?").

The manual form takes Scout's address (`https://your-mac.your-tailnet.ts.net`) and the
code, or the whole pasted link. The pairing (device id, secret, agent address) stays in
the Keychain; token requests are signed with the device secret. Errors say what to do:
Scout unreachable (turn Tailscale on), code expired or already used (make a new one).
Finding Scout uses react-native-zeroconf 0.14.0 (MIT, a small Objective-C module over
NSNetServiceBrowser) and the Local Network permission (asked the first time); a build
without the module just finds nothing.

## Patches (each drafted as an upstream PR)

- `vendor/patches/01-…`: a voice call starts in voice mode (receiver, or a connected
  Bluetooth headset or car), not on the loudspeaker; the mode follows the call's own media
  instead of the app's `supportsVideo`.
- `vendor/patches/02-…`: the CallKit observer takes a display name, so the call screen and
  Recents say "Scout", not the agent's address.
- `vendor/patches/03-…`: Bluetooth and earpiece routes on iOS (route failures were
  unhandled rejections; "Phone" couldn't take a call off Bluetooth).
- `vendor/patches/04-…`: the audio-session fallback timer is cleared once CallKit
  activates the session.
- `patches/react-native-incall-manager+4.2.2.patch` (patch-package, applied on install):
  iOS `chooseAudioRoute`, `getAudioDeviceStatus` and the `onAudioDeviceChanged` event,
  which until now were Android-only.

## Simulator

The Simulator can't place a call (no SignalWire config, and CallKit doesn't run calls
there), but everything up to it runs against a stand-in for Scout:

    node scripts/mock-scout.mjs          # prints a scout://pair link with a one-time code
    xcrun simctl launch booted com.example.scoutphone -ScoutTestLink '<that link>' -ScoutTestPair 1
    xcrun simctl launch booted com.example.scoutphone -ScoutTestCall 1   # stops at Scout's token (409)

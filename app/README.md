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

For an iPhone: `SCOUT_IOS_BUNDLE_ID=<an id your team owns> npx expo prebuild --platform ios --clean`,
open `ios/Scout.xcworkspace`, pick your signing team, and run on the device.

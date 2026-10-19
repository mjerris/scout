// Expo config (CNG: `npx expo prebuild` generates ios/, which is not committed).
//
// The bundle id must be one your Apple team owns, so it is not in this public repo: set
// SCOUT_IOS_BUNDLE_ID for a device build (the placeholder is fine for the Simulator), and
// SCOUT_IOS_TEAM_ID (your Apple team id) so a `prebuild --clean` keeps automatic signing.
//
// Push (Scout ringing the phone, and pairing requests with Approve / Deny) needs a team in
// the Apple Developer Program with Push Notifications on that App ID (app/README.md,
// "Push"). It is on unless SCOUT_IOS_PUSH=0, which builds for a team without it (a free
// personal team can't sign the aps-environment entitlement). SCOUT_IOS_APS_ENV is the
// APNs environment the build is signed for: "development" (the default: Xcode and
// development profiles) or "production" (TestFlight, App Store, Ad Hoc).
// Nothing about the SignalWire space or project is here either: the app gets everything
// it needs from Scout at pairing and at each call (docs/phone-design.md).
const PLACEHOLDER_BUNDLE_ID = 'com.example.scoutphone';
const PUSH = process.env.SCOUT_IOS_PUSH !== '0';
const APS_ENV = process.env.SCOUT_IOS_APS_ENV === 'production' ? 'production' : 'development';

module.exports = () => ({
  name: 'Scout',
  slug: 'scout-phone',
  version: '0.1.0',
  orientation: 'portrait',
  scheme: 'scout',
  userInterfaceStyle: 'automatic',
  // The SignalWire library supports the New Architecture only; callkeep, incall-manager
  // and zeroconf run through React Native's interop layer for legacy modules.
  newArchEnabled: true,
  ios: {
    bundleIdentifier: process.env.SCOUT_IOS_BUNDLE_ID || PLACEHOLDER_BUNDLE_ID,
    ...(process.env.SCOUT_IOS_TEAM_ID ? { appleTeamId: process.env.SCOUT_IOS_TEAM_ID } : {}),
    supportsTablet: false,
    ...(PUSH ? { entitlements: { 'aps-environment': APS_ENV } } : {}),
    infoPlist: {
      NSMicrophoneUsageDescription: 'Scout uses the microphone for calls to Scout.',
      // audio: the call keeps going with the screen locked; voip: CallKit calls.
      UIBackgroundModes: ['audio', 'voip'],
      ITSAppUsesNonExemptEncryption: false,
      // Finding Scout on the home network for pairing (it advertises _scout._tcp).
      NSLocalNetworkUsageDescription: 'Scout looks for your Mac on this network so you can pair without typing.',
      NSBonjourServices: ['_scout._tcp'],
    },
  },
  plugins: [
    ['expo-secure-store', { faceIDPermission: false }],
    // CallKit (and the Recents call-back forwarder), and with push the PushKit hook that
    // reports a VoIP push to CallKit before any JavaScript runs. No Siri: Scout is called
    // from Recents or the app.
    ['@signalwire/react-native', { enableCallKit: true, enableVoipPush: PUSH, enableSiri: false }],
    // Alert pushes (pairing requests): the entitlement is set above, so the plugin only
    // registers the app; no sounds or icons of its own.
    ...(PUSH ? [['expo-notifications', { enableBackgroundRemoteNotifications: false }]] : []),
    './plugins/withPodBuildSettings',
    './plugins/withSceneLifecycle',
  ],
});

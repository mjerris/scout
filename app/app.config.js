// Expo config (CNG: `npx expo prebuild` generates ios/, which is not committed).
//
// The bundle id must be one your Apple team owns, so it is not in this public repo: set
// SCOUT_IOS_BUNDLE_ID for a device build (the placeholder is fine for the Simulator), and
// SCOUT_IOS_TEAM_ID (your Apple team id) so a `prebuild --clean` keeps automatic signing.
// Nothing about the SignalWire space or project is here either: the app gets everything
// it needs from Scout at pairing and at each call (docs/phone-design.md).
const PLACEHOLDER_BUNDLE_ID = 'com.example.scoutphone';

module.exports = () => ({
  name: 'Scout',
  slug: 'scout-phone',
  version: '0.1.0',
  orientation: 'portrait',
  scheme: 'scout',
  userInterfaceStyle: 'automatic',
  // react-native-webrtc 124, callkeep and incall-manager are old-architecture modules;
  // the library this app builds on is verified on the old architecture (Expo SDK 54).
  newArchEnabled: false,
  ios: {
    bundleIdentifier: process.env.SCOUT_IOS_BUNDLE_ID || PLACEHOLDER_BUNDLE_ID,
    ...(process.env.SCOUT_IOS_TEAM_ID ? { appleTeamId: process.env.SCOUT_IOS_TEAM_ID } : {}),
    supportsTablet: false,
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
  plugins: [['expo-secure-store', { faceIDPermission: false }], './plugins/withPodBuildSettings', './plugins/withSceneLifecycle'],
});

/* global jest */
// Native modules the app touches, for unit tests (jest-expo mocks Expo's own).
jest.mock('react-native-safe-area-context', () => require('react-native-safe-area-context/jest/mock').default);
jest.mock('@react-native-async-storage/async-storage', () =>
  require('@react-native-async-storage/async-storage/jest/async-storage-mock'),
);
jest.mock('@react-native-community/netinfo', () => require('@react-native-community/netinfo/jest/netinfo-mock.js'));
jest.mock('react-native-webrtc', () => ({
  RTCPeerConnection: jest.fn(),
  RTCSessionDescription: jest.fn(),
  RTCIceCandidate: jest.fn(),
  MediaStream: jest.fn(),
  MediaStreamTrack: jest.fn(),
  mediaDevices: { getUserMedia: jest.fn(), enumerateDevices: jest.fn(async () => []) },
  RTCView: 'RTCView',
}));
jest.mock('react-native-incall-manager', () => ({
  __esModule: true,
  default: {
    start: jest.fn(),
    stop: jest.fn(),
    setForceSpeakerphoneOn: jest.fn(),
    chooseAudioRoute: jest.fn(async () => ({})),
  },
}));
jest.mock('@signalwire/react-native-callkeep', () => {
  const listeners = new Map();
  const RNCallKeep = {
    setup: jest.fn(async () => true),
    setAvailable: jest.fn(),
    startCall: jest.fn(),
    endCall: jest.fn(),
    setMutedCall: jest.fn(),
    reportConnectedOutgoingCallWithUUID: jest.fn(),
    reportEndCallWithUUID: jest.fn(),
    setCurrentCallActive: jest.fn(),
    backToForeground: jest.fn(),
    updateDisplay: jest.fn(),
    clearInitialEvents: jest.fn(),
    getInitialEvents: jest.fn(async () => []),
    setOnHold: jest.fn(),
    displayIncomingCall: jest.fn(),
    answerIncomingCall: jest.fn(),
    rejectCall: jest.fn(),
    addEventListener: jest.fn((type, fn) => {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
      return { remove: () => listeners.get(type)?.delete(fn) };
    }),
    removeEventListener: jest.fn((type) => listeners.delete(type)),
    __emit: (type, data) => listeners.get(type)?.forEach((fn) => fn(data)),
  };
  return { __esModule: true, default: RNCallKeep, CONSTANTS: {
      END_CALL_REASONS: { FAILED: 1, REMOTE_ENDED: 2, UNANSWERED: 3, ANSWERED_ELSEWHERE: 4, DECLINED_ELSEWHERE: 5, MISSED: 6 },
    }, };
});
// What index.ts does first, so @signalwire/js can load.
require('@signalwire/react-native/polyfills');
// The wrapper checks that callkeep's and WebRTC's native modules are linked.
Object.assign(require('react-native').NativeModules, { RNCallKeep: { setup: jest.fn() }, WebRTCModule: {} });

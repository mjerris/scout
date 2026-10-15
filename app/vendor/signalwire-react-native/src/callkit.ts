/**
 * Native call UI: CallKit on iOS, ConnectionService on Android.
 *
 * Requires the optional peer `react-native-callkeep`.
 *
 * ```ts
 * // index.js — before React mounts, so a cold-start VoIP push finds it ready
 * import { getCallKit } from '@signalwire/react-native/callkit';
 * void getCallKit().setup({ appName: 'Demo' });
 *
 * // your VoIP push handler
 * getCallKit().reportIncomingPush({ callId, from, fromName });
 *
 * // register the device for VoIP push
 * watchVoipToken((token) => void registerDevice(token));
 * ```
 */
export { CallKeepBridge, getCallKit, resetCallKitForTesting } from './callkit/CallKeepBridge';
export type { CallKitSetupOptions } from './callkit/CallKeepBridge';
export { CallRegistry } from './callkit/CallRegistry';
export type {
  CallEntry,
  CallEntryState,
  CallIntent,
  CallRegistryHost,
  CallRegistryOptions,
  PushPayload
} from './callkit/types';

export { getVoipToken, onVoipTokenChange, watchVoipToken } from './push/voipToken';
export { getFcmToken, watchFcmToken } from './push/fcmToken';
export { registerAndroidCallPush, decodePushData } from './push/androidCallPush';
export { useRingingPushes } from './callkit/useRingingPushes';
export type { RingingPush, RingingPushes } from './callkit/useRingingPushes';
export { watchPushToken } from './push/pushToken';

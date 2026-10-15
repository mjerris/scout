/**
 * Calls ringing on the native side, without pulling in the native call UI.
 *
 * A separate entry point on purpose. The `./callkit` barrel imports
 * `react-native-callkeep`, which constructs a NativeEventEmitter at import
 * time — so importing anything from it makes an optional peer mandatory, and
 * crashes an app that has no native call UI installed. A component library
 * that wants to *draw* ringing calls should not drag that in.
 */
export { useRingingPushes, ringingFrom } from './callkit/useRingingPushes';
export type { RingingPush, RingingPushes } from './callkit/useRingingPushes';

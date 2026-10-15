/**
 * React Native support for `@signalwire/js`.
 *
 * Import `@signalwire/react-native/polyfills` first, from your app entry file.
 *
 * The hooks live in `@signalwire/react` and are re-exported here, so React
 * Native apps install one package and browser apps install the other.
 */

// ---------------------------------------------------------------------------
// Re-exported from the universal core, so RN apps need only this package.
// ---------------------------------------------------------------------------
export { SignalWireContext, useSignalWire, useCall, useIncomingCalls, useDevices, useObservable } from '@signalwire/react';
export type {
  CallObserver,
  SignalWireContextValue,
  SignalWirePlatform,
  UseCallResult,
  UseDevicesResult,
  UseIncomingCallsResult,
  UseSignalWireResult
} from '@signalwire/react';

// ---------------------------------------------------------------------------
// React Native specific. The provider shadows the core one: same API, but it
// supplies the platform layer and the optional CallKit observer itself.
// ---------------------------------------------------------------------------
export { SignalWireProvider } from './react/SignalWireProvider';
export type { SignalWireProviderProps } from './react/SignalWireProvider';

// Platform
export { createReactNativePlatform } from './platform/createReactNativePlatform';
export type {
  ReactNativePlatform,
  ReactNativePlatformOptions
} from './platform/createReactNativePlatform';
export { createWebRTCApiProvider } from './platform/webrtc';
export { ReactNativeStorage } from './platform/storage';
export type { AsyncStorageLike, StorageScope } from './platform/storage';
export { installBaseGlobals } from './platform/baseGlobals';

// Components
export { SignalWireVideoView } from './components/SignalWireVideoView';
export type { SignalWireVideoViewProps } from './components/SignalWireVideoView';

// Errors
export { MissingPeerDependencyError, PolyfillNotInstalledError } from './errors';

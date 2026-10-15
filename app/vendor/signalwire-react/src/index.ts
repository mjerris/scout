/**
 * Universal React bindings for `@signalwire/js`.
 *
 * Runs unchanged in the browser and in React Native — this package contains no
 * platform-specific code. React Native users should install
 * `@signalwire/react-native`, which supplies the platform layer and re-exports
 * everything here.
 */

export { SignalWireProvider, SignalWireContext } from './SignalWireProvider';
export type {
  SignalWireContextValue,
  SignalWireProviderProps
} from './SignalWireProvider';

export { useSignalWire } from './useSignalWire';
export type { UseSignalWireResult } from './useSignalWire';
export { useCall } from './useCall';
export type { UseCallResult } from './useCall';
export { useIncomingCalls } from './useIncomingCalls';
export type { UseIncomingCallsResult } from './useIncomingCalls';
export { useDevices } from './useDevices';
export type { UseDevicesResult } from './useDevices';
export { useObservable } from './useObservable';

export type { CallObserver, SignalWirePlatform } from './types';

export { logger } from './logger';

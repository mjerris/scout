/**
 * Audio routing for React Native.
 *
 * Requires the optional peer `react-native-incall-manager`.
 */
export {
  AudioRouteController,
  getAudioRouteController,
  resetAudioRouteControllerForTesting
} from './audio/AudioRouteController';
export type { AudioRoute } from './audio/AudioRouteController';
export { useAudioRoute } from './audio/useAudioRoute';
export type { UseAudioRouteResult } from './audio/useAudioRoute';

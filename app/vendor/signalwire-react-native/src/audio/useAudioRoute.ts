import { useCallback, useMemo } from 'react';

import { useObservable } from '@signalwire/react';
import { getAudioRouteController } from './AudioRouteController';

import type { AudioRoute } from './AudioRouteController';

export interface UseAudioRouteResult {
  readonly route: AudioRoute;
  setRoute(route: AudioRoute): void;
}

/** Current audio output route and a setter. */
export function useAudioRoute(): UseAudioRouteResult {
  const controller = useMemo(() => getAudioRouteController(), []);
  const route = useObservable(controller.route$, controller.route);

  const setRoute = useCallback((next: AudioRoute): void => controller.setRoute(next), [controller]);

  return { route, setRoute };
}

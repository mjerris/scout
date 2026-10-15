import InCallManager from 'react-native-incall-manager';
import { BehaviorSubject } from 'rxjs';

import { logger } from '@signalwire/react';
import { assertPeerModule } from '../platform/peers';

import type { Observable } from 'rxjs';

export type AudioRoute = 'earpiece' | 'speaker' | 'bluetooth';

/**
 * Owns the native audio session and output routing.
 *
 * `'bluetooth'` is a *request*: the OS makes the final routing decision and
 * InCallManager cannot reliably report the active device, so `route` reflects
 * what was asked for, not necessarily what the OS settled on.
 */
export class AudioRouteController {
  private readonly _route$ = new BehaviorSubject<AudioRoute>('earpiece');
  private started = false;
  private destroyed = false;

  constructor() {
    assertPeerModule(
      InCallManager as object | undefined,
      'react-native-incall-manager',
      'audio routing',
      'start'
    );
  }

  get route$(): Observable<AudioRoute> {
    return this._route$.asObservable();
  }

  get route(): AudioRoute {
    return this._route$.value;
  }

  /** Begins the native audio session. Video calls default to speaker. */
  start(media: 'audio' | 'video'): void {
    // Idempotent: an answered inbound call starts audio on the native answer
    // and again when it reaches `connected`. Restarting would re-run
    // InCallManager and force the route back to its default, discarding a
    // speaker toggle the user had already made.
    if (this.started) {
      return;
    }
    try {
      InCallManager.start({ media });
      this.started = true;
      logger.debug(`Audio session started (${media})`);
    } catch (error) {
      logger.warn('Failed to start the audio session:', error);
    }
    this.setRoute(media === 'video' ? 'speaker' : 'earpiece', true);
  }

  /** Ends the native audio session and resets to the earpiece. */
  stop(): void {
    try {
      if (this.started) {
        InCallManager.stop();
      }
    } catch (error) {
      logger.warn('Failed to stop the audio session:', error);
    }
    this.started = false;
    this.applyRoute('earpiece');
    if (!this.destroyed) {
      this._route$.next('earpiece');
    }
  }

  /**
   * Requests an output route.
   *
   * @param force - Internal. Re-applies even when the route is unchanged, so
   *   `start()` can push the initial route through to the native layer.
   */
  setRoute(route: AudioRoute, force = false): void {
    if (!force && route === this._route$.value) {
      return;
    }
    this.applyRoute(route);
    this._route$.next(route);
  }

  private applyRoute(route: AudioRoute): void {
    try {
      if (route === 'bluetooth') {
        void InCallManager.chooseAudioRoute('BLUETOOTH');
        return;
      }
      InCallManager.setForceSpeakerphoneOn(route === 'speaker');
    } catch (error) {
      logger.warn(`Failed to apply audio route "${route}":`, error);
    }
  }

  /**
   * Releases the audio session. Idempotent, and deliberately silent: emitting
   * a route change while tearing down would push a value at subscribers that
   * are themselves unmounting.
   */
  destroy(): void {
    if (this.destroyed) {
      return;
    }
    this.destroyed = true;
    this.stop();
    this._route$.complete();
  }
}

/** Same duplication hazard as the CallKit bridge — see its note. One audio
 * session exists per app, so the controller must be per-process, and only
 * `globalThis` is shared across the subpath entry bundles. */
const AUDIO_GLOBAL = '__signalwireRNAudioRoute';

type AudioHolder = { [AUDIO_GLOBAL]?: AudioRouteController };

/** The process-wide audio route controller. One audio session exists per app. */
export function getAudioRouteController(): AudioRouteController {
  const holder = globalThis as AudioHolder;
  holder[AUDIO_GLOBAL] ??= new AudioRouteController();
  return holder[AUDIO_GLOBAL];
}

/** Test seam — drops the singleton. */
export function resetAudioRouteControllerForTesting(): void {
  const holder = globalThis as AudioHolder;
  holder[AUDIO_GLOBAL]?.destroy();
  delete holder[AUDIO_GLOBAL];
}

/**
 * Minimal DOM-event globals the SDK touches at import time.
 *
 * `@signalwire/js`'s entry point runs, unconditionally, at module load:
 *
 * ```js
 * if (typeof window !== 'undefined') {
 *   window.dispatchEvent(new CustomEvent('signalwire:js:ready', ...));
 * }
 * ```
 *
 * React Native defines `window` (it aliases `global`) but provides neither
 * `CustomEvent` nor `window.dispatchEvent`, so that guard passes and the import
 * throws. This installs just enough for the SDK to load.
 *
 * Everything here is additive and idempotent: existing implementations win, and
 * the richer listener tracking in `networkShim` / `visibilityShim` layers on top.
 */

interface MinimalEvent {
  type: string;
  detail?: unknown;
}

type Listener = (event: MinimalEvent) => void;

interface EventTargetish {
  addEventListener?: (type: string, listener: Listener) => void;
  removeEventListener?: (type: string, listener: Listener) => void;
  dispatchEvent?: (event: MinimalEvent) => boolean;
}

function installEventTarget(target: EventTargetish): void {
  const listeners = new Map<string, Set<Listener>>();

  target.addEventListener ??= (type: string, listener: Listener): void => {
    const set = listeners.get(type) ?? new Set<Listener>();
    set.add(listener);
    listeners.set(type, set);
  };

  target.removeEventListener ??= (type: string, listener: Listener): void => {
    listeners.get(type)?.delete(listener);
  };

  target.dispatchEvent ??= (event: MinimalEvent): boolean => {
    for (const listener of listeners.get(event.type) ?? []) {
      listener(event);
    }
    return true;
  };
}

/**
 * Installs `CustomEvent`, `window` and `document` event-target stubs when the
 * runtime lacks them. Safe to call more than once.
 */
export function installBaseGlobals(): void {
  const root = globalThis as unknown as {
    CustomEvent?: unknown;
    window?: EventTargetish;
    document?: EventTargetish;
  };

  if (typeof root.CustomEvent === 'undefined') {
    class CustomEventShim<T = unknown> implements MinimalEvent {
      readonly type: string;
      readonly detail: T | undefined;

      constructor(type: string, options?: { detail?: T }) {
        this.type = type;
        this.detail = options?.detail;
      }
    }
    root.CustomEvent = CustomEventShim;
  }

  root.window ??= {};
  installEventTarget(root.window);

  root.document ??= {};
  installEventTarget(root.document);
}

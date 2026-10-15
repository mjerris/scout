/**
 * Side-effect polyfills required before `@signalwire/js` loads.
 *
 * Import this as the very first line of your app entry file:
 *
 * ```ts
 * import '@signalwire/react-native/polyfills';
 * ```
 *
 * Order matters, in three ways:
 *
 * - `react-native-get-random-values` must install `crypto.getRandomValues`
 *   before the SDK's bundled `uuid` reads it.
 * - `react-native-url-polyfill` must replace `URL` before the SDK parses a dial
 *   destination with a custom scheme (`new URL('destination:' + dest)`).
 * - `installBaseGlobals()` must run before the SDK's entry point, which
 *   dispatches a `CustomEvent` on `window` at import time — React Native has
 *   `window` but neither `CustomEvent` nor `window.dispatchEvent`.
 *
 * `installIteratorHelpers()` has no ordering constraint — the SDK reaches those
 * methods lazily, not at import — but it belongs here so a consumer gets it
 * from the same single import.
 */
import 'react-native-get-random-values';
import 'react-native-url-polyfill/auto';

import { installBaseGlobals } from './platform/baseGlobals';
import { installIteratorHelpers } from './platform/iteratorHelpers';

installBaseGlobals();
installIteratorHelpers();

export { installBaseGlobals, installIteratorHelpers };

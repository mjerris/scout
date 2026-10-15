import { PolyfillNotInstalledError } from '../errors';

const POLYFILL_IMPORT = '@signalwire/react-native/polyfills';

function hasRandomValues(): boolean {
  const candidate = (globalThis as { crypto?: { getRandomValues?: unknown } }).crypto;
  return typeof candidate?.getRandomValues === 'function';
}

/**
 * The SDK parses dial destinations with `new URL('destination:' + dest)`
 * (`Call.ts:133`). React Native's built-in URL drops the scheme, so verify a
 * custom scheme survives a round trip.
 */
function parsesCustomScheme(): boolean {
  try {
    const url = new URL('destination:/public/room?channel=video');
    return url.protocol === 'destination:';
  } catch {
    return false;
  }
}

/** Throws when the required side-effect polyfills have not been imported. */
export function assertPolyfillsInstalled(): void {
  if (!hasRandomValues() || !parsesCustomScheme()) {
    throw new PolyfillNotInstalledError(POLYFILL_IMPORT);
  }
}

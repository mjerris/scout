// Finding Scout on the local network: Scout advertises _scout._tcp over Bonjour with its
// https address in the TXT record (`url`, and `host`, a name to show; never a token). The
// app still talks to that https address, over Tailscale. Browsing needs the
// react-native-zeroconf native module; in a build without it, nothing is found and the
// other ways to pair still work.
import { NativeModules } from 'react-native';

import { normalizeServer } from './server';

export interface Found {
  key: string;
  label: string;
  server: string;
}

interface Service {
  name?: unknown;
  host?: unknown;
  txt?: unknown;
}

/** What a resolved Bonjour service offers, or null if it has no https address. */
export function foundFromService(s: Service | null | undefined): Found | null {
  if (!s || typeof s.name !== 'string' || !s.txt || typeof s.txt !== 'object') return null;
  const txt = s.txt as Record<string, unknown>;
  const url = typeof txt.url === 'string' ? txt.url : '';
  if (!url.startsWith('https://')) return null;
  let server: string;
  try {
    server = normalizeServer(url);
  } catch {
    return null;
  }
  const label = typeof txt.host === 'string' && txt.host.trim() ? txt.host.trim() : new URL(server).host;
  return { key: s.name, label, server };
}

export type Browse = (onChange: (found: Found[]) => void) => () => void;

/** Browse for Scout; calls onChange with everything found so far. Returns stop. */
export const browseScout: Browse = (onChange) => {
  if (!NativeModules.RNZeroconf) return () => {};
  // Loaded only when the native module is in this build.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const Zeroconf = require('react-native-zeroconf').default as new () => {
    on(event: string, fn: (arg: unknown) => void): void;
    scan(type: string, protocol: string, domain: string): void;
    stop(): void;
    removeDeviceListeners(): void;
  };
  const zc = new Zeroconf();
  const found = new Map<string, Found>();
  zc.on('resolved', (s) => {
    const f = foundFromService(s as Service);
    if (f) {
      found.set(f.key, f);
      onChange([...found.values()]);
    }
  });
  zc.on('remove', (name) => {
    if (typeof name === 'string' && found.delete(name)) onChange([...found.values()]);
  });
  zc.on('error', () => {}); // e.g. Local Network access denied: the other ways still work
  try {
    zc.scan('scout', 'tcp', 'local.');
  } catch {
    return () => {};
  }
  return () => {
    try {
      zc.stop();
    } finally {
      zc.removeDeviceListeners();
    }
  };
};

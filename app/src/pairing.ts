// What pairing leaves on the phone, kept in the iOS Keychain (expo-secure-store), never
// in AsyncStorage: Scout's address and web token, and this phone's device id, secret and
// the agent address it dials (the secret signs every call). Readable only while the
// phone is unlocked, and only on this device (not in backups or iCloud Keychain).
import * as SecureStore from 'expo-secure-store';

import { normalizeServer } from './server';

export interface Pairing {
  server: string;
  accessToken: string;
  name: string;
  device: string;
  secret: string;
  address: string;
}

const KEY = 'scout.pairing';
const OPTIONS: SecureStore.SecureStoreOptions = {
  keychainService: 'scout.phone',
  keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
};

function valid(p: unknown): p is Pairing {
  if (!p || typeof p !== 'object') return false;
  const o = p as Record<string, unknown>;
  return ['server', 'accessToken', 'name', 'device', 'secret', 'address'].every(
    (k) => typeof o[k] === 'string' && (o[k] as string).length > 0,
  );
}

export async function loadPairing(): Promise<Pairing | null> {
  const text = await SecureStore.getItemAsync(KEY, OPTIONS);
  if (!text) return null;
  try {
    const p: unknown = JSON.parse(text);
    return valid(p) ? p : null;
  } catch {
    return null;
  }
}

export async function savePairing(p: Pairing): Promise<void> {
  await SecureStore.setItemAsync(KEY, JSON.stringify(p), OPTIONS);
}

/** Forget the pairing on this phone (Scout keeps the device until `scoutctl phone forget`). */
export async function clearPairing(): Promise<void> {
  await SecureStore.deleteItemAsync(KEY, OPTIONS);
}

export interface PairLink {
  server: string;
  accessToken: string;
}

/**
 * A pairing link, scout://pair?server=<Scout's address>&token=<web token>: what a QR code
 * shown by Scout would carry (the iPhone camera opens it in the app). Null if it isn't one.
 */
export function parsePairLink(link: string): PairLink | null {
  let url: URL;
  try {
    url = new URL(link);
  } catch {
    return null;
  }
  if (url.protocol !== 'scout:') return null;
  const where = (url.host || url.pathname.replace(/^\/+/, '')).toLowerCase();
  if (where !== 'pair') return null;
  const server = url.searchParams.get('server') ?? '';
  const accessToken = url.searchParams.get('token') ?? '';
  if (!server || !accessToken) return null;
  try {
    return { server: normalizeServer(server), accessToken };
  } catch {
    return null;
  }
}

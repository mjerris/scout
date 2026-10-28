// What pairing leaves on the phone, kept in the iOS Keychain (expo-secure-store), never
// in AsyncStorage: Scout's address, and this phone's device id, secret and the agent
// address it dials (the secret signs every call and every token request). A pairing made
// before one-time codes also holds Scout's web token (accessToken), which is still sent. Readable only while the
// phone is unlocked, and only on this device (not in backups or iCloud Keychain).
import * as SecureStore from 'expo-secure-store';

import { normalizeServer } from './server';
import { clearSiriKey } from './siri';

export interface Pairing {
  server: string;
  accessToken?: string;
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
  if (o.accessToken !== undefined && typeof o.accessToken !== 'string') return false;
  return ['server', 'name', 'device', 'secret', 'address'].every(
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
  await clearSiriKey(); // Siri's key goes with the pairing
}

export interface PairLink {
  server: string;
  code: string;
}

// Scout's code alphabet (phone_pairing.ALPHABET): no 0/O or 1/I/L.
const CODE_CHARS = /^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{8}$/;

/** A one-time pairing code, read loosely (abcd efgh, ABCD-EFGH) and shown as ABCD-EFGH. Null if it isn't one. */
export function parseCode(text: string): string | null {
  const raw = text.toUpperCase().replace(/[^A-Z0-9]/g, '');
  return CODE_CHARS.test(raw) ? `${raw.slice(0, 4)}-${raw.slice(4)}` : null;
}

/**
 * A pairing link, scout://pair?server=<Scout's https address>&code=<one-time code>: what
 * the QR code from `scoutctl phone pair` or the /phone page carries (the iPhone Camera
 * opens it in the app). Null if it isn't one.
 */
export function parsePairLink(link: string): PairLink | null {
  let url: URL;
  try {
    url = new URL(link.trim());
  } catch {
    return null;
  }
  if (url.protocol !== 'scout:') return null;
  const where = (url.host || url.pathname.replace(/^\/+/, '')).toLowerCase();
  if (where !== 'pair') return null;
  const server = url.searchParams.get('server') ?? '';
  const code = parseCode(url.searchParams.get('code') ?? '');
  if (!server || !code) return null;
  try {
    return { server: normalizeServer(server), code };
  } catch {
    return null;
  }
}

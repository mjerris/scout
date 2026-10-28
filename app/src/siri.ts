// Siri (docs/siri-design.md): "Hey Siri, ask Scout" and a few fixed questions, answered by
// Scout on the Mac. The intents are Swift (plugins/withSiriIntents.js) and run without the
// app's JavaScript, so they read this phone's second key straight from the Keychain. Unlike
// the pairing (readable only while the phone is unlocked), the second key stays readable
// after the first unlock, so Siri works with the phone locked (CarPlay, AirPods). Scout
// decides what it may do ([phone] locked_key: by default only ask).
import * as SecureStore from 'expo-secure-store';

import type { Pairing } from './pairing';
import type { ScoutServer } from './server';

// The Swift side reads exactly this item: service "scout.siri:no-auth" (expo-secure-store
// adds ":no-auth"), account "scout.siri.key".
export const SIRI_KEY = 'scout.siri.key';
export const SIRI_OPTIONS: SecureStore.SecureStoreOptions = {
  keychainService: 'scout.siri',
  keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
};

export interface SiriKey {
  server: string;
  device: string;
  secret: string;
  abilities: string[];
}

export async function loadSiriKey(): Promise<SiriKey | null> {
  const text = await SecureStore.getItemAsync(SIRI_KEY, SIRI_OPTIONS);
  if (!text) return null;
  try {
    const k = JSON.parse(text) as Partial<SiriKey>;
    if (typeof k.server !== 'string' || typeof k.device !== 'string' || typeof k.secret !== 'string') return null;
    return { server: k.server, device: k.device, secret: k.secret, abilities: Array.isArray(k.abilities) ? k.abilities : [] };
  } catch {
    return null;
  }
}

export async function clearSiriKey(): Promise<void> {
  await SecureStore.deleteItemAsync(SIRI_KEY, SIRI_OPTIONS);
}

/**
 * Give Siri a fresh key for this pairing, on every launch (so a change to what Scout lets
 * it do arrives too). With Scout unreachable, the key kept for this pairing stays. Returns
 * what the key may do.
 */
export async function setUpSiri(pairing: Pairing, server: ScoutServer): Promise<string[]> {
  let fresh: { secret: string; abilities: string[] };
  try {
    fresh = await server.lockedKey();
  } catch (e) {
    const kept = await loadSiriKey();
    if (kept && kept.server === pairing.server && kept.device === pairing.device) return kept.abilities;
    throw e;
  }
  const key: SiriKey = { server: pairing.server, device: pairing.device, ...fresh };
  await SecureStore.setItemAsync(SIRI_KEY, JSON.stringify(key), SIRI_OPTIONS);
  return fresh.abilities;
}

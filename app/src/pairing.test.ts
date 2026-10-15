import * as SecureStore from 'expo-secure-store';

import { clearPairing, loadPairing, parsePairLink, savePairing } from './pairing';

jest.mock('expo-secure-store', () => {
  const items = new Map<string, string>();
  return {
    WHEN_UNLOCKED_THIS_DEVICE_ONLY: 6,
    getItemAsync: jest.fn(async (k: string) => items.get(k) ?? null),
    setItemAsync: jest.fn(async (k: string, v: string) => void items.set(k, v)),
    deleteItemAsync: jest.fn(async (k: string) => void items.delete(k)),
  };
});

const P = {
  server: 'https://mac.ts.net',
  accessToken: 'tok',
  name: 'Mike iPhone',
  device: 'd1234567890abcdef',
  secret: 'c2VjcmV0',
  address: '/private/scout',
};

test('the pairing round-trips through the Keychain, device-only and unlocked-only', async () => {
  expect(await loadPairing()).toBeNull();
  await savePairing(P);
  expect(await loadPairing()).toEqual(P);
  const opts = (SecureStore.setItemAsync as jest.Mock).mock.calls[0][2];
  expect(opts.keychainAccessible).toBe(SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY);
  await clearPairing();
  expect(await loadPairing()).toBeNull();
});

test('a damaged or partial item reads as unpaired', async () => {
  await SecureStore.setItemAsync('scout.pairing', '{nope');
  expect(await loadPairing()).toBeNull();
  await SecureStore.setItemAsync('scout.pairing', JSON.stringify({ ...P, secret: '' }));
  expect(await loadPairing()).toBeNull();
});

test('pairing links', () => {
  expect(parsePairLink('scout://pair?server=mac.tail1.ts.net&token=abc')).toEqual({
    server: 'https://mac.tail1.ts.net',
    accessToken: 'abc',
  });
  expect(parsePairLink('scout://pair?server=http%3A%2F%2Fmac.tail1.ts.net&token=abc')).toBeNull();
  expect(parsePairLink('scout://pair?server=mac.tail1.ts.net')).toBeNull();
  expect(parsePairLink('scout://call')).toBeNull();
  expect(parsePairLink('https://pair?server=a&token=b')).toBeNull();
  expect(parsePairLink('not a link')).toBeNull();
});

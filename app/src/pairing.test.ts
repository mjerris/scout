import * as SecureStore from 'expo-secure-store';

import { clearPairing, loadPairing, parseCode, parsePairLink, savePairing } from './pairing';

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

test('pairing links carry the server and a one-time code, never the web token', () => {
  expect(parsePairLink('scout://pair?server=https%3A%2F%2Fmac.tail1.ts.net&code=ABCD-EFGH')).toEqual({
    server: 'https://mac.tail1.ts.net',
    code: 'ABCD-EFGH',
  });
  expect(parsePairLink('scout://pair?server=mac.tail1.ts.net&code=abcdefgh')).toEqual({
    server: 'https://mac.tail1.ts.net',
    code: 'ABCD-EFGH',
  });
  expect(parsePairLink(' scout://pair?server=mac.tail1.ts.net&code=ABCD-EFGH ')).not.toBeNull();
  expect(parsePairLink('scout://pair?server=mac.tail1.ts.net&token=abc')).toBeNull(); // the old kind
  expect(parsePairLink('scout://pair?server=http%3A%2F%2Fmac.tail1.ts.net&code=ABCD-EFGH')).toBeNull();
  expect(parsePairLink('scout://pair?server=mac.tail1.ts.net&code=short')).toBeNull();
  expect(parsePairLink('scout://pair?code=ABCD-EFGH')).toBeNull();
  expect(parsePairLink('scout://call')).toBeNull();
  expect(parsePairLink('https://pair?server=a&code=ABCD-EFGH')).toBeNull();
  expect(parsePairLink('not a link')).toBeNull();
});

test('codes are read loosely and shown as ABCD-EFGH', () => {
  expect(parseCode('abcd-efgh')).toBe('ABCD-EFGH');
  expect(parseCode(' ABCD EFGH ')).toBe('ABCD-EFGH');
  expect(parseCode('ABCDEFG')).toBeNull();
  expect(parseCode('ABCD-EFG0')).toBeNull(); // no 0, 1, I, L or O in a code
  expect(parseCode('')).toBeNull();
});

test('a pairing made before codes (with the web token) still loads', async () => {
  await savePairing({ ...P, accessToken: 'tok' });
  expect(await loadPairing()).toMatchObject({ device: P.device, accessToken: 'tok' });
  const { accessToken: _, ...noToken } = { ...P, accessToken: '' };
  await savePairing(noToken);
  expect(await loadPairing()).toEqual(noToken);
});

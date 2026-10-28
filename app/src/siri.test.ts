import * as SecureStore from 'expo-secure-store';

import { ScoutServer } from './server';
import { LOCKED_KEY_PURPOSE, askPurpose, sign } from './sign';
import { SIRI_KEY, loadSiriKey, setUpSiri } from './siri';

jest.mock('expo-secure-store', () => {
  const items = new Map<string, string>();
  return {
    AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY: 2,
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

function server(status: number, body: unknown, calls: { url: string; body: Record<string, unknown> }[] = []) {
  const impl = (async (url: string, init: RequestInit) => {
    calls.push({ url, body: JSON.parse(init.body as string) });
    return { ok: status < 400, status, json: async () => body } as Response;
  }) as unknown as typeof fetch;
  return new ScoutServer(P.server, P, impl);
}

test("Siri's key comes from Scout signed with the pairing key, and stays readable when locked", async () => {
  const calls: { url: string; body: Record<string, unknown> }[] = [];
  expect(await setUpSiri(P, server(200, { status: 'ok', secret: 'bG9ja2Vk', abilities: ['ask'] }, calls))).toEqual(['ask']);
  expect(calls[0]!.url).toBe('https://mac.ts.net/api/phone/key');
  const b = calls[0]!.body;
  expect(b.sig).toBe(sign(P.secret, P.device, b.ts as number, b.nonce as string, LOCKED_KEY_PURPOSE));
  expect(await loadSiriKey()).toEqual({ server: P.server, device: P.device, secret: 'bG9ja2Vk', abilities: ['ask'] });
  const [key, , opts] = (SecureStore.setItemAsync as jest.Mock).mock.calls.at(-1);
  expect(key).toBe(SIRI_KEY);
  expect(opts).toEqual({ keychainService: 'scout.siri', keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY });
});

test('with Scout unreachable the kept key stays; with none kept the error shows', async () => {
  expect(await setUpSiri(P, server(503, {}))).toEqual(['ask']); // kept from the test above
  await expect(setUpSiri({ ...P, device: 'dother1234567890' }, server(503, {}))).rejects.toThrow(/503/);
});

test("Siri signs a question for exactly its text (web.ask_purpose)", () => {
  expect(askPurpose('is Starlink down?')).toBe('scout:ask|is Starlink down?');
});

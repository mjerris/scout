import { normalizeServer, ScoutServer, ServerError } from './server';

test('normalizeServer: https Tailscale names, localhost over http, nothing else in the clear', () => {
  expect(normalizeServer('mac.tail1234.ts.net')).toBe('https://mac.tail1234.ts.net');
  expect(normalizeServer(' https://mac.tail1234.ts.net/phone?token=x ')).toBe('https://mac.tail1234.ts.net');
  expect(normalizeServer('https://mac.tail1234.ts.net:8443/')).toBe('https://mac.tail1234.ts.net:8443');
  expect(normalizeServer('http://localhost:8765')).toBe('http://localhost:8765');
  expect(normalizeServer('http://127.0.0.1:8765')).toBe('http://127.0.0.1:8765');
  expect(() => normalizeServer('http://mac.tail1234.ts.net')).toThrow(/https/);
  expect(() => normalizeServer('http://100.101.102.103:8765')).toThrow(/https/);
  expect(() => normalizeServer('')).toThrow();
  expect(() => normalizeServer('ftp://x')).toThrow();
});

function fakeFetch(status: number, body: unknown) {
  const calls: { url: string; init: RequestInit }[] = [];
  const impl = (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
  }) as unknown as typeof fetch;
  return { calls, impl };
}

test('pair posts the name with the web token as a Bearer token', async () => {
  const f = fakeFetch(200, { device: 'd1234567890abcdef', secret: 'c2VjcmV0', address: '/private/scout' });
  const s = new ScoutServer('https://mac.ts.net', 'web-token-value', f.impl);
  expect(await s.pair('Mike iPhone')).toEqual({
    device: 'd1234567890abcdef',
    secret: 'c2VjcmV0',
    address: '/private/scout',
  });
  expect(f.calls).toHaveLength(1);
  const call = f.calls[0]!;
  expect(call.url).toBe('https://mac.ts.net/api/phone/pair');
  expect(call.init.method).toBe('POST');
  expect((call.init.headers as Record<string, string>).Authorization).toBe('Bearer web-token-value');
  expect(JSON.parse(call.init.body as string)).toEqual({ name: 'Mike iPhone' });
});

test("errors carry Scout's own message, and a 401 says what token it wants", async () => {
  const conflict = fakeFetch(409, { status: 'error', error: 'Phone calls are off: set [phone] enabled = true' });
  await expect(new ScoutServer('https://m', 't', conflict.impl).token()).rejects.toThrow(/HTTP 409\): Phone calls are off/);
  const denied = fakeFetch(401, null);
  const err = await new ScoutServer('https://m', 't', denied.impl).pair('x').catch((e: unknown) => e);
  expect(err).toBeInstanceOf(ServerError);
  expect((err as ServerError).status).toBe(401);
  expect((err as Error).message).toMatch(/web_token/);
});

test('token returns the minted subscriber token', async () => {
  const f = fakeFetch(200, { token: 'sat-token', expires_at: 1760003600 });
  expect(await new ScoutServer('https://m', 't', f.impl).token()).toEqual({ token: 'sat-token', expires_at: 1760003600 });
  expect(f.calls[0]!.url).toBe('https://m/api/phone/token');
});

test('a network failure says Scout could not be reached', async () => {
  const impl = (async () => {
    throw new TypeError('Network request failed');
  }) as unknown as typeof fetch;
  await expect(new ScoutServer('https://m', 't', impl).token()).rejects.toThrow(/Couldn't reach Scout.*Tailscale/);
});

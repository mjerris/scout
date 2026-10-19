import { normalizeServer, ScoutServer, ServerError } from './server';
import { PAIR_REVIEW, pairDecidePurpose, sign, TOKEN_PURPOSE } from './sign';

test('normalizeServer: https Tailscale names, localhost over http, nothing else in the clear', () => {
  expect(normalizeServer('mac.tail1234.ts.net')).toBe('https://mac.tail1234.ts.net');
  expect(normalizeServer(' https://mac.tail1234.ts.net/phone?token=x ')).toBe('https://mac.tail1234.ts.net');
  expect(normalizeServer('https://mac.tail1234.ts.net:8443/')).toBe('https://mac.tail1234.ts.net:8443');
  expect(normalizeServer('http://localhost:8765')).toBe('http://localhost:8765');
  expect(normalizeServer('http://127.0.0.1:8765')).toBe('http://127.0.0.1:8765');
  expect(() => normalizeServer('http://mac.tail1234.ts.net')).toThrow(/https/);
  expect(() => normalizeServer('http://100.101.102.103:8765')).toThrow(/https/);
  expect(() => normalizeServer('')).toThrow(/Tailscale address/);
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

const headers = (c: { init: RequestInit }) => c.init.headers as Record<string, string>;

test('pairing with a code sends the code and the name, and no token', async () => {
  const f = fakeFetch(200, { device: 'd1234567890abcdef', secret: 'c2VjcmV0', address: '/private/scout' });
  const s = new ScoutServer('https://mac.ts.net', null, f.impl);
  expect(await s.pairWithCode('ABCD-EFGH', 'Mike iPhone')).toEqual({
    device: 'd1234567890abcdef',
    secret: 'c2VjcmV0',
    address: '/private/scout',
  });
  const call = f.calls[0]!;
  expect(call.url).toBe('https://mac.ts.net/api/phone/pair');
  expect(call.init.method).toBe('POST');
  expect(headers(call).Authorization).toBeUndefined();
  expect(JSON.parse(call.init.body as string)).toEqual({ code: 'ABCD-EFGH', name: 'Mike iPhone' });
});

test("errors are Scout's own words", async () => {
  const expired = fakeFetch(410, { status: 'error', error: 'That pairing code expired (a code lasts 10 minutes).' });
  const err = await new ScoutServer('https://m', null, expired.impl).pairWithCode('ABCD-EFGH', 'x').catch((e: unknown) => e);
  expect(err).toBeInstanceOf(ServerError);
  expect((err as ServerError).status).toBe(410);
  expect((err as Error).message).toBe('That pairing code expired (a code lasts 10 minutes).');
  const bare = fakeFetch(500, null);
  await expect(new ScoutServer('https://m', null, bare.impl).pairWithCode('ABCD-EFGH', 'x')).rejects.toThrow(/HTTP 500/);
});

test('asking to pair, then waiting for the yes', async () => {
  const f = fakeFetch(200, { request: 'r'.repeat(43), match: '47', expires_at: 1760000120 });
  const s = new ScoutServer('https://m', null, f.impl);
  expect(await s.requestPairing('Mike iPhone')).toEqual({ request: 'r'.repeat(43), match: '47', expires_at: 1760000120 });
  expect(f.calls[0]!.url).toBe('https://m/api/phone/pair/request');
  const g = fakeFetch(200, { status: 'approved', device: 'd1', secret: 's', address: '/private/scout' });
  expect(await new ScoutServer('https://m', null, g.impl).pairStatus('rid', 20)).toEqual({
    status: 'approved',
    device: 'd1',
    secret: 's',
    address: '/private/scout',
  });
  expect(JSON.parse(g.calls[0]!.init.body as string)).toEqual({ request: 'rid', wait: 20 });
});

test('a token request is signed with the device secret', async () => {
  const f = fakeFetch(200, { token: 'sat-token', expires_at: 1760003600 });
  const me = { device: 'd1234567890abcdef', secret: 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8' };
  expect(await new ScoutServer('https://m', me, f.impl).token()).toEqual({ token: 'sat-token', expires_at: 1760003600 });
  const call = f.calls[0]!;
  expect(call.url).toBe('https://m/api/phone/token');
  expect(headers(call).Authorization).toBeUndefined();
  const body = JSON.parse(call.init.body as string) as { device: string; ts: number; nonce: string; sig: string };
  expect(body.device).toBe(me.device);
  expect(body.sig).toBe(sign(me.secret, me.device, body.ts, body.nonce, TOKEN_PURPOSE));
});

test('a phone paired with the web token still sends it', async () => {
  const f = fakeFetch(200, { token: 'sat', expires_at: 0 });
  const me = { device: 'd1234567890abcdef', secret: 'c2VjcmV0', accessToken: 'web-token-value' };
  await new ScoutServer('https://m', me, f.impl).token();
  expect(headers(f.calls[0]!).Authorization).toBe('Bearer web-token-value');
});

test('a network failure says to turn Tailscale on', async () => {
  const impl = (async () => {
    throw new TypeError('Network request failed');
  }) as unknown as typeof fetch;
  await expect(new ScoutServer('https://m', null, impl).pairWithCode('ABCD-EFGH', 'x')).rejects.toThrow(
    /Couldn't reach Scout at https:\/\/m.*Turn on Tailscale/,
  );
});

test('pairing requests from other devices: listed and answered with signatures for exactly that', async () => {
  const me = { device: 'd1234567890abcdef', secret: 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8' };
  const req = { id: 'ABCDEF', name: 'Guest', match: '47', login: '', owner: false, peer: '', ip: '100.64.0.9', agent: 'x', at: 1, expires_at: 2 };
  const f = fakeFetch(200, { requests: [req, null], version: 3 });
  expect(await new ScoutServer('https://m', me, f.impl).pairPending(2, 20)).toEqual({ requests: [req], version: 3 });
  const pend = JSON.parse(f.calls[0]!.init.body as string) as Record<string, unknown>;
  expect(f.calls[0]!.url).toBe('https://m/api/phone/pair/pending');
  expect(pend).toMatchObject({ device: me.device, since: 2, wait: 20 });
  expect(pend.sig).toBe(sign(me.secret, me.device, pend.ts as number, pend.nonce as string, PAIR_REVIEW));

  const g = fakeFetch(200, { status: 'denied' });
  expect(await new ScoutServer('https://m', me, g.impl).pairDecide('ABCDEF', false)).toEqual({ status: 'denied' });
  const dec = JSON.parse(g.calls[0]!.init.body as string) as Record<string, unknown>;
  expect(g.calls[0]!.url).toBe('https://m/api/phone/pair/decide');
  expect(dec).toMatchObject({ id: 'ABCDEF', approve: false });
  expect(dec.sig).toBe(sign(me.secret, me.device, dec.ts as number, dec.nonce as string, pairDecidePurpose('ABCDEF', false)));
  expect(pairDecidePurpose('ABCDEF', true)).toBe('scout:pair-decide|ABCDEF|approve');

  const gone = fakeFetch(404, { status: 'unknown' });
  expect(await new ScoutServer('https://m', me, gone.impl).pairDecide('ZZZZZZ', true)).toEqual({ status: 'unknown' });
  await expect(new ScoutServer('https://m', null, gone.impl).pairPending()).rejects.toThrow(/not paired/);
});

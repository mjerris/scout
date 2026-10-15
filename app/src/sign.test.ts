import { b64urlDecode, b64urlEncode, callVariables, makeNonce, sign } from './sign';

// The browser page's signer, for parity: loaded as plain text (it lives outside app/,
// where Babel can't resolve its helpers) and run as CommonJS.
interface BrowserSign {
  sign(secret: string, device: string, ts: number, nonce: string, address: string): Promise<string>;
}
declare const __dirname: string;
function loadBrowserSign(): BrowserSign {
  const fs = jest.requireActual<{ readFileSync(path: string, encoding: 'utf8'): string }>('fs');
  const text = fs.readFileSync(`${__dirname}/../../src/scout/phone_sign.js`, 'utf8');
  const mod = { exports: {} as BrowserSign };
  new Function('module', text)(mod);
  return mod.exports;
}
const browser = loadBrowserSign();

const SECRET = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8'; // bytes 0..31, not a real secret

test('base64url round trip', () => {
  const bytes = b64urlDecode(SECRET);
  expect(Array.from(bytes)).toEqual(Array.from({ length: 32 }, (_, i) => i));
  expect(b64urlEncode(bytes)).toBe(SECRET);
  expect(b64urlEncode(Uint8Array.from([0xfb, 0xff]))).toBe('-_8');
  expect(() => b64urlDecode('a+b')).toThrow();
});

test('signature matches Scout (phone_devices.sign, computed with Python)', () => {
  expect(sign(SECRET, 'd0123456789abcdef', 1760000000, 'AAAAAAAAAAAAAAAA', '/private/scout')).toBe(
    'cb0e1f829652402c1d2cc9afbab03938fbd79206584a50d9d10f68ab8c00a1e1',
  );
});

test('signature matches the browser page (phone_sign.js)', async () => {
  const args = [SECRET, 'd0123456789abcdef', 1760000123, 'xyzXYZ0123-_abcd', '/private/scout?x=1'] as const;
  expect(sign(...args)).toBe(await browser.sign(...args));
});

test('callVariables: seconds, a fresh nonce, a signature over them', () => {
  const p = { device: 'd0123456789abcdef', secret: SECRET, address: '/private/scout' };
  const v = callVariables(p, 1760000000999);
  expect(v.ts).toBe(1760000000);
  expect(v.nonce).toMatch(/^[A-Za-z0-9_-]{16}$/);
  expect(v.sig).toBe(sign(SECRET, p.device, v.ts, v.nonce, p.address));
  expect(makeNonce()).not.toBe(makeNonce());
});

// The signed userVariables a paired app sends with each call: the same scheme as the
// browser page (src/scout/phone_sign.js) and Scout's check (src/scout/phone_devices.py):
//   sig = hex(HMAC-SHA256(key = base64url-decoded secret, `${device}|${ts}|${nonce}|${address}`))
// Hermes has no WebCrypto, so the HMAC is @noble/hashes (pure JS); the nonce's random
// bytes come from crypto.getRandomValues (react-native-get-random-values, in the polyfills).
import { hmac } from '@noble/hashes/hmac';
import { sha256 } from '@noble/hashes/sha2';

const B64URL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

export function b64urlDecode(text: string): Uint8Array {
  const clean = text.replace(/=+$/, '');
  const out: number[] = [];
  let bits = 0;
  let value = 0;
  for (const ch of clean) {
    const v = B64URL.indexOf(ch);
    if (v < 0) throw new Error('not base64url');
    value = (value << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push((value >> bits) & 0xff);
    }
  }
  return Uint8Array.from(out);
}

export function b64urlEncode(bytes: Uint8Array): string {
  let out = '';
  let bits = 0;
  let value = 0;
  for (const b of bytes) {
    value = (value << 8) | b;
    bits += 8;
    while (bits >= 6) {
      bits -= 6;
      out += B64URL[(value >> bits) & 0x3f];
    }
    value &= 0xff;
  }
  if (bits > 0) out += B64URL[(value << (6 - bits)) & 0x3f];
  return out;
}

/** 16 base64url characters (12 random bytes). */
export function makeNonce(): string {
  return b64urlEncode(crypto.getRandomValues(new Uint8Array(12)));
}

export function sign(secret: string, device: string, ts: number, nonce: string, address: string): string {
  const mac = hmac(sha256, b64urlDecode(secret), new TextEncoder().encode(`${device}|${ts}|${nonce}|${address}`));
  return Array.from(mac, (b) => b.toString(16).padStart(2, '0')).join('');
}

export interface CallVariables {
  device: string;
  ts: number;
  nonce: string;
  sig: string;
}

/** What dial() carries: {device, ts, nonce, sig}. */
export function callVariables(
  pairing: { device: string; secret: string; address: string },
  now: number = Date.now(),
): CallVariables {
  const ts = Math.floor(now / 1000);
  const nonce = makeNonce();
  return { device: pairing.device, ts, nonce, sig: sign(pairing.secret, pairing.device, ts, nonce, pairing.address) };
}

/**
 * What a paired phone signs, in place of the agent address, to ask Scout for a SignalWire
 * token (phone_pairing.TOKEN_PURPOSE): never an agent address, so it can't pass as a call.
 */
export const TOKEN_PURPOSE = 'scout:phone-token';

/** The body of a signed token request: {device, ts, nonce, sig}. */
export function tokenRequest(pairing: { device: string; secret: string }, now: number = Date.now()): CallVariables {
  return callVariables({ ...pairing, address: TOKEN_PURPOSE }, now);
}

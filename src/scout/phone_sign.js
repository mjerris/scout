// The signed userVariables a paired app sends with each call (see phone_devices.py):
//   sig = hex(HMAC-SHA256(key = base64url-decoded secret, `${device}|${ts}|${nonce}|${address}`))
// Plain WebCrypto, so the same file runs in the browser and under node (tests).
(function (root) {
  "use strict";
  const subtle = () => globalThis.crypto.subtle;

  function b64urlDecode(text) {
    const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function b64urlEncode(bytes) {
    let bin = "";
    for (const b of bytes) bin += String.fromCharCode(b);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  // 16 base64url characters (12 random bytes).
  function makeNonce() {
    return b64urlEncode(globalThis.crypto.getRandomValues(new Uint8Array(12)));
  }

  async function sign(secret, device, ts, nonce, address) {
    const key = await subtle().importKey("raw", b64urlDecode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
      "sign",
    ]);
    const msg = new TextEncoder().encode(`${device}|${ts}|${nonce}|${address}`);
    const mac = new Uint8Array(await subtle().sign("HMAC", key, msg));
    return Array.from(mac, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  // What dial() carries: {device, ts, nonce, sig}.
  async function callVariables(pairing, now = Date.now()) {
    const ts = Math.floor(now / 1000);
    const nonce = makeNonce();
    const sig = await sign(pairing.secret, pairing.device, ts, nonce, pairing.address);
    return { device: pairing.device, ts, nonce, sig };
  }

  const api = { b64urlDecode, b64urlEncode, makeNonce, sign, callVariables };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ScoutSign = api;
})(typeof self !== "undefined" ? self : globalThis);

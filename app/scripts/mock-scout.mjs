// A stand-in for Scout's web server, for trying the app in the Simulator without Scout:
// the routes the app uses, with the same shapes as src/scout/web.py. It prints a
// scout://pair link with a one-time code (single use, 10 minutes); asking to pair without
// a code is approved after 3 seconds (Scout would ask the room). A token request is
// checked for a signature shape and answered 409, the way Scout does when [phone] isn't
// set up, so a call stops before SignalWire. With --ask, a made-up device asks to pair
// 5 seconds after start, for the paired app's Approve / Deny (also from a notification:
// xcrun simctl push, app/README.md). Push registrations and declined rings are logged.
//   node app/scripts/mock-scout.mjs [port] [--ask]     (0 or none: a free port; prints it)
import { randomBytes, randomInt } from 'node:crypto';
import { createServer } from 'node:http';

const ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
const raw = Array.from({ length: 8 }, () => ALPHABET[randomInt(ALPHABET.length)]).join('');
const code = `${raw.slice(0, 4)}-${raw.slice(4)}`;
const expires = Date.now() + 600_000;
let used = false;
const requests = new Map(); // request id -> {name, match, approved}
const args = process.argv.slice(2);
const ask = args.includes('--ask');
let pending = []; // devices asking to pair, as Scout shows them to a paired phone
let version = 0;
if (ask) {
  setTimeout(() => {
    const at = Date.now() / 1000;
    pending = [{ id: 'MOCKAB', name: 'Guest phone', match: '47', login: 'guest@example.com', owner: false,
      peer: 'guest-phone', ip: '100.64.0.9', agent: 'Scout/1.0', at, expires_at: at + 120 }];
    version++;
    console.log('Guest phone asks to pair (request MOCKAB, code 47)');
  }, 5000);
}
const signed = (b) => typeof b.device === 'string' && typeof b.sig === 'string';

const paired = (name) => {
  const device = 'd' + randomBytes(8).toString('hex');
  console.log(`paired ${device} (${name})`);
  return { device, secret: randomBytes(32).toString('base64url'), address: '/private/mock-scout' };
};

const server = createServer((req, res) => {
  const send = (status, body) => {
    res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(body));
  };
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    console.log(`${req.method} ${req.url}`);
    if (req.method !== 'POST') return send(405, { status: 'error', error: 'POST only' });
    let b;
    try {
      b = JSON.parse(body || '{}');
    } catch {
      return send(400, { status: 'error', error: 'the body must be a JSON object' });
    }
    const name = typeof b.name === 'string' ? b.name.trim() : '';
    if (req.url === '/api/phone/pair') {
      if (typeof b.code !== 'string') return send(401, null);
      if (!name) return send(400, { status: 'error', error: 'name must be a non-empty string' });
      const given = b.code.toUpperCase().replace(/[^A-Z0-9]/g, '');
      if (given !== raw) return send(401, { status: 'error', error: "That pairing code isn't right." });
      if (used) return send(410, { status: 'error', error: 'That pairing code was already used.' });
      if (Date.now() > expires) return send(410, { status: 'error', error: 'That pairing code expired.' });
      used = true;
      return send(200, paired(name));
    }
    if (req.url === '/api/phone/pair/request') {
      if (!name) return send(400, { status: 'error', error: 'name must be a non-empty string' });
      const id = randomBytes(32).toString('base64url');
      const r = { name, match: String(10 + randomInt(90)), approved: false };
      requests.set(id, r);
      setTimeout(() => (r.approved = true), 3000);
      console.log(`${name} asks to pair, code ${r.match} (approving in 3 s)`);
      return send(200, { request: id, match: r.match, expires_at: Date.now() / 1000 + 120 });
    }
    if (req.url === '/api/phone/pair/status') {
      const r = requests.get(b.request);
      if (!r) return send(200, { status: 'unknown' });
      if (!r.approved) return setTimeout(() => send(200, { status: 'waiting', match: r.match }), 1000);
      requests.delete(b.request);
      return send(200, { status: 'approved', ...paired(r.name) });
    }
    if (req.url === '/api/phone/pair/pending') {
      if (!signed(b)) return send(401, null);
      // A long poll: held while nothing changed (checked each second, up to `wait`).
      const until = Date.now() + Math.min(25, Number(b.wait) || 0) * 1000;
      const answer = () =>
        b.since === version && Date.now() < until ? setTimeout(answer, 1000) : send(200, { requests: pending, version });
      return answer();
    }
    if (req.url === '/api/phone/pair/decide') {
      if (!signed(b)) return send(401, null);
      if (!pending.some((r) => r.id === b.id)) return send(404, { status: 'unknown' });
      pending = pending.filter((r) => r.id !== b.id);
      version++;
      console.log(`request ${b.id} ${b.approve ? 'approved' : 'denied'} by the phone`);
      return send(200, b.approve ? { status: 'approved', device: 'dmockguest' } : { status: 'denied' });
    }
    if (req.url === '/api/phone/push') {
      if (!signed(b)) return send(401, null);
      console.log(`push registered: calls ${b.voip ? 'yes' : 'no'}, notifications ${b.alert ? 'yes' : 'no'}, ${b.env}`);
      return send(200, { status: 'ok', ring: false, push: ask }); // with --ask the app asks for notifications
    }
    if (req.url === '/api/phone/ring/decline') {
      if (!signed(b)) return send(401, null);
      console.log('ring declined');
      return send(404, { status: 'unknown' });
    }
    if (req.url === '/api/phone/token') {
      if (typeof b.device !== 'string' || typeof b.sig !== 'string') return send(401, null);
      return send(409, { status: 'error', error: 'Mock Scout: no SignalWire here (a real Scout mints the token).' });
    }
    send(404, { status: 'error', error: 'not found' });
  });
});
server.listen(Number(args.find((a) => !a.startsWith('--')) ?? 0), '127.0.0.1', () => {
  const { port } = server.address();
  console.log(`mock Scout on http://localhost:${port}`);
  console.log(`pair link: scout://pair?server=${encodeURIComponent(`http://localhost:${port}`)}&code=${code}`);
});

// A stand-in for Scout's web server, for trying the app in the Simulator without Scout:
// the two routes the app uses, with the same auth and error shapes as src/scout/web.py.
// Pairing works (with a made-up device and secret); minting a SignalWire token answers
// 409 the way Scout does when [phone] isn't set up, so a call stops before SignalWire.
//   node app/scripts/mock-scout.mjs [port]     (0 or none: a free port; prints it)
import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';

const token = randomBytes(18).toString('base64url');
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
    if (req.headers.authorization !== `Bearer ${token}`) return send(401, null);
    if (req.url === '/api/phone/pair') {
      let name = '';
      try {
        name = JSON.parse(body).name;
      } catch {
        return send(400, { status: 'error', error: 'the body must be a JSON object' });
      }
      if (typeof name !== 'string' || !name.trim()) return send(400, { status: 'error', error: 'name must be a non-empty string' });
      const device = 'd' + randomBytes(8).toString('hex');
      console.log(`paired ${device} (${name})`);
      return send(200, { device, secret: randomBytes(32).toString('base64url'), address: '/private/mock-scout' });
    }
    if (req.url === '/api/phone/token') {
      return send(409, { status: 'error', error: 'Mock Scout: no SignalWire here (a real Scout mints the token).' });
    }
    send(404, { status: 'error', error: 'not found' });
  });
});
server.listen(Number(process.argv[2] ?? 0), '127.0.0.1', () => {
  const { port } = server.address();
  console.log(`mock Scout on http://localhost:${port}`);
  console.log(`pair link: scout://pair?server=${encodeURIComponent(`http://localhost:${port}`)}&token=${token}`);
});

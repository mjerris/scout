import { Subject } from 'rxjs';

import { resetRingsForTesting, takeRing, watchRings } from './ring';

type Entry = { uuid: string; data?: Record<string, string> };
const PAIRING = { server: 'https://mac.ts.net', name: 'p', device: 'd1234567890abcdef', secret: 's', address: '/private/scout' };

function rig() {
  const answers = new Subject<Entry>();
  const rejects = new Subject<Entry>();
  const send = jest.fn(async () => 'declined');
  const stop = watchRings({ answerRequested$: answers, rejectRequested$: rejects }, async () => PAIRING, send);
  return { answers, rejects, send, stop };
}

beforeEach(() => resetRingsForTesting());

test("Scout's answered rings wait for the call panel; other pushes are ignored", () => {
  const r = rig();
  r.answers.next({ uuid: 'u1', data: { scoutRing: 'tok-1' } });
  r.answers.next({ uuid: 'u2' });
  r.answers.next({ uuid: 'u3', data: { scoutRing: 'tok-3' } });
  // u1 ended in CallKit before the panel got to it: skipped.
  expect(takeRing((u) => u !== 'u1')).toEqual({ uuid: 'u3', ring: 'tok-3' });
  expect(takeRing(() => true)).toBeNull();
  r.stop();
});

test('a decline goes to Scout with the ring; a decline of anything else does not', async () => {
  const r = rig();
  r.rejects.next({ uuid: 'u1', data: { scoutRing: 'tok-1' } });
  r.rejects.next({ uuid: 'u2' });
  await new Promise((res) => setTimeout(res, 0));
  expect(r.send).toHaveBeenCalledTimes(1);
  expect(r.send).toHaveBeenCalledWith(PAIRING, 'tok-1');
  r.stop();
});

test('an unpaired phone declines nothing, and a failed decline is only logged', async () => {
  const rejects = new Subject<Entry>();
  const send = jest.fn(async () => {
    throw new Error('offline');
  });
  const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
  const stop = watchRings({ answerRequested$: new Subject(), rejectRequested$: rejects }, async () => null, send);
  rejects.next({ uuid: 'u1', data: { scoutRing: 't' } });
  await new Promise((res) => setTimeout(res, 0));
  expect(send).not.toHaveBeenCalled();
  stop();
  const stop2 = watchRings({ answerRequested$: new Subject(), rejectRequested$: rejects }, async () => PAIRING, send);
  rejects.next({ uuid: 'u2', data: { scoutRing: 't' } });
  await new Promise((res) => setTimeout(res, 0));
  expect(warn).toHaveBeenCalledWith("Couldn't tell Scout the call was declined:", 'offline');
  stop2();
  warn.mockRestore();
});

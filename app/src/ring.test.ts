import { BehaviorSubject, Subject } from 'rxjs';

import { endReason, resetRingsForTesting, takeRing, watchCancels, watchRings } from './ring';

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

type Display = { callUUID: string; fromPushKit?: string; errorCode?: string; payload?: unknown };
type Live = { uuid: string; state: string; data?: Record<string, string> };

function cancelRig() {
  const displays = new Set<(e: Display) => void>();
  const entries = new BehaviorSubject<readonly Live[]>([]);
  const reportEnded = jest.fn();
  const forget = jest.fn();
  const stop = watchCancels({
    onDisplay: (fn) => {
      displays.add(fn);
      return () => displays.delete(fn);
    },
    entries$: entries,
    reportEnded,
    forget,
  });
  const display = (e: Display) => displays.forEach((fn) => fn(e));
  return { display, entries, reportEnded, forget, stop };
}

const cancel = (uuid: string, scoutCancel: string, errorCode = 'CallUUIDAlreadyExists'): Display => ({
  callUUID: uuid,
  fromPushKit: '1',
  errorCode,
  payload: { callId: '', scoutCancel },
});

test('the end reasons are CallKit\'s: answered or declined elsewhere, or unanswered', () => {
  expect([endReason('answered'), endReason('declined'), endReason('missed')]).toEqual([4, 5, 3]);
});

test('a cancel for the ringing call ends it as answered elsewhere, once', () => {
  const r = cancelRig();
  // Scout rang this phone with this uuid (upper case); CallKit reports it lower case.
  r.display(cancel('aaaa-1', 'answered'));
  expect(r.reportEnded).toHaveBeenCalledWith('aaaa-1', 4);
  expect(r.forget).toHaveBeenCalledWith('aaaa-1');
  // The wrapper's entry for it, seen again: not ended natively a second time.
  r.entries.next([{ uuid: 'aaaa-1', state: 'pending-push', data: { scoutCancel: 'answered' } }]);
  expect(r.reportEnded).toHaveBeenCalledTimes(1);
  r.display(cancel('aaaa-2', 'declined'));
  r.display(cancel('aaaa-3', 'missed'));
  expect(r.reportEnded.mock.calls).toEqual([
    ['aaaa-1', 4],
    ['aaaa-2', 5],
    ['aaaa-3', 3],
  ]);
  r.stop();
});

test('a cancel that came first (an unknown call) is reported, then ended at once', () => {
  const r = cancelRig();
  // No error: CallKit had no such call, so it is ringing now; it ends straight away.
  r.display(cancel('bbbb-1', 'missed', ''));
  expect(r.reportEnded).toHaveBeenCalledWith('bbbb-1', 3);
  r.stop();
});

test('a cancel adopted at a cold start (no display event) is ended from the registry', () => {
  const r = cancelRig();
  r.entries.next([
    { uuid: 'cccc-1', state: 'pending-push', data: { scoutRing: 'tok' } }, // a ring: left alone
    { uuid: 'cccc-2', state: 'pending-push', data: { scoutCancel: 'declined' } },
    { uuid: 'cccc-3', state: 'ended', data: { scoutCancel: 'missed' } }, // already over
  ]);
  expect(r.reportEnded.mock.calls).toEqual([['cccc-2', 5]]);
  expect(r.forget.mock.calls).toEqual([['cccc-2']]);
  r.stop();
});

test('a cancel for the call this phone answered leaves it, and its answer still waits', () => {
  const answers = new Subject<Entry>();
  const stopRings = watchRings({ answerRequested$: answers, rejectRequested$: new Subject() }, async () => PAIRING);
  const r = cancelRig();
  const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
  answers.next({ uuid: 'DDDD-1', data: { scoutRing: 'tok-1' } });
  answers.next({ uuid: 'dddd-2', data: { scoutRing: 'tok-2' } });
  r.display(cancel('dddd-1', 'missed')); // the ring ran out just after this phone answered
  expect(r.reportEnded).not.toHaveBeenCalled();
  expect(r.forget).not.toHaveBeenCalled();
  expect(takeRing(() => true)).toEqual({ uuid: 'DDDD-1', ring: 'tok-1' });
  warn.mockRestore();
  r.stop();
  stopRings();
});

test('only pushes count, and pushes that are not cancels are left to the wrapper', () => {
  const r = cancelRig();
  r.display({ callUUID: 'eeee-1', fromPushKit: '0', payload: { scoutCancel: 'missed' } });
  r.display({ callUUID: 'eeee-2', fromPushKit: '1', payload: { scoutRing: 'tok' } });
  r.display({ callUUID: 'eeee-3', fromPushKit: '1', payload: '' });
  expect(r.reportEnded).not.toHaveBeenCalled();
  r.stop();
});

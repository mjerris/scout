import { registerPush } from './push';
import type { PushDeps } from './push';

const VOIP = 'ab'.repeat(32);
const ALERT = 'cd'.repeat(32);

function deps(alert = ALERT) {
  let voip: ((t: string) => void) | null = null;
  const alertToken = jest.fn(async () => alert);
  const d: PushDeps = {
    watchVoip: (fn) => {
      voip = fn;
      return () => (voip = null);
    },
    alertToken,
    env: async () => 'sandbox',
  };
  return { d, alertToken, voip: (t: string) => voip?.(t), watching: () => voip !== null };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

test('registers the PushKit token, then asks for notifications once Scout can send them', async () => {
  const server = { registerPush: jest.fn(async () => ({ push: true })) };
  const t = deps();
  const stop = registerPush(server, () => {}, t.d);
  await flush();
  expect(server.registerPush).not.toHaveBeenCalled(); // no token yet
  t.voip(VOIP);
  await flush();
  expect(server.registerPush).toHaveBeenNthCalledWith(1, VOIP, '', 'sandbox');
  expect(server.registerPush).toHaveBeenNthCalledWith(2, VOIP, ALERT, 'sandbox');
  t.voip(VOIP); // the same again: nothing new to tell
  await flush();
  expect(server.registerPush).toHaveBeenCalledTimes(2);
  expect(t.alertToken).toHaveBeenCalledTimes(1);
  stop();
  expect(t.watching()).toBe(false);
});

test("a Scout without push never prompts for notifications", async () => {
  const server = { registerPush: jest.fn(async () => ({ push: false })) };
  const t = deps();
  registerPush(server, () => {}, t.d);
  t.voip(VOIP);
  await flush();
  expect(server.registerPush).toHaveBeenCalledTimes(1);
  expect(t.alertToken).not.toHaveBeenCalled();
});

test('a failure is reported and retried on the next token', async () => {
  const server = { registerPush: jest.fn(async (): Promise<{ push: boolean }> => Promise.reject(new Error('offline'))) };
  const onResult = jest.fn();
  const t = deps('');
  registerPush(server, onResult, t.d);
  t.voip(VOIP);
  await flush();
  expect(onResult).toHaveBeenLastCalledWith("Scout couldn't be told where to reach this phone: offline");
  server.registerPush.mockImplementation(async () => ({ push: false }));
  t.voip('ef'.repeat(32));
  await flush();
  expect(onResult).toHaveBeenLastCalledWith('');
});

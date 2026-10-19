import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import { describe as lines, pairAction, PairApprovals } from './PairApprovals';

import type { PairReviewer } from './PairApprovals';
import type { PairAnswer, PendingPair } from './server';

// expo-notifications, with the response listener the test drives.
const mockNotes: { listener?: (r: unknown) => void } = {};
jest.mock('expo-notifications', () => ({
  addNotificationResponseReceivedListener: (fn: (r: unknown) => void) => {
    mockNotes.listener = fn;
    return { remove: () => (mockNotes.listener = undefined) };
  },
  getLastNotificationResponseAsync: async () => null,
  clearLastNotificationResponseAsync: async () => {},
}));

const PAIRING = { server: 'https://m', name: 'Mike iPhone', device: 'd1234567890abcdef', secret: 's', address: '/a' };
const REQ: PendingPair = {
  id: 'ABCDEF',
  name: 'Guest phone',
  match: '47',
  login: 'guest@example.com',
  owner: false,
  peer: 'guest-phone.tail1.ts.net',
  ip: '100.64.0.9',
  agent: 'Scout/1.0',
  at: 1760000000,
  expires_at: 1760000120,
};

/** A Scout whose long poll answers when the test says. */
function fakeScout() {
  const polls: { since: number; resolve: (v: { requests: PendingPair[]; version: number }) => void }[] = [];
  const reviewer = {
    pairPending: jest.fn(
      (since?: number) =>
        new Promise<{ requests: PendingPair[]; version: number }>((resolve) => polls.push({ since: since ?? -1, resolve })),
    ),
    pairDecide: jest.fn(async (_id: string, approve: boolean): Promise<PairAnswer> =>
      approve ? { status: 'approved', device: 'dnew' } : { status: 'denied' },
    ),
  };
  return { reviewer: reviewer as unknown as PairReviewer & typeof reviewer, polls };
}

test('the prompt says who is asking, labelled, with the fixed prefix', () => {
  expect(lines(REQ)).toEqual([
    'A new device calling itself "Guest phone" wants to pair with Scout.',
    'It shows the code 47.',
    'Tailscale: guest@example.com',
    'Device: guest-phone.tail1.ts.net',
    'Address: 100.64.0.9',
    'App: Scout/1.0',
    expect.stringMatching(/^Asked at /),
  ]);
  expect(lines({ ...REQ, name: '', login: '', owner: false, peer: '', agent: '' })[0]).toBe(
    'A new device with no usable name wants to pair with Scout.',
  );
  expect(lines({ ...REQ, login: 'me@example.com', owner: true })[2]).toBe('Tailscale: me@example.com (yours)');
  expect(lines({ ...REQ, login: '' })[2]).toBe('Tailscale: login not verified');
});

test('a request shows up, and Approve answers it signed by this phone', async () => {
  const { reviewer, polls } = fakeScout();
  render(<PairApprovals pairing={PAIRING} server={reviewer} />);
  await waitFor(() => expect(polls.length).toBe(1));
  await act(async () => polls[0]!.resolve({ requests: [REQ], version: 1 }));
  expect(await screen.findByTestId('pair-text-ABCDEF')).toHaveTextContent(/calling itself "Guest phone"/);
  await waitFor(() => expect(polls.length).toBe(2));
  expect(polls[1]!.since).toBe(1); // the long poll waits for a change after what it has
  await act(async () => fireEvent.press(screen.getByTestId('pair-approve-ABCDEF')));
  expect(reviewer.pairDecide).toHaveBeenCalledWith('ABCDEF', true);
  expect(await screen.findByText('Approved: paired as dnew.')).toBeTruthy();
  expect(screen.queryByTestId('pair-text-ABCDEF')).toBeNull();
});

test('Deny, and a request answered elsewhere disappears with a note', async () => {
  const { reviewer, polls } = fakeScout();
  render(<PairApprovals pairing={PAIRING} server={reviewer} />);
  await waitFor(() => expect(polls.length).toBe(1));
  await act(async () => polls[0]!.resolve({ requests: [REQ, { ...REQ, id: 'GHJKLM' }], version: 1 }));
  await act(async () => fireEvent.press(await screen.findByTestId('pair-deny-ABCDEF')));
  expect(reviewer.pairDecide).toHaveBeenCalledWith('ABCDEF', false);
  expect(await screen.findByText('Denied.')).toBeTruthy();
  await waitFor(() => expect(polls.length).toBe(2));
  await act(async () => polls[1]!.resolve({ requests: [], version: 2 }));
  expect(await screen.findByText('Request GHJKLM was answered elsewhere, or expired.')).toBeTruthy();
  expect(screen.queryByText(/Request ABCDEF was answered elsewhere/)).toBeNull(); // this phone answered that one
});

test('an answer that lost the race says who won', async () => {
  const { reviewer, polls } = fakeScout();
  reviewer.pairDecide.mockResolvedValueOnce({ status: 'already', result: 'approved', via: 'voice' });
  render(<PairApprovals pairing={PAIRING} server={reviewer} />);
  await waitFor(() => expect(polls.length).toBe(1));
  await act(async () => polls[0]!.resolve({ requests: [REQ], version: 1 }));
  await act(async () => fireEvent.press(await screen.findByTestId('pair-approve-ABCDEF')));
  expect(await screen.findByText('Already answered (approved by voice).')).toBeTruthy();
});

test('nothing is shown when nobody is asking', async () => {
  const { reviewer, polls } = fakeScout();
  const { toJSON } = render(<PairApprovals pairing={PAIRING} server={reviewer} />);
  await waitFor(() => expect(polls.length).toBe(1));
  await act(async () => polls[0]!.resolve({ requests: [], version: 0 }));
  expect(toJSON()).toBeNull();
});

test('Approve on the notification answers the request, signed, like the button', async () => {
  const listener = (r: unknown) => mockNotes.listener?.(r);
  const { reviewer } = fakeScout();
  render(<PairApprovals pairing={PAIRING} server={reviewer} />);
  const response = (action: string, id = 'ABCDEF', ident = 'n1') => ({
    actionIdentifier: action,
    notification: { request: { identifier: ident, content: { data: { scoutPair: { id, match: '47' } } } } },
  });
  await act(async () => listener(response('com.apple.UNNotificationDefaultActionIdentifier')));
  expect(reviewer.pairDecide).not.toHaveBeenCalled(); // a tap just opens the app
  await act(async () => listener(response('approve')));
  await act(async () => listener(response('approve'))); // the same response again (cold start)
  expect(reviewer.pairDecide).toHaveBeenCalledTimes(1);
  expect(reviewer.pairDecide).toHaveBeenCalledWith('ABCDEF', true);
  expect(await screen.findByText('Approved: paired as dnew.')).toBeTruthy();
  await act(async () => listener(response('deny', 'ZZZZZZ', 'n2')));
  expect(reviewer.pairDecide).toHaveBeenLastCalledWith('ZZZZZZ', false);
});

test('pairAction reads only our actions with a request id', () => {
  const r = (actionIdentifier: string, data: unknown) =>
    ({ actionIdentifier, notification: { request: { identifier: 'x', content: { data } } } }) as never;
  expect(pairAction(r('approve', { scoutPair: { id: 'A' } }))).toEqual({ id: 'A', approve: true });
  expect(pairAction(r('deny', { scoutPair: { id: 'A' } }))).toEqual({ id: 'A', approve: false });
  expect(pairAction(r('approve', {}))).toBeNull();
  expect(pairAction(r('other', { scoutPair: { id: 'A' } }))).toBeNull();
  expect(pairAction(null)).toBeNull();
});

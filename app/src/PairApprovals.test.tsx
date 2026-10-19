import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import { describe as lines, PairApprovals } from './PairApprovals';
import type { PairReviewer } from './PairApprovals';
import type { PairAnswer, PendingPair } from './server';

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

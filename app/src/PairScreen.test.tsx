import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import type { Found } from './discovery';
import type { Pairing } from './pairing';
import { PairScreen } from './PairScreen';
import type { PairStatus, ScoutServer } from './server';
import { ServerError } from './server';

jest.mock('expo-secure-store', () => {
  const items = new Map<string, string>();
  return {
    WHEN_UNLOCKED_THIS_DEVICE_ONLY: 6,
    getItemAsync: jest.fn(async (k: string) => items.get(k) ?? null),
    setItemAsync: jest.fn(async (k: string, v: string) => void items.set(k, v)),
    deleteItemAsync: jest.fn(async (k: string) => void items.delete(k)),
  };
});

const PAIRED = { device: 'd1234567890abcdef', secret: 'c2VjcmV0', address: '/private/scout' };
type Fake = jest.Mocked<Pick<ScoutServer, 'pairWithCode' | 'requestPairing' | 'pairStatus'>>;

function fakeServer(): Fake {
  return {
    pairWithCode: jest.fn(async (_code: string, _name: string) => PAIRED),
    requestPairing: jest.fn(async (_name: string) => ({ request: 'rid', match: '47', expires_at: 0 })),
    pairStatus: jest.fn(async (_request: string, _wait?: number): Promise<PairStatus> => ({ status: 'approved', ...PAIRED })),
  };
}

function setup(over: Partial<Parameters<typeof PairScreen>[0]> = {}) {
  const server = fakeServer();
  const makeServer = jest.fn(() => server);
  const onPaired = jest.fn((_p: Pairing) => {});
  const onDismissLink = jest.fn();
  render(
    <PairScreen
      link={null}
      onPaired={onPaired}
      onDismissLink={onDismissLink}
      makeServer={makeServer}
      browse={() => () => {}}
      {...over}
    />,
  );
  return { server, makeServer, onPaired, onDismissLink };
}

const LINK = { server: 'https://mac.tail1.ts.net', code: 'ABCD-EFGH' };

test('a scanned link asks "Pair with <host>?" and pairs with one tap', async () => {
  const { server, makeServer, onPaired } = setup({ link: LINK });
  expect(screen.getByText('Pair with mac.tail1.ts.net?')).toBeTruthy();
  await act(async () => fireEvent.press(screen.getByTestId('pair')));
  expect(makeServer).toHaveBeenCalledWith('https://mac.tail1.ts.net');
  expect(server.pairWithCode).toHaveBeenCalledWith('ABCD-EFGH', 'iPhone');
  await waitFor(() => expect(onPaired).toHaveBeenCalled());
  const p = onPaired.mock.calls[0]![0];
  expect(p).toEqual({ server: 'https://mac.tail1.ts.net', name: 'iPhone', ...PAIRED });
  expect(p).not.toHaveProperty('accessToken');
});

test("an expired code says so, in Scout's words", async () => {
  const server = fakeServer();
  server.pairWithCode.mockRejectedValue(
    new ServerError('That pairing code expired (a code lasts 10 minutes). Make a new one: scoutctl phone pair', 410),
  );
  setup({ link: LINK, makeServer: () => server });
  await act(async () => fireEvent.press(screen.getByTestId('pair')));
  expect(await screen.findByTestId('error')).toHaveTextContent(/expired.*scoutctl phone pair/);
});

test('"Not now" goes back to the other ways', async () => {
  const { onDismissLink } = setup({ link: LINK });
  fireEvent.press(screen.getByTestId('not-now'));
  expect(onDismissLink).toHaveBeenCalled();
});

test('the manual form explains the address instead of showing a fake one', () => {
  setup();
  const server = screen.getByTestId('server');
  expect(server.props.placeholder).toBe('https://your-mac.your-tailnet.ts.net');
  expect(screen.getByText(/Your Mac's Tailscale address, e\.g\. https:\/\/your-mac\.your-tailnet\.ts\.net/)).toBeTruthy();
  expect(screen.getByText(/scoutctl phone pair/)).toBeTruthy();
  expect(screen.queryByText(/web_token/)).toBeNull();
});

test('a pasted scout:// link in the manual form fills in the address and code', async () => {
  const { server, makeServer, onPaired } = setup();
  fireEvent.changeText(
    screen.getByTestId('code'),
    'scout://pair?server=https%3A%2F%2Fmac.tail1.ts.net&code=ABCD-EFGH',
  );
  await act(async () => fireEvent.press(screen.getByTestId('pair-code')));
  expect(makeServer).toHaveBeenCalledWith('https://mac.tail1.ts.net');
  expect(server.pairWithCode).toHaveBeenCalledWith('ABCD-EFGH', 'iPhone');
  await waitFor(() => expect(onPaired).toHaveBeenCalled());
});

test('a typed address and code pair', async () => {
  const { server, makeServer } = setup();
  fireEvent.changeText(screen.getByTestId('server'), 'mac.tail1.ts.net');
  fireEvent.changeText(screen.getByTestId('code'), 'abcd efgh');
  fireEvent.changeText(screen.getByTestId('name'), 'Mike iPhone');
  await act(async () => fireEvent.press(screen.getByTestId('pair-code')));
  expect(makeServer).toHaveBeenCalledWith('https://mac.tail1.ts.net');
  expect(server.pairWithCode).toHaveBeenCalledWith('ABCD-EFGH', 'Mike iPhone');
});

test('a code without an address, or a bad code, says what is missing', async () => {
  const { server } = setup();
  fireEvent.changeText(screen.getByTestId('code'), 'ABCD-EFGH');
  await act(async () => fireEvent.press(screen.getByTestId('pair-code')));
  expect(screen.getByTestId('error')).toHaveTextContent(/Tailscale address/);
  fireEvent.changeText(screen.getByTestId('server'), 'mac.tail1.ts.net');
  fireEvent.changeText(screen.getByTestId('code'), 'nope');
  await act(async () => fireEvent.press(screen.getByTestId('pair-code')));
  expect(screen.getByTestId('error')).toHaveTextContent(/ABCD-EFGH/);
  expect(server.pairWithCode).not.toHaveBeenCalled();
});

test('Scout found on the network pairs by asking the room, showing the match number', async () => {
  let release: (v: { status: 'approved' } & typeof PAIRED) => void = () => {};
  const server = fakeServer();
  server.pairStatus
    .mockResolvedValueOnce({ status: 'waiting', match: '47' })
    .mockImplementationOnce(() => new Promise((r) => (release = r)));
  const found: Found[] = [{ key: "Scout on Mike's Mac", label: "Mike's Mac", server: 'https://mac.tail1.ts.net' }];
  const makeServer = jest.fn(() => server);
  const { onPaired } = setup({ makeServer, browse: (onChange) => (onChange(found), () => {}) });
  expect(screen.getByText("Found Mike's Mac")).toBeTruthy();
  fireEvent.press(screen.getByTestId('found-0'));
  expect(makeServer).toHaveBeenCalledWith('https://mac.tail1.ts.net');
  expect(server.requestPairing).toHaveBeenCalledWith('iPhone');
  expect(await screen.findByTestId('match')).toHaveTextContent('47');
  expect(screen.getByText(/Say yes when Scout asks/)).toBeTruthy();
  await act(async () => release({ status: 'approved', ...PAIRED }));
  await waitFor(() => expect(onPaired).toHaveBeenCalled());
  expect(onPaired.mock.calls[0]![0]).toEqual({ server: 'https://mac.tail1.ts.net', name: 'iPhone', ...PAIRED });
});

test('asking by address, and a no from the room', async () => {
  const server = fakeServer();
  server.pairStatus.mockResolvedValue({ status: 'denied' });
  const { onPaired } = setup({ makeServer: () => server });
  fireEvent.changeText(screen.getByTestId('server'), 'mac.tail1.ts.net');
  fireEvent.press(screen.getByTestId('ask'));
  expect(await screen.findByTestId('error')).toHaveTextContent(/said no/);
  expect(onPaired).not.toHaveBeenCalled();
});

test('nobody answering in time is said plainly', async () => {
  const server = fakeServer();
  server.pairStatus.mockResolvedValue({ status: 'expired' });
  setup({ makeServer: () => server });
  fireEvent.changeText(screen.getByTestId('server'), 'mac.tail1.ts.net');
  fireEvent.press(screen.getByTestId('ask'));
  expect(await screen.findByTestId('error')).toHaveTextContent(/Nobody said yes/);
});

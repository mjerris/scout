import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import * as SecureStore from 'expo-secure-store';

import App from './App';

jest.mock('expo-secure-store', () => {
  const items = new Map<string, string>();
  return {
    WHEN_UNLOCKED_THIS_DEVICE_ONLY: 6,
    getItemAsync: jest.fn(async (k: string) => items.get(k) ?? null),
    setItemAsync: jest.fn(async (k: string, v: string) => void items.set(k, v)),
    deleteItemAsync: jest.fn(async (k: string) => void items.delete(k)),
  };
});

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

test('pairs with a typed code and keeps the secret in the Keychain', async () => {
  const fetchMock = jest.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ device: 'd1234567890abcdef', secret: 'c2VjcmV0', address: '/private/scout' }),
  }));
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  render(<App />);
  await screen.findByText('Pair with Scout');
  fireEvent.changeText(screen.getByTestId('server'), 'mac.tail1.ts.net');
  fireEvent.changeText(screen.getByTestId('code'), 'ABCD-EFGH');
  fireEvent.changeText(screen.getByTestId('name'), 'Mike iPhone');
  await act(async () => fireEvent.press(screen.getByTestId('pair-code')));
  await waitFor(() => expect(screen.getByTestId('paired')).toHaveTextContent(/Mike iPhone \(d1234567890abcdef\)/));
  expect(fetchMock).toHaveBeenCalledWith('https://mac.tail1.ts.net/api/phone/pair', expect.anything());
  const stored = JSON.parse((SecureStore.setItemAsync as jest.Mock).mock.calls.at(-1)[1]);
  expect(stored).toMatchObject({ secret: 'c2VjcmV0', device: 'd1234567890abcdef' });
  expect(stored).not.toHaveProperty('accessToken');
});

test('shows why pairing failed', async () => {
  await SecureStore.deleteItemAsync('scout.pairing');
  globalThis.fetch = jest.fn(async () => ({
    ok: false,
    status: 409,
    json: async () => ({ status: 'error', error: "Set [phone] address (what the app dials) in Scout's config first." }),
  })) as unknown as typeof fetch;
  render(<App />);
  await screen.findByText('Pair with Scout');
  fireEvent.changeText(screen.getByTestId('server'), 'https://mac.tail1.ts.net');
  fireEvent.changeText(screen.getByTestId('code'), 'ABCD-EFGH');
  await act(async () => fireEvent.press(screen.getByTestId('pair-code')));
  expect(await screen.findByTestId('error')).toHaveTextContent(/\[phone\] address/);
});

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import RNCallKeep from 'react-native-callkeep';
import { BehaviorSubject, Subject } from 'rxjs';

import { CallPanel } from './CallPanel';
import { sign } from './sign';

// A stand-in for @signalwire/js: a client that connects at once and a call whose status
// the test drives.
const mockSw = {
  constructed: [] as unknown[],
  dial: jest.fn(),
  call: null as null | ReturnType<typeof fakeCall>,
};
function fakeCall() {
  const audioMuted$ = new BehaviorSubject(false);
  const self = {
    audioMuted$,
    get audioMuted(): boolean {
      return audioMuted$.value;
    },
    mute: jest.fn(async () => audioMuted$.next(true)),
    unmute: jest.fn(async () => audioMuted$.next(false)),
  };
  const status$ = new BehaviorSubject('trying');
  return {
    id: 'call-1',
    status$,
    get status() {
      return status$.value;
    },
    self$: new BehaviorSubject(self),
    self,
    errors$: new Subject(),
    hangup: jest.fn(async () => status$.next('disconnected')),
  };
}
jest.mock('@signalwire/js', () => {
  const rx = jest.requireActual<typeof import('rxjs')>('rxjs');
  const log = { trace: jest.fn(), debug: jest.fn(), info: jest.fn(), warn: jest.fn(), error: jest.fn() };
  return {
    getLogger: () => log,
    SignalWire: class {
      isConnected$ = new rx.BehaviorSubject(true);
      isConnected = true;
      isRegistered$ = new rx.BehaviorSubject(false);
      errors$ = new rx.Subject();
      constructor(...args: unknown[]) {
        mockSw.constructed.push(args);
      }
      dial = (...args: unknown[]) => mockSw.dial(...args);
      destroy = jest.fn();
    },
  };
});

const PAIRING = {
  server: 'https://mac.tail1.ts.net',
  accessToken: 'web-token-value',
  name: 'Mike iPhone',
  device: 'd1234567890abcdef',
  secret: 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8',
  address: '/private/scout',
};

function respond(status: number, body: unknown) {
  return { ok: status < 300, status, json: async () => body };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockSw.constructed = [];
  mockSw.call = fakeCall();
  mockSw.dial.mockImplementation(async () => mockSw.call);
});

test("when Scout can't mint a token, says why and builds no SignalWire client", async () => {
  globalThis.fetch = jest.fn(async () =>
    respond(409, { status: 'error', error: 'Phone calls are off: set [phone] enabled = true in Scout\'s config.' }),
  ) as unknown as typeof fetch;
  render(<CallPanel pairing={PAIRING} />);
  await act(async () => fireEvent.press(screen.getByTestId('call')));
  expect(await screen.findByTestId('error')).toHaveTextContent(/Phone calls are off/);
  expect(screen.getByTestId('state')).toHaveTextContent('State: idle');
  expect(mockSw.constructed).toHaveLength(0);
  expect(globalThis.fetch).toHaveBeenCalledWith('https://mac.tail1.ts.net/api/phone/token', expect.anything());
});

test('dials the agent with signed userVariables, as a voice call named Scout in CallKit', async () => {
  globalThis.fetch = jest.fn(async () => respond(200, { token: 'sat', expires_at: 1760003600 })) as unknown as typeof fetch;
  render(<CallPanel pairing={PAIRING} />);
  await act(async () => fireEvent.press(screen.getByTestId('call')));
  await waitFor(() => expect(mockSw.dial).toHaveBeenCalled());

  const [address, options] = mockSw.dial.mock.calls[0] as [string, { audio: boolean; video: boolean; userVariables: Record<string, unknown> }];
  expect(address).toBe('/private/scout');
  expect(options).toMatchObject({ audio: true, video: false });
  const uv = options.userVariables as { device: string; ts: number; nonce: string; sig: string };
  expect(uv.device).toBe(PAIRING.device);
  expect(uv.sig).toBe(sign(PAIRING.secret, PAIRING.device, uv.ts, uv.nonce, PAIRING.address));
  expect(Math.abs(uv.ts - Date.now() / 1000)).toBeLessThan(5);

  // The client: outbound only.
  expect(mockSw.constructed[0]).toEqual([expect.anything(), expect.objectContaining({ skipRegister: true })]);
  await waitFor(() =>
    expect(RNCallKeep.startCall).toHaveBeenCalledWith(expect.any(String), '/private/scout', 'Scout', 'generic', false),
  );
  const uuid = (RNCallKeep.startCall as jest.Mock).mock.calls[0][0];

  // Mute goes through CallKit, so the system call screen and a headset agree.
  await act(async () => mockSw.call!.status$.next('connected'));
  expect(screen.getByTestId('state')).toHaveTextContent('State: connected');
  fireEvent.press(screen.getByTestId('mute'));
  expect(RNCallKeep.setMutedCall).toHaveBeenCalledWith(uuid, true);

  // Hang up through CallKit; the call ending puts the panel back.
  fireEvent.press(screen.getByTestId('hangup'));
  expect(RNCallKeep.endCall).toHaveBeenCalledWith(uuid);
  await act(async () => mockSw.call!.status$.next('disconnected'));
  await waitFor(() => expect(screen.getByTestId('call')).toBeTruthy());
  expect(RNCallKeep.reportEndCallWithUUID).toHaveBeenCalledWith(uuid, expect.any(Number));
});

test('a call requested from Recents is placed like a press of Call', async () => {
  globalThis.fetch = jest.fn(async () => respond(200, { token: 'sat', expires_at: 0 })) as unknown as typeof fetch;
  const view = render(<CallPanel pairing={PAIRING} request={0} />);
  expect(mockSw.dial).not.toHaveBeenCalled();
  view.rerender(<CallPanel pairing={PAIRING} request={1} />);
  await waitFor(() => expect(mockSw.dial).toHaveBeenCalledTimes(1));
});

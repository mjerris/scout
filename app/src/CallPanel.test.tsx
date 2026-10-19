import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import RNCallKeep from '@signalwire/react-native-callkeep';
import { BehaviorSubject, Subject } from 'rxjs';

import { getCallKit, resetCallKitForTesting } from '@signalwire/react-native/callkit';

import { CallPanel } from './CallPanel';
import { resetRingsForTesting, watchRings } from './ring';
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
    hangup: jest.fn(async () => status$.next('destroyed')),
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
  name: 'Mike iPhone',
  device: 'd1234567890abcdef',
  secret: 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8',
  address: '/private/scout',
};

function respond(status: number, body: unknown) {
  return { ok: status < 300, status, json: async () => body };
}

// What index.ts does at launch.
let stopRings: () => void = () => {};
beforeAll(async () => {
  await getCallKit().setup({ appName: 'Scout', supportsVideo: false });
  stopRings = watchRings(getCallKit().registry, async () => PAIRING, mockDecline);
});
afterAll(() => {
  stopRings();
  resetCallKitForTesting(); // its timers (the push entries' fusion tick) stop with it
});
const mockDecline = jest.fn(async () => 'declined');

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
  // CallKit activates the audio session; the library waits for it (up to 3 s) to start audio.
  await act(async () =>
    (RNCallKeep as unknown as { __emit(t: string, d: unknown): void }).__emit('didActivateAudioSession', {}),
  );
  expect(screen.getByTestId('state')).toHaveTextContent('State: connected');
  fireEvent.press(screen.getByTestId('mute'));
  expect(RNCallKeep.setMutedCall).toHaveBeenCalledWith(uuid, true);

  // An ICE blip (Wi-Fi to cellular) is not the end of the call.
  await act(async () => mockSw.call!.status$.next('disconnected'));
  expect(screen.getByTestId('hangup')).toBeTruthy();
  await act(async () => mockSw.call!.status$.next('connected'));

  // Hang up through CallKit: its end action reaches the bridge, which hangs the call up,
  // and the call ending puts the panel back.
  fireEvent.press(screen.getByTestId('hangup'));
  expect(RNCallKeep.endCall).toHaveBeenCalledWith(uuid);
  await act(async () => (RNCallKeep as unknown as { __emit(t: string, d: unknown): void }).__emit('endCall', { callUUID: uuid }));
  await waitFor(() => expect(screen.getByTestId('call')).toBeTruthy());
  expect(mockSw.call!.hangup).toHaveBeenCalled();
});

test('a call asked for from Recents is placed, once', async () => {
  globalThis.fetch = jest.fn(async () => respond(200, { token: 'sat', expires_at: 0 })) as unknown as typeof fetch;
  // Before the panel is up (a cold start), and CallKit's echo of our own call is ignored.
  (RNCallKeep as unknown as { __emit(t: string, d: unknown): void }).__emit('didReceiveStartCallAction', {
    handle: '/private/scout',
  });
  render(<CallPanel pairing={PAIRING} />);
  await waitFor(() => expect(mockSw.dial).toHaveBeenCalledTimes(1));
  const uuid = await waitFor(() => (RNCallKeep.startCall as jest.Mock).mock.calls[0][0] as string);
  (RNCallKeep as unknown as { __emit(t: string, d: unknown): void }).__emit('didReceiveStartCallAction', {
    callUUID: uuid,
    handle: '/private/scout',
  });
  await act(async () => {});
  expect(mockSw.dial).toHaveBeenCalledTimes(1);
});

const emit = (t: string, d: unknown) => (RNCallKeep as unknown as { __emit(t: string, d: unknown): void }).__emit(t, d);

test("answering Scout's ring places a call to the agent with the ring, on the answered CallKit entry", async () => {
  resetRingsForTesting();
  globalThis.fetch = jest.fn(async () => respond(200, { token: 'sat', expires_at: 0 })) as unknown as typeof fetch;
  // What the PushKit hook does natively, then the user answers on the lock screen (before
  // the panel is up: a cold start).
  const uuid = getCallKit().reportIncomingPush({ callId: 'tok-1', from: 'Scout', fromName: 'Scout', data: { scoutRing: 'tok-1' } });
  emit('didDisplayIncomingCall', { callUUID: uuid, handle: 'Scout', fromPushKit: '1', payload: { call_id: 'tok-1' } });
  await act(async () => emit('answerCall', { callUUID: uuid }));
  await act(async () => emit('didActivateAudioSession', {})); // CallKit, once the answer is taken
  render(<CallPanel pairing={PAIRING} />);
  await waitFor(() => expect(mockSw.dial).toHaveBeenCalledTimes(1));
  const [address, options] = mockSw.dial.mock.calls[0] as [string, { userVariables: Record<string, unknown> }];
  expect(address).toBe('/private/scout');
  const uv = options.userVariables as { device: string; ts: number; nonce: string; sig: string; ring: string };
  expect(uv.ring).toBe('tok-1');
  expect(uv.sig).toBe(sign(PAIRING.secret, PAIRING.device, uv.ts, uv.nonce, PAIRING.address)); // a signed call as ever
  // It joins the entry the user answered: no second, outgoing CallKit call.
  await waitFor(() => expect(getCallKit().registry.entryForUuid(uuid)?.call).toBeTruthy());
  expect(RNCallKeep.startCall).not.toHaveBeenCalled();
  await act(async () => mockSw.call!.status$.next('connected'));
  expect(screen.getByTestId('state')).toHaveTextContent('State: connected');
  await act(async () => mockSw.call!.status$.next('destroyed'));
  await waitFor(() => expect(screen.getByTestId('call')).toBeTruthy());
});

test("declining Scout's ring tells Scout, and nothing is dialled", async () => {
  resetRingsForTesting();
  const uuid = getCallKit().reportIncomingPush({ callId: 'tok-2', from: 'Scout', data: { scoutRing: 'tok-2' } });
  emit('didDisplayIncomingCall', { callUUID: uuid, handle: 'Scout', fromPushKit: '1', payload: { call_id: 'tok-2' } });
  await act(async () => emit('endCall', { callUUID: uuid }));
  await waitFor(() => expect(mockDecline).toHaveBeenCalledWith(PAIRING, 'tok-2'));
  render(<CallPanel pairing={PAIRING} />);
  await act(async () => {});
  expect(mockSw.dial).not.toHaveBeenCalled();
});

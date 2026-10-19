import type { Call, CredentialProvider, SignalWireOptions } from '@signalwire/js';
import { SignalWireProvider, useCall, useSignalWire } from '@signalwire/react-native';
import { AudioRoutePickerButton, useAudioDevices, useAudioRoute } from '@signalwire/react-native/audio';
import type { AudioDeviceName, AudioRoute } from '@signalwire/react-native/audio';
import { getCallKit } from '@signalwire/react-native/callkit';
import type { CallKitObserverOptions } from '@signalwire/react-native/callkit';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Text } from 'react-native';
import RNCallKeep from '@signalwire/react-native-callkeep';

import { scoutCredentials } from './credentials';
import { takeCallRequests, usePendingCallRequests } from './startRequests';
import { takeRing, usePendingRings } from './ring';
import type { RingAnswer } from './ring';
import type { Pairing } from './pairing';
import { ScoutServer } from './server';
import type { MintedToken } from './server';
import { callVariables } from './sign';
import { useTheme } from './theme';
import { Button, ErrorText, Muted, Row, Section } from './ui';

// Outbound only, like the browser page: no registering to receive calls, and React
// Native has no device-change events to watch.
const SW_OPTIONS: SignalWireOptions = { skipRegister: true, skipDeviceMonitoring: true };
// CallKit shows the call (and Recents lists it) as "Scout", not the agent's address.
const CALLKIT: CallKitObserverOptions = { displayName: () => 'Scout' };
// How a call ends, as the CallKit bridge reads it: 'disconnected' is not an end, but what
// an ICE blip (Wi-Fi to cellular) looks like while the SDK recovers the media; a hangup
// from either side reaches 'destroyed'.
const ENDED = new Set(['ended', 'failed', 'destroyed']);
const HANGUP_WAIT_MS = 5000;
// How long an answered ring waits for SignalWire to connect (a cold start) before giving up.
const RING_CONNECT_MS = 15000;

interface Session {
  id: number;
  credentials: CredentialProvider;
  /** Answering Scout's call: the CallKit entry the user answered, and the ring's token. */
  ring?: RingAnswer;
}

/**
 * Calling Scout: Call gets a SignalWire token from Scout, then a SignalWire client is
 * built for this one call (and torn down after it), which dials the agent's address
 * with the signed userVariables. CallKit shows the call in the system UI.
 *
 * A call asked for from outside (Recents, Siri) is placed as soon as the panel is up.
 */
export function CallPanel({ pairing }: { pairing: Pairing }) {
  const [session, setSession] = useState<Session | null>(null);
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState('idle');
  const [error, setError] = useState('');
  const attempt = useRef(0);

  const start = useCallback(async (ring?: RingAnswer) => {
    if (busy || session) return;
    const mine = ++attempt.current;
    setBusy(true);
    setError('');
    setState(ring ? 'answering Scout: getting a token' : 'getting a token from Scout');
    try {
      const server = new ScoutServer(pairing.server, pairing);
      const first: MintedToken = await server.token();
      if (mine !== attempt.current) return;
      setSession({ id: mine, credentials: scoutCredentials(server, first), ring });
      setState('connecting');
    } catch (e) {
      // The answered call can't go anywhere: end it in CallKit rather than leave it up.
      if (ring) getCallKit().registry.endCall(ring.uuid, 'failed');
      if (mine !== attempt.current) return;
      setState('idle');
      setError((e as Error).message);
    } finally {
      if (mine === attempt.current) setBusy(false);
    }
  }, [busy, session, pairing]);

  const ended = useCallback((why?: string) => {
    attempt.current++;
    setSession(null);
    setBusy(false);
    setState('idle');
    if (why) setError(why);
  }, []);

  const requests = usePendingCallRequests();
  useEffect(() => {
    if (requests > 0 && takeCallRequests()) void start();
  }, [requests, start]);

  // Scout's call, answered in CallKit: placed as soon as nothing else is going on.
  const rings = usePendingRings();
  useEffect(() => {
    if (rings.length === 0 || busy || session) return;
    const ring = takeRing();
    if (ring) void start(ring);
  }, [rings, busy, session, start]);

  return (
    <Section title="Call Scout">
      <Text testID="state" style={{ color: useTheme().ink }}>
        State: {state}
      </Text>
      {session ? (
        <SignalWireProvider key={session.id} credentialProvider={session.credentials} options={SW_OPTIONS} callKit={CALLKIT}>
          <ActiveCall pairing={pairing} ring={session.ring} onState={setState} onEnded={ended} />
        </SignalWireProvider>
      ) : (
        <Row>
          <Button
            testID="call"
            title={busy ? 'Calling…' : 'Call Scout'}
            kind="go"
            big
            disabled={busy}
            onPress={() => void start()}
          />
          {busy ? <Button testID="cancel" title="Cancel" kind="stop" big onPress={() => ended()} /> : null}
        </Row>
      )}
      <ErrorText>{error}</ErrorText>
    </Section>
  );
}

function ActiveCall({
  pairing,
  ring,
  onState,
  onEnded,
}: {
  pairing: Pairing;
  ring?: RingAnswer;
  onState: (s: string) => void;
  onEnded: (why?: string) => void;
}) {
  const { isConnected, dial, error: clientError } = useSignalWire();
  const [call, setCall] = useState<Call | null>(null);
  const { status, isAudioMuted, setAudioMuted, hangup, error: callError } = useCall(call);
  const dialed = useRef(false);
  const gone = useRef(false);
  const hangupTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!isConnected || dialed.current) return;
    dialed.current = true;
    onState('dialing');
    const bridge = getCallKit();
    if (ring) {
      // The user may have ended it in CallKit while this connected.
      if (!bridge.registry.isLive(ring.uuid)) {
        onEnded();
        return;
      }
      // This call joins the CallKit entry the user already answered: claim it first, or
      // CallKit is told about a second, outgoing call and refuses it.
      bridge.beginBridgeDial(ring.uuid);
    }
    dial(pairing.address, {
      audio: true,
      video: false,
      // device, ts, nonce, sig: Scout's per-call check (docs/phone-design.md); ring: the
      // call answers Scout's ring.
      userVariables: { ...callVariables(pairing), ...(ring ? { ring: ring.ring } : {}) },
    })
      .finally(() => {
        if (ring) bridge.endBridgeDial();
      })
      .then(
        (placed) => {
          if (gone.current || (ring && !bridge.bindBridgeCall(ring.uuid, placed))) {
            // Hung up meanwhile (here, or the answered entry ended in CallKit).
            placed.hangup().catch(() => {});
            if (!gone.current) onEnded();
            return;
          }
          setCall(placed);
        },
        (e: unknown) => {
          if (ring) bridge.registry.endCall(ring.uuid, 'failed');
          onEnded(`Couldn't place the call: ${String((e as Error)?.message ?? e)}`);
        },
      );
  }, [isConnected, dial, pairing, ring, onState, onEnded]);

  // An answered ring whose client never connects (no network on a cold start) ends as failed.
  useEffect(() => {
    if (!ring || isConnected) return;
    const t = setTimeout(() => {
      if (dialed.current) return;
      getCallKit().registry.endCall(ring.uuid, 'failed');
      onEnded("Couldn't reach SignalWire to answer Scout's call.");
    }, RING_CONNECT_MS);
    return () => clearTimeout(t);
  }, [ring, isConnected, onEnded]);

  useEffect(() => {
    if (!call) return;
    onState(status);
    if (ENDED.has(status)) onEnded(status === 'failed' ? 'The call failed.' : undefined);
  }, [call, status, onState, onEnded]);

  useEffect(
    () => () => {
      gone.current = true;
      if (hangupTimer.current) clearTimeout(hangupTimer.current);
    },
    [],
  );

  const uuid = call ? getCallKit().registry.uuidForCall(call) : undefined;

  const end = useCallback(() => {
    if (!call) {
      gone.current = true;
      onEnded();
      return;
    }
    onState('hanging up');
    // Through CallKit when it knows the call, so the system UI ends it as ours.
    if (uuid) RNCallKeep.endCall(uuid);
    else void hangup().catch(() => {});
    hangupTimer.current = setTimeout(() => onEnded(), HANGUP_WAIT_MS); // the status normally ends it first
  }, [call, uuid, hangup, onEnded, onState]);

  // Muting through CallKit keeps the system call screen (and a headset's mute) in step;
  // CallKit then mutes the SDK's participant.
  const toggleMute = useCallback(() => {
    if (uuid) RNCallKeep.setMutedCall(uuid, !isAudioMuted);
    else void setAudioMuted(!isAudioMuted);
  }, [uuid, isAudioMuted, setAudioMuted]);

  const err = clientError?.message ?? (callError ? String((callError as { message?: string }).message ?? callError) : '');
  return (
    <>
      <Row>
        <Button testID="mute" title={isAudioMuted ? 'Unmute' : 'Mute'} kind={isAudioMuted ? 'on' : 'plain'} onPress={toggleMute} disabled={!call} />
        <Button testID="hangup" title="Hang up" kind="stop" onPress={end} />
      </Row>
      <RoutePicker active={status === 'connected'} />
      <ErrorText>{err}</ErrorText>
    </>
  );
}

const ROUTES: { route: AudioRoute; device: AudioDeviceName; title: string }[] = [
  { route: 'earpiece', device: 'EARPIECE', title: 'Phone' },
  { route: 'speaker', device: 'SPEAKER_PHONE', title: 'Speaker' },
  { route: 'bluetooth', device: 'BLUETOOTH', title: 'Bluetooth' },
];

/** Where the call plays: the phone, its speaker, or Bluetooth (a headset, the car). */
export function RoutePicker({ active }: { active: boolean }) {
  const { route, setRoute } = useAudioRoute();
  // What iOS is playing the call through (incall-manager's iOS routes, patches/).
  const devices = useAudioDevices();
  const known = devices.available.length > 0;
  const current = devices.selected || ROUTES.find((r) => r.route === route)?.device;
  return (
    <>
      <Row>
        {ROUTES.map((r) => (
          <Button
            key={r.route}
            testID={`route-${r.route}`}
            title={r.title}
            kind={current === r.device ? 'on' : 'plain'}
            disabled={!active || (known && !devices.available.includes(r.device))}
            onPress={() => setRoute(r.route)}
          />
        ))}
      </Row>
      <Row>
        {/* iOS's own output menu: every headset and car by name (nothing where unavailable). */}
        <AudioRoutePickerButton testID="route-menu" />
        {active && devices.selectedName ? <Muted>Playing through {devices.selectedName}</Muted> : null}
      </Row>
    </>
  );
}

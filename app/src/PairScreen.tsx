import { useEffect, useRef, useState } from 'react';
import { Text } from 'react-native';

import { browseScout } from './discovery';
import type { Browse, Found } from './discovery';
import { parseCode, parsePairLink, savePairing } from './pairing';
import type { PairLink, Pairing } from './pairing';
import { ADDRESS_EXAMPLE, hostOf, normalizeServer, ScoutServer } from './server';
import type { Paired } from './server';
import { useTheme } from './theme';
import { Button, ErrorText, Field, Muted, Row, Section } from './ui';

type Server = Pick<ScoutServer, 'pairWithCode' | 'requestPairing' | 'pairStatus'>;

/**
 * Pair this phone with Scout, without its web token. Three ways, all ending in this
 * phone's own device secret:
 * - a scout://pair link (the iPhone Camera scanned the QR code from `scoutctl phone pair`
 *   or Scout's /phone page): "Pair with <host>?" and one tap;
 * - Scout found on the local network (Bonjour), or its address typed in: the app asks,
 *   and Scout asks the room out loud, with the same two-digit number on both;
 * - the address and the code typed in (or the link pasted).
 */
export function PairScreen({
  link,
  onPaired,
  onDismissLink = () => {},
  autoPair = false,
  makeServer = (base) => new ScoutServer(base),
  browse = browseScout,
}: {
  link: PairLink | null;
  onPaired: (p: Pairing) => void;
  onDismissLink?: () => void;
  /** Pair as soon as a link arrives (only for driving the Simulator; see testFlags). */
  autoPair?: boolean;
  makeServer?: (base: string) => Server;
  browse?: Browse;
}) {
  const t = useTheme();
  const [server, setServer] = useState('');
  const [code, setCode] = useState('');
  const [name, setName] = useState('iPhone');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [found, setFound] = useState<Found[]>([]);
  const [asking, setAsking] = useState<{ match: string; host: string } | null>(null);
  const askId = useRef(0);

  useEffect(() => browse(setFound), [browse]);

  useEffect(() => {
    setError('');
    if (link && autoPair) void pairWithCode(link.server, link.code);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [link]);

  function phoneName(): string | null {
    const n = name.trim();
    if (!n) setError('Give this phone a name.');
    return n || null;
  }

  async function finish(base: string, n: string, p: Paired) {
    const pairing: Pairing = { server: base, name: n, ...p };
    await savePairing(pairing);
    onPaired(pairing);
  }

  async function pairWithCode(base: string, c: string) {
    setError('');
    const n = phoneName();
    if (!n) return;
    setBusy(true);
    try {
      await finish(base, n, await makeServer(base).pairWithCode(c, n));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  /** The manual form: an address and a code, or a pasted scout:// link in either field. */
  async function pairTyped() {
    setError('');
    const pasted = parsePairLink(code) ?? parsePairLink(server);
    let base: string;
    try {
      base = normalizeServer(pasted?.server ?? server);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    const c = pasted?.code ?? parseCode(code);
    if (!c) {
      setError('Type the code Scout shows, like ABCD-EFGH (or paste the whole scout:// link).');
      return;
    }
    await pairWithCode(base, c);
  }

  /** Ask Scout to pair; the room says yes (or no). */
  async function ask(typed: string) {
    setError('');
    let base: string;
    try {
      base = normalizeServer(typed);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    const n = phoneName();
    if (!n) return;
    const mine = ++askId.current;
    setBusy(true);
    try {
      const s = makeServer(base);
      const r = await s.requestPairing(n);
      if (mine !== askId.current) return;
      setAsking({ match: r.match, host: hostOf(base) });
      for (;;) {
        const st = await s.pairStatus(r.request, 20);
        if (mine !== askId.current) return;
        if (st.status === 'waiting') continue;
        if (st.status === 'approved') {
          const { status: _, ...p } = st;
          await finish(base, n, p);
          return;
        }
        if (st.status === 'denied') throw new Error('Scout heard no: someone said no in the room, or tapped Deny.');
        if (st.status === 'expired') throw new Error('Nobody said yes in time. Try again, and answer when Scout asks.');
        if (st.status === 'error') throw new Error(st.error || "Scout couldn't pair this phone.");
        throw new Error('Scout lost track of that request; ask again.');
      }
    } catch (e) {
      if (mine === askId.current) setError((e as Error).message);
    } finally {
      if (mine === askId.current) {
        setBusy(false);
        setAsking(null);
      }
    }
  }

  function cancelAsk() {
    askId.current++;
    setBusy(false);
    setAsking(null);
  }

  const nameField = <Field label="Name for this phone" testID="name" value={name} onChangeText={setName} maxLength={64} />;

  if (link) {
    return (
      <Section title={`Pair with ${hostOf(link.server)}?`}>
        <Muted>
          Scout at {link.server} gave this phone a one-time code. Pairing gives this phone its own secret, which signs
          every call; it stays in this phone&apos;s Keychain.
        </Muted>
        {nameField}
        <Row>
          <Button testID="not-now" title="Not now" onPress={onDismissLink} disabled={busy} />
          <Button
            testID="pair"
            title={busy ? 'Pairing…' : 'Pair'}
            kind="go"
            onPress={() => pairWithCode(link.server, link.code)}
            disabled={busy}
          />
        </Row>
        <ErrorText>{error}</ErrorText>
      </Section>
    );
  }

  if (asking) {
    return (
      <Section title={`Asking ${asking.host}`}>
        <Text testID="match" style={{ color: t.ink, fontSize: 44, fontWeight: '700', textAlign: 'center' }}>
          {asking.match}
        </Text>
        <Muted>
          Say yes when Scout asks out loud (or tap Approve on Scout&apos;s web page). Scout says the same number: if
          it&apos;s not {asking.match}, say no.
        </Muted>
        <Row>
          <Button testID="cancel" title="Cancel" onPress={cancelAsk} />
        </Row>
      </Section>
    );
  }

  return (
    <>
      {found.length > 0 ? (
        <Section title="Found on your network">
          {found.map((f, i) => (
            <Row key={f.key}>
              <Text style={{ color: t.ink, flex: 2, alignSelf: 'center', fontSize: 16 }}>Found {f.label}</Text>
              <Button testID={`found-${i}`} title="Pair" kind="go" onPress={() => ask(f.server)} disabled={busy} />
            </Row>
          ))}
          <Muted>Scout asks out loud in the room, and shows a two-digit number on this phone and on the Mac.</Muted>
        </Section>
      ) : null}
      <Section title="Pair with Scout">
        <Muted>
          Quickest: on the Mac, show a pairing QR code, scan it with the iPhone Camera and tap the Scout banner. Turn
          on Tailscale on this phone first.
        </Muted>
        <Field
          label="Scout's address"
          testID="server"
          value={server}
          onChangeText={setServer}
          placeholder={ADDRESS_EXAMPLE}
          keyboardType="url"
          textContentType="URL"
        />
        <Muted>
          Your Mac&apos;s Tailscale address, e.g. {ADDRESS_EXAMPLE} — or scan the QR from `scoutctl phone pair`.
        </Muted>
        <Field
          label="Pairing code"
          testID="code"
          value={code}
          onChangeText={setCode}
          placeholder="ABCD-EFGH, or paste the scout:// link"
          autoCapitalize="characters"
        />
        {nameField}
        <Row>
          <Button testID="pair-code" title={busy ? 'Pairing…' : 'Pair with code'} kind="go" onPress={pairTyped} disabled={busy} />
        </Row>
        <Row>
          <Button testID="ask" title="No code? Ask Scout to approve" onPress={() => ask(server)} disabled={busy} />
        </Row>
        <ErrorText>{error}</ErrorText>
      </Section>
    </>
  );
}

import { useEffect, useState } from 'react';

import { savePairing } from './pairing';
import type { PairLink, Pairing } from './pairing';
import { normalizeServer, ScoutServer } from './server';
import { Button, ErrorText, Field, Muted, Row, Section } from './ui';

/**
 * Pair this phone with Scout: the same request as the /phone page's "Pair this browser"
 * (POST /api/phone/pair with Scout's web token). Scout's address and token are typed in,
 * or filled from a scout://pair link.
 */
export function PairScreen({
  link,
  onPaired,
  autoPair = false,
  makeServer = (base, token) => new ScoutServer(base, token),
}: {
  link: PairLink | null;
  onPaired: (p: Pairing) => void;
  /** Pair as soon as a link arrives (only for driving the Simulator; see testFlags). */
  autoPair?: boolean;
  makeServer?: (base: string, token: string) => Pick<ScoutServer, 'pair'>;
}) {
  const [server, setServer] = useState(link?.server ?? '');
  const [token, setToken] = useState(link?.accessToken ?? '');
  const [name, setName] = useState('iPhone');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (link) {
      setServer(link.server);
      setToken(link.accessToken);
      if (autoPair) void pair(link.server, link.accessToken, name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [link]);

  async function pair(server: string, token: string, name: string) {
    setError('');
    let base: string;
    try {
      base = normalizeServer(server);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    const n = name.trim();
    if (!token.trim()) return setError("Paste Scout's access token (the contents of state/web_token).");
    if (!n) return setError('Give this phone a name.');
    setBusy(true);
    try {
      const p = await makeServer(base, token.trim()).pair(n);
      const pairing: Pairing = { server: base, accessToken: token.trim(), name: n, ...p };
      await savePairing(pairing);
      onPaired(pairing);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Pair with Scout">
      <Muted>
        Once, from somewhere this phone reaches Scout over Tailscale. Scout gives this phone its own secret, which
        signs every call; it stays in this phone&apos;s Keychain.
      </Muted>
      <Field
        label="Scout's address"
        testID="server"
        value={server}
        onChangeText={setServer}
        placeholder="https://mac.tailnet.ts.net"
        keyboardType="url"
        textContentType="URL"
      />
      <Field
        label="Access token (Scout's state/web_token)"
        testID="token"
        value={token}
        onChangeText={setToken}
        placeholder="paste it here"
        secureTextEntry
      />
      <Field label="Name for this phone" testID="name" value={name} onChangeText={setName} maxLength={64} />
      <Row>
        <Button testID="pair" title={busy ? 'Pairing…' : 'Pair'} kind="go" onPress={() => pair(server, token, name)} disabled={busy} />
      </Row>
      <ErrorText>{error}</ErrorText>
    </Section>
  );
}

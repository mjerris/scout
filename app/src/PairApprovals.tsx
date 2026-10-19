// Other devices asking to pair with Scout, shown on this (already paired) phone while
// the app is open, for an Approve or Deny. Scout asks every channel at once (the room,
// its web UI, every paired phone, an alert on the Mac, its CLI); the first answer wins,
// and a request answered elsewhere disappears here with a note saying so. Scout has
// cleaned every detail it sends (the requester chooses its own name); it is shown as
// plain text.
//
// There is no push: the app long-polls Scout while it is in the foreground (each request
// signed with this phone's secret), so a closed app doesn't see a request.
import { useCallback, useEffect, useRef, useState } from 'react';
import { AppState, Text } from 'react-native';

import type { Pairing } from './pairing';
import { ScoutServer } from './server';
import type { PairAnswer, PendingPair } from './server';
import { useTheme } from './theme';
import { Button, Muted, Row, Section } from './ui';

export type PairReviewer = Pick<ScoutServer, 'pairPending' | 'pairDecide'>;

const WAIT_S = 20;
const RETRY_MS = 5000;

export function describe(p: PendingPair): string[] {
  const lines = [
    p.name
      ? `A new device calling itself "${p.name}" wants to pair with Scout.`
      : 'A new device with no usable name wants to pair with Scout.',
    `It shows the code ${p.match}.`,
    `Tailscale: ${p.login ? `${p.login}${p.owner ? ' (yours)' : ''}` : 'login not verified'}`,
  ];
  if (p.peer) lines.push(`Device: ${p.peer}`);
  if (p.ip) lines.push(`Address: ${p.ip}`);
  if (p.agent) lines.push(`App: ${p.agent}`);
  if (p.at) lines.push(`Asked at ${new Date(p.at * 1000).toLocaleTimeString()}`);
  return lines;
}

function outcome(a: PairAnswer): string {
  switch (a.status) {
    case 'approved':
      return `Approved: paired as ${a.device}.`;
    case 'denied':
      return 'Denied.';
    case 'already':
      return `Already answered (${a.result}${a.via ? ` by ${a.via}` : ''}).`;
    case 'unknown':
      return 'That request is gone (it expired or was answered).';
    default:
      return a.error ?? "Scout couldn't record that.";
  }
}

export function PairApprovals({ pairing, server }: { pairing: Pairing; server?: PairReviewer }) {
  const t = useTheme();
  const [requests, setRequests] = useState<PendingPair[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [active, setActive] = useState(AppState.currentState !== 'background');
  const reviewer = useRef<PairReviewer>(server ?? new ScoutServer(pairing.server, pairing));
  const shown = useRef<Set<string>>(new Set());
  const answered = useRef<Set<string>>(new Set());

  const note = useCallback((text: string) => setNotes((n) => [...n.slice(-2), text]), []);

  useEffect(() => {
    const sub = AppState.addEventListener('change', (s) => setActive(s === 'active'));
    return () => sub.remove();
  }, []);

  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let wake: (() => void) | undefined;
    const pause = (ms: number) =>
      new Promise<void>((resolve) => {
        wake = resolve;
        timer = setTimeout(resolve, ms);
      });
    (async () => {
      let version = -1;
      while (!stopped) {
        try {
          const got = await reviewer.current.pairPending(version, version < 0 ? 0 : WAIT_S);
          if (stopped) return;
          version = got.version;
          const now = new Set(got.requests.map((r) => r.id));
          for (const id of shown.current) {
            if (!now.has(id) && !answered.current.has(id)) note(`Request ${id} was answered elsewhere, or expired.`);
          }
          shown.current = now;
          setRequests(got.requests);
        } catch {
          if (stopped) return;
          await pause(RETRY_MS);
        }
      }
    })();
    return () => {
      stopped = true;
      clearTimeout(timer);
      wake?.();
    };
  }, [active, note]);

  const decide = useCallback(
    async (id: string, approve: boolean) => {
      setBusy(id);
      answered.current.add(id);
      try {
        note(outcome(await reviewer.current.pairDecide(id, approve)));
        setRequests((rs) => rs.filter((r) => r.id !== id));
      } catch (e) {
        answered.current.delete(id);
        note(`Couldn't send that: ${String((e as Error)?.message ?? e)}`);
      } finally {
        setBusy(null);
      }
    },
    [note],
  );

  if (!requests.length && !notes.length) return null;
  return (
    <Section title="Asking to pair">
      {requests.map((r) => (
        <Section key={r.id}>
          {describe(r).map((line, i) => (
            <Text key={i} style={{ color: i === 0 ? t.ink : t.muted }} testID={i === 0 ? `pair-text-${r.id}` : undefined}>
              {line}
            </Text>
          ))}
          <Row>
            <Button
              testID={`pair-approve-${r.id}`}
              title="Approve"
              kind="go"
              disabled={busy !== null}
              onPress={() => void decide(r.id, true)}
            />
            <Button
              testID={`pair-deny-${r.id}`}
              title="Deny"
              kind="stop"
              disabled={busy !== null}
              onPress={() => void decide(r.id, false)}
            />
          </Row>
        </Section>
      ))}
      {notes.map((n, i) => (
        <Muted key={i}>{n}</Muted>
      ))}
    </Section>
  );
}

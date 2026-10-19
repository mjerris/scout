// Scout's web server, as the app uses it (src/scout/web.py). Pairing needs no web token:
// a one-time code from a scout://pair link (POST /api/phone/pair {code, name}), or a
// request the room approves by voice (/api/phone/pair/request, then .../status). After
// that the app signs its SignalWire token requests with its device secret, and (while it
// is open) shows other devices asking to pair, for an Approve or Deny signed the same way. It reaches the
// Mac at its Tailscale name ([web] public_url, else the Tailscale Serve name,
// https://<mac>.<tailnet>.ts.net).
import { PAIR_REVIEW, pairDecidePurpose, pushRegisterPurpose, ringDeclinePurpose, signedFor, tokenRequest } from './sign';

export class ServerError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ServerError';
  }
}

export const ADDRESS_EXAMPLE = 'https://your-mac.your-tailnet.ts.net';

/**
 * The server's base URL, from what the user typed or a pairing link: https only (the
 * device secret is handed over at pairing), except plain http to this device itself
 * (localhost, for the Simulator). A bare host name gets https://. Paths are dropped.
 */
export function normalizeServer(input: string): string {
  let text = input.trim();
  if (!text) throw new Error(`Enter your Mac's Tailscale address, e.g. ${ADDRESS_EXAMPLE}`);
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) text = `https://${text}`;
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    throw new Error(`"${input.trim()}" is not an address`);
  }
  const host = url.hostname.toLowerCase();
  const local = host === 'localhost' || host === '127.0.0.1' || host === '[::1]';
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) {
    throw new Error("Scout's address must start with https:// (the Tailscale name)");
  }
  if (!host) throw new Error(`"${input.trim()}" has no host name`);
  return `${url.protocol}//${url.host}`;
}

/** The host name of a server URL, for "Pair with <host>?". */
export function hostOf(server: string): string {
  try {
    return new URL(server).host;
  } catch {
    return server;
  }
}

export interface Paired {
  device: string;
  secret: string;
  address: string;
}

export interface MintedToken {
  token: string;
  expires_at: number;
}

export interface PairRequest {
  request: string;
  match: string;
  expires_at: number;
}

/** A device asking to pair, as Scout shows it (every part cleaned by Scout). */
export interface PendingPair {
  id: string;
  name: string;
  match: string;
  login: string;
  owner: boolean;
  peer: string;
  ip: string;
  agent: string;
  at: number;
  expires_at: number;
}

export type PairAnswer =
  | { status: 'approved'; device: string }
  | { status: 'denied' }
  | { status: 'already'; result: string; via: string }
  | { status: 'unknown' | 'error'; error?: string };

export type PairStatus =
  | ({ status: 'approved' } & Paired)
  | { status: 'waiting'; match?: string }
  | { status: 'denied' | 'expired' | 'unknown' }
  | { status: 'error'; error?: string };

/** Who this phone is, for signed requests (and the web token, from a pairing made before codes). */
export interface Identity {
  device: string;
  secret: string;
  accessToken?: string;
}

type Fetch = typeof fetch;

export class ScoutServer {
  constructor(
    readonly base: string,
    private readonly me: Identity | null = null,
    private readonly fetchImpl: Fetch = (...args) => fetch(...args),
  ) {}

  private async post(path: string, body: unknown, timeoutMs = 20000): Promise<unknown> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const headers: Record<string, string> = { 'Content-Type': 'application/json', Accept: 'application/json' };
    if (this.me?.accessToken) headers.Authorization = `Bearer ${this.me.accessToken}`;
    let r: Response;
    try {
      r = await this.fetchImpl(`${this.base}${path}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body ?? {}),
        signal: controller.signal,
      });
    } catch (e) {
      const why = controller.signal.aborted ? 'it did not answer in time' : String((e as Error)?.message ?? e);
      throw new ServerError(
        `Couldn't reach Scout at ${this.base} (${why}). Turn on Tailscale on this phone, then try again.`,
        0,
      );
    } finally {
      clearTimeout(timer);
    }
    let data: unknown = null;
    try {
      data = await r.json();
    } catch {
      data = null;
    }
    if (!r.ok) {
      const err = data && typeof data === 'object' ? (data as { error?: unknown }).error : undefined;
      if (typeof err === 'string' && err) throw new ServerError(err, r.status);
      const why =
        r.status === 401
          ? path === '/api/phone/token'
            ? "Scout doesn't know this phone any more (it was forgotten): pair it again."
            : "Scout didn't accept that."
          : '';
      throw new ServerError(`${path} failed (HTTP ${r.status})${why ? `: ${why}` : ''}`, r.status);
    }
    return data;
  }

  private static paired(d: Partial<Paired> | null, what: string): Paired {
    if (!d || typeof d.device !== 'string' || typeof d.secret !== 'string' || typeof d.address !== 'string') {
      throw new ServerError(`Scout answered the ${what} with something unexpected`, 502);
    }
    return { device: d.device, secret: d.secret, address: d.address };
  }

  /** Pair with a one-time code (from a scout://pair link): the secret comes back this once. */
  async pairWithCode(code: string, name: string): Promise<Paired> {
    return ScoutServer.paired((await this.post('/api/phone/pair', { code, name })) as Partial<Paired> | null, 'pairing');
  }

  /**
   * Ask to pair without a code: Scout asks for a yes (the room, the web UI, a paired
   * phone, the Mac's alert, or its CLI), with `match` shown on both.
   */
  async requestPairing(name: string): Promise<PairRequest> {
    const d = (await this.post('/api/phone/pair/request', { name })) as Partial<PairRequest> | null;
    if (!d || typeof d.request !== 'string' || typeof d.match !== 'string') {
      throw new ServerError('Scout answered the pairing request with something unexpected', 502);
    }
    return { request: d.request, match: d.match, expires_at: typeof d.expires_at === 'number' ? d.expires_at : 0 };
  }

  /** Where a pairing request stands; Scout holds the answer up to `wait` seconds. */
  async pairStatus(request: string, wait = 20): Promise<PairStatus> {
    const d = (await this.post('/api/phone/pair/status', { request, wait }, (wait + 15) * 1000)) as {
      status?: unknown;
    } | null;
    if (d && d.status === 'approved') return { status: 'approved', ...ScoutServer.paired(d as Partial<Paired>, 'pairing') };
    if (d && typeof d.status === 'string') return d as PairStatus;
    throw new ServerError('Scout answered the pairing request with something unexpected', 502);
  }

  /** A short-lived SignalWire subscriber token, minted by Scout for this call (signed by this phone). */
  async token(): Promise<MintedToken> {
    if (!this.me) throw new ServerError('This phone is not paired', 401);
    const d = (await this.post('/api/phone/token', tokenRequest(this.me))) as Partial<MintedToken> | null;
    if (!d || typeof d.token !== 'string' || !d.token) {
      throw new ServerError('Scout answered the token request with something unexpected', 502);
    }
    return { token: d.token, expires_at: typeof d.expires_at === 'number' ? d.expires_at : 0 };
  }

  /**
   * The devices asking to pair now (signed by this phone). Scout holds the answer up to
   * `wait` seconds for a change after `since` (the `version` it last returned).
   */
  async pairPending(since = -1, wait = 0): Promise<{ requests: PendingPair[]; version: number }> {
    if (!this.me) throw new ServerError('This phone is not paired', 401);
    const body = { ...signedFor(this.me, PAIR_REVIEW), since, wait };
    const d = (await this.post('/api/phone/pair/pending', body, (wait + 15) * 1000)) as {
      requests?: unknown;
      version?: unknown;
    } | null;
    if (!d || !Array.isArray(d.requests) || typeof d.version !== 'number') {
      throw new ServerError('Scout answered with something unexpected', 502);
    }
    const requests = d.requests.filter(
      (r): r is PendingPair => !!r && typeof r === 'object' && typeof (r as PendingPair).id === 'string',
    );
    return { requests, version: d.version };
  }

  /** Approve or deny a device asking to pair (signed by this phone for exactly this answer). */
  async pairDecide(id: string, approve: boolean): Promise<PairAnswer> {
    if (!this.me) throw new ServerError('This phone is not paired', 401);
    const body = { ...signedFor(this.me, pairDecidePurpose(id, approve)), id, approve };
    let d: unknown;
    try {
      d = await this.post('/api/phone/pair/decide', body);
    } catch (e) {
      if (e instanceof ServerError && e.status === 404) return { status: 'unknown' };
      throw e;
    }
    if (d && typeof d === 'object' && typeof (d as { status?: unknown }).status === 'string') return d as PairAnswer;
    throw new ServerError('Scout answered with something unexpected', 502);
  }

  /**
   * Register this phone's push tokens with Scout (signed for exactly these tokens): the
   * PushKit token Scout rings for its calls, and the APNs token for notifications
   * (pairing requests). Empty strings unregister. `env` is the APNs environment the build
   * is signed for.
   */
  async registerPush(voip: string, alert: string, env: 'sandbox' | 'production'): Promise<{ push: boolean }> {
    if (!this.me) throw new ServerError('This phone is not paired', 401);
    const d = (await this.post('/api/phone/push', {
      ...signedFor(this.me, pushRegisterPurpose(voip, alert, env)),
      voip,
      alert,
      env,
    })) as { push?: unknown } | null;
    // Whether Scout can send pushes at all (an APNs key is set up there).
    return { push: d?.push === true };
  }

  /** Tell Scout the user declined its call (signed for exactly this ring). */
  async declineRing(ring: string): Promise<string> {
    if (!this.me) throw new ServerError('This phone is not paired', 401);
    try {
      const d = (await this.post('/api/phone/ring/decline', { ...signedFor(this.me, ringDeclinePurpose(ring)), ring })) as {
        status?: unknown;
      } | null;
      return typeof d?.status === 'string' ? d.status : 'declined';
    } catch (e) {
      // 404: Scout doesn't know that ring; 409: it was answered, declined or expired already.
      if (e instanceof ServerError && (e.status === 404 || e.status === 409)) return e.status === 404 ? 'unknown' : 'already';
      throw e;
    }
  }
}

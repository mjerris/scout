// Scout's web server, as the app uses it (src/scout/web.py). Pairing needs no web token:
// a one-time code from a scout://pair link (POST /api/phone/pair {code, name}), or a
// request the room approves by voice (/api/phone/pair/request, then .../status). After
// that the app signs its SignalWire token requests with its device secret. It reaches the
// Mac at its Tailscale name ([web] public_url, else the Tailscale Serve name,
// https://<mac>.<tailnet>.ts.net).
import { tokenRequest } from './sign';

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
   * Ask to pair without a code: Scout asks the room, with `match` shown on both. From the
   * owner's own Tailscale login ([phone] pair_mode = "tailnet") it pairs at once.
   */
  async requestPairing(name: string): Promise<PairRequest | ({ status: 'approved' } & Paired)> {
    const d = (await this.post('/api/phone/pair/request', { name })) as (Partial<PairRequest> & { status?: unknown }) | null;
    if (d && d.status === 'approved') return { status: 'approved', ...ScoutServer.paired(d as Partial<Paired>, 'pairing') };
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
}

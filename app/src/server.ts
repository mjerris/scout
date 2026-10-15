// Scout's web server, as the app uses it: the same endpoints and auth as the browser's
// /phone page (src/scout/web.py). The app holds Scout's web token (state/web_token) and
// sends it as a Bearer token; it reaches the Mac at its Tailscale name ([web] public_url,
// else the Tailscale Serve name, https://<mac>.<tailnet>.ts.net).

export class ServerError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ServerError';
  }
}

/**
 * The server's base URL, from what the user typed or a pairing link: https only (the
 * token travels with every request), except plain http to this device itself
 * (localhost, for the Simulator). A bare host name gets https://. Paths are dropped.
 */
export function normalizeServer(input: string): string {
  let text = input.trim();
  if (!text) throw new Error("Enter Scout's address, e.g. https://mac.tailnet.ts.net");
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

export interface Paired {
  device: string;
  secret: string;
  address: string;
}

export interface MintedToken {
  token: string;
  expires_at: number;
}

type Fetch = typeof fetch;

export class ScoutServer {
  constructor(
    readonly base: string,
    private readonly accessToken: string,
    private readonly fetchImpl: Fetch = (...args) => fetch(...args),
  ) {}

  private async post(path: string, body: unknown, timeoutMs = 20000): Promise<unknown> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let r: Response;
    try {
      r = await this.fetchImpl(`${this.base}${path}`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${this.accessToken}`,
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        body: JSON.stringify(body ?? {}),
        signal: controller.signal,
      });
    } catch (e) {
      const why = controller.signal.aborted ? 'it did not answer in time' : String((e as Error)?.message ?? e);
      throw new ServerError(`Couldn't reach Scout at ${this.base}: ${why}. Is Tailscale on?`, 0);
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
      const why =
        typeof err === 'string' && err
          ? err
          : r.status === 401
            ? "Scout didn't accept the access token (the contents of state/web_token)."
            : '';
      throw new ServerError(`${path} failed (HTTP ${r.status})${why ? `: ${why}` : ''}`, r.status);
    }
    return data;
  }

  /** Pair this phone, like the /phone page's "Pair this browser": the secret comes back this once. */
  async pair(name: string): Promise<Paired> {
    const d = (await this.post('/api/phone/pair', { name })) as Partial<Paired> | null;
    if (!d || typeof d.device !== 'string' || typeof d.secret !== 'string' || typeof d.address !== 'string') {
      throw new ServerError('Scout answered the pairing with something unexpected', 502);
    }
    return { device: d.device, secret: d.secret, address: d.address };
  }

  /** A short-lived SignalWire subscriber token, minted by Scout for this call. */
  async token(): Promise<MintedToken> {
    const d = (await this.post('/api/phone/token', {})) as Partial<MintedToken> | null;
    if (!d || typeof d.token !== 'string' || !d.token) {
      throw new ServerError('Scout answered the token request with something unexpected', 502);
    }
    return { token: d.token, expires_at: typeof d.expires_at === 'number' ? d.expires_at : 0 };
  }
}

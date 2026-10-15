import type { CredentialProvider, SDKCredential } from '@signalwire/js';

import type { MintedToken, ScoutServer } from './server';

function asCredential(t: MintedToken): SDKCredential {
  // Scout gives seconds; the SDK wants milliseconds. With expiry_at set, the SDK calls
  // refresh() before the token runs out, so a long call survives Scout's one-hour token.
  return t.expires_at > 0 ? { token: t.token, expiry_at: t.expires_at * 1000 } : { token: t.token };
}

/**
 * SignalWire credentials for one call: the token Scout minted when Call was pressed
 * (fetched first, so "phone calls are off" shows before anything connects), then fresh
 * ones from Scout on refresh or reconnect.
 */
export function scoutCredentials(server: Pick<ScoutServer, 'token'>, first: MintedToken): CredentialProvider {
  let pending: MintedToken | null = first;
  return {
    authenticate: async () => {
      const t = pending ?? (await server.token());
      pending = null;
      return asCredential(t);
    },
    refresh: async () => asCredential(await server.token()),
  };
}

import { useCallback, useContext } from 'react';

import { SignalWireContext } from './SignalWireProvider';
import { useObservable } from './useObservable';

import type { SignalWireContextValue } from './SignalWireProvider';
import type { Call, DialOptions, Directory, SignalWire, User } from '@signalwire/js';

export interface UseSignalWireResult {
  readonly client: SignalWire | null;
  readonly isConnected: boolean;
  readonly isRegistered: boolean;
  readonly user: User | undefined;
  readonly directory: Directory | undefined;
  readonly error: Error | null;
  dial(destination: string, options?: DialOptions): Promise<Call>;
  disconnect(): Promise<void>;
}

function useSignalWireContext(): SignalWireContextValue {
  const context = useContext(SignalWireContext);
  if (!context) {
    throw new Error(
      'useSignalWire must be used inside a <SignalWireProvider>. ' +
        'Wrap your app root with <SignalWireProvider credentialProvider={...}>.'
    );
  }
  return context;
}

/** Client-level state and actions. */
export function useSignalWire(): UseSignalWireResult {
  const { client, error, observer } = useSignalWireContext();

  const isConnected = useObservable(client?.isConnected$, client?.isConnected ?? false);
  const isRegistered = useObservable(client?.isRegistered$, client?.isRegistered ?? false);
  const user = useObservable(client?.user$, client?.user);
  const directory = useObservable(client?.directory$, client?.directory);

  const dial = useCallback(
    async (destination: string, options?: DialOptions): Promise<Call> => {
      if (!client) {
        throw new Error('SignalWire client is not ready yet.');
      }
      const call = await client.dial(destination, options);
      // The core does not know what an observer does with this — on React
      // Native it registers the call with CallKit; on the web nobody listens.
      observer?.onOutgoingCall?.(call, destination);
      return call;
    },
    [client, observer]
  );

  const disconnect = useCallback((): Promise<void> => {
    if (!client) {
      return Promise.resolve();
    }
    return client.disconnect();
  }, [client]);

  return { client, isConnected, isRegistered, user, directory, error, dial, disconnect };
}

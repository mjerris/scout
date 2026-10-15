import { logger } from '@signalwire/react';

import type { Storage } from '@signalwire/js';

/**
 * The SDK exports `Storage` but not `StorageScope`, so redeclare it.
 * Must stay identical to `dependencies/interfaces.ts` in the SDK.
 */
export type StorageScope = 'local' | 'session';

/** The subset of AsyncStorage this adapter uses. */
export interface AsyncStorageLike {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
  getAllKeys(): Promise<readonly string[]>;
  multiRemove(keys: readonly string[]): Promise<void>;
}

const NAMESPACE = 'sw-rn';

const prefixFor = (scope: StorageScope): string => `${NAMESPACE}:${scope}:`;
const keyFor = (key: string, scope: StorageScope): string => `${prefixFor(scope)}${key}`;

/**
 * Guards the once-per-process session purge. React Native has no session
 * scope, so an app cold start is the equivalent of opening a new browser tab.
 */
let sessionPurge: Promise<void> | null = null;

/** Test seam — clears the once-per-process purge guard. */
export function resetSessionPurgeForTesting(): void {
  sessionPurge = null;
}

async function purgeScope(backing: AsyncStorageLike, scope: StorageScope): Promise<void> {
  const prefix = prefixFor(scope);
  const keys = await backing.getAllKeys();
  const matching = keys.filter((key) => key.startsWith(prefix));
  if (matching.length > 0) {
    await backing.multiRemove(matching);
  }
}

/** SDK `Storage` backed by AsyncStorage, with an emulated session scope. */
export class ReactNativeStorage implements Storage {
  constructor(private readonly backing: AsyncStorageLike) {
    sessionPurge ??= purgeScope(backing, 'session').catch((error: unknown) => {
      logger.warn('Failed to purge session-scoped storage:', error);
    });
  }

  private async ready(): Promise<void> {
    await sessionPurge;
  }

  async setItem(key: string, value: string | null, scope: StorageScope = 'session'): Promise<void> {
    await this.ready();
    try {
      if (value === null) {
        await this.backing.removeItem(keyFor(key, scope));
        return;
      }
      await this.backing.setItem(keyFor(key, scope), value);
    } catch (error) {
      logger.warn(`Failed to write "${key}" (${scope}):`, error);
    }
  }

  async getItem(key: string, scope: StorageScope = 'session'): Promise<string | null> {
    await this.ready();
    try {
      return await this.backing.getItem(keyFor(key, scope));
    } catch (error) {
      logger.warn(`Failed to read "${key}" (${scope}):`, error);
      return null;
    }
  }

  async removeItem(key: string, scope: StorageScope = 'session'): Promise<void> {
    await this.ready();
    try {
      await this.backing.removeItem(keyFor(key, scope));
    } catch (error) {
      logger.warn(`Failed to remove "${key}" (${scope}):`, error);
    }
  }

  async clear(scope: StorageScope = 'session'): Promise<void> {
    await this.ready();
    try {
      await purgeScope(this.backing, scope);
    } catch (error) {
      logger.warn(`Failed to clear scope "${scope}":`, error);
    }
  }
}

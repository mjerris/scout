import { getLogger } from '@signalwire/js';

const PREFIX = '[SignalWireRN]';

/** Logger that routes through the SDK's logger so app-level log config applies. */
export const logger = {
  debug: (...args: unknown[]): void => getLogger().debug(PREFIX, ...args),
  info: (...args: unknown[]): void => getLogger().info(PREFIX, ...args),
  warn: (...args: unknown[]): void => getLogger().warn(PREFIX, ...args),
  error: (...args: unknown[]): void => getLogger().error(PREFIX, ...args)
};

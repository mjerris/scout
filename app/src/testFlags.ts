// Launch arguments for driving the app in the Simulator, where nothing can tap:
//   xcrun simctl launch <device> <bundle id> -ScoutTestLink 'scout://pair?...' -ScoutTestPair 1 -ScoutTestCall 1
// iOS puts "-key value" launch arguments in NSUserDefaults' argument domain, which
// React Native's Settings reads. Nobody can pass launch arguments to an app on a phone,
// so these do nothing there.
import { Platform, Settings } from 'react-native';

function flag(name: string): boolean {
  if (Platform.OS !== 'ios') return false;
  try {
    const v: unknown = Settings.get(name);
    return v === 1 || v === '1' || v === true || v === 'YES';
  } catch {
    return false;
  }
}

function text(name: string): string {
  if (Platform.OS !== 'ios') return '';
  try {
    const v: unknown = Settings.get(name);
    return typeof v === 'string' ? v : '';
  } catch {
    return '';
  }
}

export const testFlags = {
  /** A scout://pair link, as if opened (simctl openurl asks "Open in Scout?", which nothing can tap). */
  link: text('ScoutTestLink'),
  /** Pair as soon as a scout://pair link arrives. */
  pair: flag('ScoutTestPair'),
  /** Press Call once paired. */
  call: flag('ScoutTestCall'),
};

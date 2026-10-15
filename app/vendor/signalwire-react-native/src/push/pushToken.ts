import { Platform } from 'react-native';

import { watchFcmToken } from './fcmToken';
import { watchVoipToken } from './voipToken';

/** Which push service a token came from, as the server needs to know. */
export type PushPlatform = 'ios' | 'android';

export interface PushRegistration {
  platform: PushPlatform;
  token: string;
}

/**
 * Watches whichever push token this platform issues.
 *
 * The two services are not interchangeable — PushKit on iOS, FCM on Android,
 * with different token formats and different senders — but an app registering
 * a device wants one call, not a platform branch it has to remember to write.
 * Getting that branch wrong is quiet: the device registers under the wrong
 * platform, the server picks the wrong sender, and the push simply never
 * arrives.
 */
export function watchPushToken(onToken: (registration: PushRegistration) => void): () => void {
  if (Platform.OS === 'android') {
    return watchFcmToken((token) => onToken({ platform: 'android', token }));
  }
  return watchVoipToken((token) => onToken({ platform: 'ios', token }));
}

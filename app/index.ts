// Polyfills first: they must run before anything loads @signalwire/js.
import '@signalwire/react-native/polyfills';

import { getCallKit } from '@signalwire/react-native/callkit';
import { registerRootComponent } from 'expo';

import App from './src/App';
import { setUpPairActions, showWhileOpen } from './src/push';
import { watchCancels, watchRings } from './src/ring';

// CallKit before React mounts. Voice only: no video button, and calls start on the
// receiver or a connected Bluetooth headset or car, not the loudspeaker.
getCallKit()
  .setup({ appName: 'Scout', supportsVideo: false })
  .catch((e: unknown) => console.warn('CallKit setup failed:', e));

// Scout's calls (VoIP pushes, already ringing in CallKit) and pairing notifications, before
// React mounts: a push can launch the app, and the answer can come before the first screen.
watchRings();
watchCancels();
showWhileOpen();
setUpPairActions().catch((e: unknown) => console.warn('Notification actions not set up:', e));

registerRootComponent(App);

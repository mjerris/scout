// Polyfills first: they must run before anything loads @signalwire/js.
import '@signalwire/react-native/polyfills';

import { getCallKit } from '@signalwire/react-native/callkit';
import { registerRootComponent } from 'expo';

import App from './src/App';

// CallKit before React mounts. Voice only: no video button, and calls start on the
// receiver or a connected Bluetooth headset or car, not the loudspeaker.
getCallKit()
  .setup({ appName: 'Scout', supportsVideo: false })
  .catch((e: unknown) => console.warn('CallKit setup failed:', e));

registerRootComponent(App);

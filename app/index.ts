// Polyfills first: they must run before anything loads @signalwire/js.
import '@signalwire/react-native/polyfills';

import { registerRootComponent } from 'expo';

import App from './src/App';

registerRootComponent(App);

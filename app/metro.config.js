const { getDefaultConfig } = require('expo/metro-config');

// The vendored SignalWire packages are tarballs (vendor/), installed as ordinary copies in
// node_modules: Expo's defaults resolve them.
module.exports = getDefaultConfig(__dirname);

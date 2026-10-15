const path = require('path');
const { getDefaultConfig } = require('expo/metro-config');

const config = getDefaultConfig(__dirname);
// The vendored SignalWire packages are symlinked from node_modules; they resolve their
// own imports (react-native-callkeep, rxjs...) from this app's node_modules.
config.watchFolders = [path.resolve(__dirname, 'vendor')];
config.resolver.nodeModulesPaths = [path.resolve(__dirname, 'node_modules')];
module.exports = config;

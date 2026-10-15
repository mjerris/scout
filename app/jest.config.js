module.exports = {
  preset: 'jest-expo/ios',
  roots: ['<rootDir>/src'],
  setupFiles: ['<rootDir>/jest.setup.js'],
  // jest-expo's list, plus uuid (ESM only since v14), which @signalwire/js requires.
  transformIgnorePatterns: [
    '/node_modules/(?!(.pnpm|react-native|@react-native|@react-native-community|expo|@expo|uuid))',
  ],
};

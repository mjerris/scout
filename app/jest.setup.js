/* global jest */
// Native modules the app touches, for unit tests (jest-expo mocks Expo's own).
jest.mock('react-native-safe-area-context', () => require('react-native-safe-area-context/jest/mock').default);

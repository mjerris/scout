/** Thrown when an optional peer dependency is absent or not natively linked. */
export class MissingPeerDependencyError extends Error {
  constructor(
    public readonly packageName: string,
    public readonly feature: string
  ) {
    super(
      `${feature} requires "${packageName}", which is not available. ` +
        `Run \`npm install ${packageName}\` and rebuild the native app ` +
        `(\`npx expo prebuild --clean\` or a fresh pod install / gradle build). ` +
        `A package that is installed but not linked produces this same error.`
    );
    this.name = 'MissingPeerDependencyError';
  }
}

/** Thrown when a required side-effect polyfill import did not run. */
export class PolyfillNotInstalledError extends Error {
  constructor(public readonly importPath: string) {
    super(
      `Missing required polyfills. Add \`import '${importPath}';\` as the very ` +
        `first line of your app entry file, before any import of "@signalwire/js" ` +
        `or "@signalwire/react-native".`
    );
    this.name = 'PolyfillNotInstalledError';
  }
}

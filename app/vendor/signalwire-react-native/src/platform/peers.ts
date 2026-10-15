import { MissingPeerDependencyError } from '../errors';

/**
 * Verifies an optional peer module is present and natively linked.
 *
 * Metro resolves `require` statically, so a `try/catch` around a missing module
 * still fails at bundle time. Subpath entry points therefore import their peers
 * statically — Metro only resolves them if the app imports that subpath. This
 * guard catches the more common real-world failure: the package is installed
 * but the native module is not linked, so the JS binding is `undefined`.
 *
 * @param mod - The imported module namespace or default export.
 * @param packageName - npm name, used in the error message.
 * @param feature - Human-readable feature name, used in the error message.
 * @param requiredMember - A member that only exists when the native side is linked.
 */
export function assertPeerModule<T extends object>(
  mod: T | null | undefined,
  packageName: string,
  feature: string,
  requiredMember: string
): T {
  if (mod === null || mod === undefined || !(requiredMember in mod)) {
    throw new MissingPeerDependencyError(packageName, feature);
  }
  return mod;
}

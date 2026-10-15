/**
 * ES2025 iterator helpers, for runtimes that lack them.
 *
 * `@signalwire/js` calls them on `Map` iterators, e.g. in `DirectoryManager`:
 *
 * ```js
 * Array.from(this._addressesInstances.values().map((address) => address.id));
 * this._addressesInstances.values().find((addr) => addr.name === name)?.id;
 * ```
 *
 * V8 has shipped `Iterator.prototype.map` and friends since Chrome 122, so the
 * browser SDK is right to use them. **Hermes has not**, so those calls throw
 * `values().map is not a function` — which surfaces as a crash the moment
 * anything touches the directory.
 *
 * This is the fifth thing that breaks the SDK under React Native, and unlike
 * the other four it is not an import-time failure: it lies dormant until a
 * code path reaches one of these calls.
 *
 * Only missing methods are installed, so a Hermes release that ships the real
 * helpers takes precedence automatically.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

type AnyIterator = Iterator<unknown> & Record<string, unknown>;

/** The shared `%IteratorPrototype%`, reached through any built-in iterator. */
function iteratorPrototype(): Record<string, unknown> | null {
  const arrayIterator = [][Symbol.iterator]();
  const arrayIteratorPrototype = Object.getPrototypeOf(arrayIterator) as object | null;
  const shared = arrayIteratorPrototype
    ? (Object.getPrototypeOf(arrayIteratorPrototype) as object | null)
    : null;
  return shared as Record<string, unknown> | null;
}

function define(target: Record<string, unknown>, name: string, value: unknown): void {
  if (typeof target[name] === 'function') {
    return;
  }
  Object.defineProperty(target, name, {
    value,
    writable: true,
    configurable: true,
    enumerable: false
  });
}

/**
 * Installs the iterator helpers the SDK relies on. Safe to call repeatedly.
 *
 * Deliberately not the full ES2025 surface — these are the ones the SDK uses,
 * plus the close relatives a caller would reasonably reach for next.
 */
export function installIteratorHelpers(): void {
  const proto = iteratorPrototype();
  if (!proto) {
    return;
  }

  define(proto, 'map', function* (this: AnyIterator, fn: (value: any, index: number) => any) {
    let index = 0;
    for (const value of this as unknown as Iterable<unknown>) {
      yield fn(value, index++);
    }
  });

  define(proto, 'filter', function* (this: AnyIterator, fn: (value: any, index: number) => boolean) {
    let index = 0;
    for (const value of this as unknown as Iterable<unknown>) {
      if (fn(value, index++)) {
        yield value;
      }
    }
  });

  define(proto, 'flatMap', function* (this: AnyIterator, fn: (value: any, index: number) => any) {
    let index = 0;
    for (const value of this as unknown as Iterable<unknown>) {
      const mapped = fn(value, index++);
      if (mapped != null && typeof (mapped as any)[Symbol.iterator] === 'function') {
        yield* mapped as Iterable<unknown>;
      } else {
        yield mapped;
      }
    }
  });

  define(proto, 'take', function* (this: AnyIterator, limit: number) {
    if (limit <= 0) {
      return;
    }
    let taken = 0;
    for (const value of this as unknown as Iterable<unknown>) {
      yield value;
      if (++taken >= limit) {
        return;
      }
    }
  });

  define(proto, 'drop', function* (this: AnyIterator, count: number) {
    let dropped = 0;
    for (const value of this as unknown as Iterable<unknown>) {
      if (dropped++ < count) {
        continue;
      }
      yield value;
    }
  });

  define(proto, 'find', function (this: AnyIterator, fn: (value: any, index: number) => boolean) {
    let index = 0;
    for (const value of this as unknown as Iterable<unknown>) {
      if (fn(value, index++)) {
        return value;
      }
    }
    return undefined;
  });

  define(proto, 'some', function (this: AnyIterator, fn: (value: any, index: number) => boolean) {
    let index = 0;
    for (const value of this as unknown as Iterable<unknown>) {
      if (fn(value, index++)) {
        return true;
      }
    }
    return false;
  });

  define(proto, 'every', function (this: AnyIterator, fn: (value: any, index: number) => boolean) {
    let index = 0;
    for (const value of this as unknown as Iterable<unknown>) {
      if (!fn(value, index++)) {
        return false;
      }
    }
    return true;
  });

  define(proto, 'forEach', function (this: AnyIterator, fn: (value: any, index: number) => void) {
    let index = 0;
    for (const value of this as unknown as Iterable<unknown>) {
      fn(value, index++);
    }
  });

  define(
    proto,
    'reduce',
    function (this: AnyIterator, fn: (acc: any, value: any, index: number) => any, ...rest: any[]) {
      let index = 0;
      let acc: unknown;
      let seeded = rest.length > 0;
      if (seeded) {
        acc = rest[0];
      }
      for (const value of this as unknown as Iterable<unknown>) {
        if (!seeded) {
          acc = value;
          seeded = true;
          index++;
          continue;
        }
        acc = fn(acc, value, index++);
      }
      if (!seeded) {
        throw new TypeError('Reduce of empty iterator with no initial value');
      }
      return acc;
    }
  );

  define(proto, 'toArray', function (this: AnyIterator) {
    return Array.from(this as unknown as Iterable<unknown>);
  });
}

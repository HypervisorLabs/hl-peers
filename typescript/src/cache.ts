/**
 * The conditional-request cache of CLIENT_SPEC.md §4.5: small, in-memory, LRU,
 * per client instance, **never global and never on disk**.
 *
 * It exists to make polling free — a `304` costs no quota unit — not to serve
 * stale bytes, so `Cache-Control: max-age` is not honoured as a hard TTL and
 * the client always revalidates.
 */

export interface CacheEntry {
  readonly etag: string;
  readonly body: unknown;
  readonly storedAt: number;
  /** The `API-Version` of the stored response, for a `304` that omits it. */
  readonly apiVersion?: string | null;
}

/** The cache contract, so a caller can supply their own. */
export interface EtagCache {
  get(key: string): CacheEntry | undefined;
  set(key: string, entry: CacheEntry): void;
  delete(key: string): void;
  clear(): void;
  readonly size: number;
}

/** A least-recently-used {@link EtagCache}. Default capacity 64 entries. */
export class LruEtagCache implements EtagCache {
  private readonly entries = new Map<string, CacheEntry>();
  readonly capacity: number;

  constructor(capacity = 64) {
    this.capacity = Math.max(1, capacity);
  }

  get(key: string): CacheEntry | undefined {
    const hit = this.entries.get(key);
    if (hit === undefined) return undefined;
    // refresh recency
    this.entries.delete(key);
    this.entries.set(key, hit);
    return hit;
  }

  set(key: string, entry: CacheEntry): void {
    if (this.entries.has(key)) this.entries.delete(key);
    this.entries.set(key, entry);
    while (this.entries.size > this.capacity) {
      const oldest = this.entries.keys().next();
      if (oldest.done === true) break;
      this.entries.delete(oldest.value);
    }
  }

  delete(key: string): void {
    this.entries.delete(key);
  }

  clear(): void {
    this.entries.clear();
  }

  get size(): number {
    return this.entries.size;
  }
}

/**
 * §4.5's key. The tier is part of it because a key changes the page ceiling,
 * so an anonymous body and a keyed body are not interchangeable.
 */
export function cacheKey(
  method: string,
  path: string,
  canonicalQuery: string,
  apiKeyPresent: boolean,
): string {
  return `${method}\0${path}\0${canonicalQuery}\0${apiKeyPresent ? 'k' : 'a'}`;
}

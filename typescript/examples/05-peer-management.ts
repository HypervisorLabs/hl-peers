/**
 * Peer management: the Peer API as one input to your own peer tooling.
 *
 * Five small building blocks, each usable on its own:
 *
 * 1. `refreshCandidates`: pull a fresh, measured-healthy candidate list.
 * 2. `diversify`: spread that list across networks (ASN) and countries.
 * 3. `checkRoots`: look up the peers already in your file, one by one.
 * 4. `rotateOut`: swap one degraded root for the next candidate, keeping a
 *    floor of known-good peers and a backup of the previous file.
 * 5. `nextPollDelayMs`: when to ask again, aligned to the data's own clock,
 *    with backoff on a rate limit or an error.
 *
 * 🔴 Nothing here restarts, signals or otherwise disturbs a node. These
 * functions decide and write a file; when that file takes effect is the
 * operator's decision, at the operator's moment.
 */
import {
  NotFoundError,
  RateLimitedError,
  peerQuery,
  type Peer,
  type PeerApiClient,
  type PeerListResult,
  type PeerQuery,
} from '../src/index.ts';
import { load, save } from '../src/gossip-fs.ts';

/** The defaults a scheduled refresh starts from: close to tip, seen recently. */
export function defaultCandidateQuery(): PeerQuery {
  return peerQuery().maxTier(2).maxBlocksBehind(5).seenWithin('1h').sort('random').limit(50);
}

export interface CandidateOptions {
  /** Drop peers more than this many blocks behind (and peers with no reading). */
  maxBlocksBehind?: number;
  /** IPs you never want back, e.g. ones you rotated out recently. */
  exclude?: readonly string[];
}

/**
 * Pattern (a): one page of candidates, re-checked locally.
 *
 * The server already filters; the local re-check is belt and braces for a
 * hand-built query, and it keeps "unknown" out: a peer with no height reading
 * or no recent observation is not a candidate.
 */
export async function refreshCandidates(
  client: PeerApiClient,
  query: PeerQuery = defaultCandidateQuery(),
  opts: CandidateOptions = {},
): Promise<Peer[]> {
  const res = await client.listPeers(query);
  if ('notModified' in res) return []; // only when you pass ifNoneMatch yourself
  // 🔴 available:false means "no graph right now", not "no peers": keep your
  // current file untouched rather than acting on an empty answer.
  if (!res.envelope.available) throw new Error('peer graph unavailable; keep the current list');
  const maxBehind = opts.maxBlocksBehind ?? 5;
  const exclude = new Set(opts.exclude ?? []);
  return res.peers.filter(
    (p) =>
      !exclude.has(p.id) &&
      p.live !== false &&
      p.blocks_behind !== null &&
      p.blocks_behind <= maxBehind,
  );
}

/**
 * Pattern (c): at most `perAsn` peers per network and `perCountry` per
 * country, keeping the input order (which is random if you sorted randomly).
 * A peer with no published ASN or country is its own bucket, never merged.
 */
export function diversify(
  peers: readonly Peer[],
  opts: { perAsn?: number; perCountry?: number; max?: number } = {},
): Peer[] {
  const perAsn = opts.perAsn ?? 1;
  const perCountry = opts.perCountry ?? 2;
  const max = opts.max ?? 8;
  const byAsn = new Map<string, number>();
  const byCc = new Map<string, number>();
  const out: Peer[] = [];
  for (const p of peers) {
    if (out.length >= max) break;
    const asn = p.asn ?? `unknown:${p.id}`;
    const cc = p.cc ?? `unknown:${p.id}`;
    if ((byAsn.get(asn) ?? 0) >= perAsn || (byCc.get(cc) ?? 0) >= perCountry) continue;
    byAsn.set(asn, (byAsn.get(asn) ?? 0) + 1);
    byCc.set(cc, (byCc.get(cc) ?? 0) + 1);
    out.push(p);
  }
  return out;
}

export type RootHealth = 'ok' | 'behind' | 'not_seen' | 'not_published';

/**
 * Pattern (b), the API half: how the map sees each root in your file. This is
 * the map's view from outside; pair it with what your own node reports about
 * its upstream before you act.
 */
export async function checkRoots(
  client: PeerApiClient,
  path: string,
  maxBlocksBehind = 5,
): Promise<Record<string, RootHealth>> {
  const out: Record<string, RootHealth> = {};
  for (const ip of load(path).rootIps()) {
    try {
      const res = await client.getPeer(ip);
      if ('notModified' in res) continue;
      const p = res.peer;
      if (p.live === false || p.blocks_behind === null) out[ip] = 'not_seen';
      else out[ip] = p.blocks_behind > maxBlocksBehind ? 'behind' : 'ok';
    } catch (err) {
      // 404: a well-formed IP the map has not published. Not proof it is
      // down, so treat it as "no information", not as "bad".
      if (err instanceof NotFoundError) out[ip] = 'not_published';
      else throw err;
    }
  }
  return out;
}

export interface RotateResult {
  removed: string;
  added: string | null;
  roots: string[];
  backupPath?: string;
  written: boolean;
}

/**
 * Patterns (b) and (e): take ONE degraded root out, put the next unused
 * candidate in, refuse to drop below `floor` roots, and keep a timestamped
 * backup of the previous file for rollback.
 */
export function rotateOut(
  path: string,
  degraded: string,
  candidates: readonly string[],
  opts: { floor?: number; dryRun?: boolean } = {},
): RotateResult {
  const floor = opts.floor ?? 4;
  const config = load(path);
  if (!config.hasRootIp(degraded)) {
    return { removed: degraded, added: null, roots: config.rootIps(), written: false };
  }
  const next = candidates.find((ip) => ip !== degraded && !config.hasRootIp(ip)) ?? null;
  config.removeRootIp(degraded);
  if (next !== null) config.addRootIp(next);
  if (config.rootIps().length < floor) {
    throw new Error(
      `refusing to rotate: ${config.rootIps().length} roots would remain, floor is ${floor}`,
    );
  }
  const saved = save(config, path, { backup: true, dryRun: opts.dryRun === true });
  return {
    removed: degraded,
    added: next,
    roots: config.rootIps(),
    written: saved.written,
    ...(saved.backupPath !== undefined ? { backupPath: saved.backupPath } : {}),
  };
}

/**
 * Pattern (d): when to poll next, in milliseconds.
 *
 * - After a page: just after `next_sweep_at`, plus jitter, never sooner than
 *   a minute.
 * - After a 429: until `resetAt` (the client computes it from the headers).
 * - After any other error: exponential backoff, capped at 15 minutes.
 */
export function nextPollDelayMs(
  outcome: { page: PeerListResult } | { error: unknown; attempt: number },
  now: number = Date.now(),
  random: () => number = Math.random,
): number {
  const jitter = Math.floor(random() * 15_000);
  if ('page' in outcome) {
    const next = Date.parse(outcome.page.envelope.next_sweep_at ?? '');
    const wait = Number.isNaN(next) ? 5 * 60_000 : next - now;
    return Math.max(60_000, wait) + jitter;
  }
  if (outcome.error instanceof RateLimitedError && outcome.error.resetAt !== null) {
    return Math.max(1_000, outcome.error.resetAt.getTime() - now) + jitter;
  }
  return Math.min(15 * 60_000, 30_000 * 2 ** Math.max(0, outcome.attempt)) + jitter;
}

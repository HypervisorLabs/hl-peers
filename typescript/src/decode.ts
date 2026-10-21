/** Body and header decoding (CLIENT_SPEC.md §3). */
import { DecodeError } from './errors.ts';
import type {
  Chain,
  CityCluster,
  Connectable,
  Envelope,
  Facet,
  NetworkMapEdge,
  NetworkMapEnvelope,
  NetworkMapTier,
  Peer,
  PeerAuction,
  PeerMempool,
  Problem,
  RateLimit,
} from './types.ts';

/** A minimal, runtime-agnostic view of response headers. */
export interface HeaderReader {
  get(name: string): string | null;
}

function intOrNull(raw: string | null): number | null {
  if (raw === null) return null;
  const trimmed = raw.trim();
  if (!/^-?\d+$/.test(trimmed)) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

/**
 * Resolve `Retry-After` to a delay in seconds (CLIENT_SPEC §5.2):
 *
 * 1. an **all-digits** value is a delay in seconds;
 * 2. otherwise parse it as an **IMF-fixdate** and convert with the client's
 *    **injectable clock**: `max(0, date - now)`, so tests never depend on wall
 *    time;
 * 3. a date in the past yields `0` — retry immediately, not "absent";
 * 4. a value that parses as neither is **absent** (`null`): fall back to
 *    backoff. Never guess, and never fail the request over a bad header.
 */
export function parseRetryAfter(raw: string | null, now: number): number | null {
  if (raw === null) return null;
  const trimmed = raw.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed);
  const date = Date.parse(trimmed);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.round((date - now) / 1000));
}

/**
 * The `RateLimit-*` block, parsed from **every** response — 200, 304 and every
 * error alike. A missing or non-integer header is `null`, never 0.
 */
export function parseRateLimit(headers: HeaderReader, now: number): RateLimit {
  return {
    limit: intOrNull(headers.get('RateLimit-Limit')),
    remaining: intOrNull(headers.get('RateLimit-Remaining')),
    // Seconds from now, not an epoch.
    reset: intOrNull(headers.get('RateLimit-Reset')),
    policy: headers.get('RateLimit-Policy'),
    retryAfter: parseRetryAfter(headers.get('Retry-After'), now),
    retryAfterRaw: headers.get('Retry-After'),
  };
}

export function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

export function str(v: unknown): string | null {
  return typeof v === 'string' ? v : null;
}

export function num(v: unknown): number | null {
  return typeof v === 'number' ? v : null;
}

export function bool(v: unknown): boolean | null {
  return typeof v === 'boolean' ? v : null;
}

/** The `API-Version` header: informational, never validated (CLIENT_SPEC §1). */
export function parseApiVersion(headers: HeaderReader): string | null {
  return headers.get('API-Version');
}

/**
 * The wire keys each decoder consumes. `test/fields.test.ts` asserts these
 * equal the field sets generated from the spec (`spec/peer-api-v1.fields.json`),
 * so a field the spec adds or drops fails the build rather than drifting.
 */
export const ENVELOPE_FIELDS = Object.freeze([
  'version',
  'available',
  'stale',
  'chain',
  'generated_at',
  'next_sweep_at',
  'attribution',
] as const);

export const PEER_FIELDS = Object.freeze([
  'id',
  'tier',
  'state',
  'role',
  'cc',
  'cc_confidence',
  'cc_source',
  'region',
  'asn',
  'org',
  'city',
  'operator',
  'name',
  'in_deg',
  'out_deg',
  'feeders',
  'latest_height',
  'blocks_behind',
  'live',
  'connectable',
  'labels',
  'last_seen',
  'uptime',
  'version',
  'mempool',
  'auction',
] as const);

export const PEER_MEMPOOL_FIELDS = Object.freeze([
  'serves',
  'last_checked',
  'verdict',
  'first_frame_ms',
  'source',
] as const);

export const PEER_AUCTION_FIELDS = Object.freeze([
  'status',
  'slot',
  'since',
  'last_bid_at',
] as const);

export const NETWORK_MAP_EDGE_FIELDS = Object.freeze(['from', 'to', 'span'] as const);

export const NETWORK_MAP_CITY_FIELDS = Object.freeze([
  'id',
  'name',
  'cc',
  'lat',
  'lon',
  'peers',
  'tiers',
] as const);

/** Decode the standing envelope fields. */
export function decodeEnvelope(body: Record<string, unknown>): Envelope {
  return {
    version: str(body['version']) ?? 'v1',
    available: bool(body['available']) ?? true,
    stale: bool(body['stale']) ?? false,
    chain: str(body['chain']) as Chain | null,
    generated_at: str(body['generated_at']),
    next_sweep_at: str(body['next_sweep_at']),
    attribution: str(body['attribution']),
  };
}

/** The `/network-map/*` envelope: no `chain`, no `next_sweep_at`. */
export function decodeNetworkMapEnvelope(body: Record<string, unknown>): NetworkMapEnvelope {
  return {
    version: str(body['version']) ?? 'v1',
    available: bool(body['available']) ?? true,
    stale: bool(body['stale']) ?? false,
    generated_at: str(body['generated_at']),
  };
}

const PEER_KEYS = new Set<string>(PEER_FIELDS);

/**
 * Decode a peer's `mempool` object. A missing or malformed value is the
 * all-`null` object (unknown), never an error. 🔴 `serves: null` stays `null`
 * — unknown is not "does not serve". Unknown inner keys are ignored.
 */
export function decodeMempool(raw: unknown): PeerMempool {
  const o = isObject(raw) ? raw : {};
  return {
    serves: bool(o['serves']),
    last_checked: str(o['last_checked']),
    verdict: str(o['verdict']),
    first_frame_ms: num(o['first_frame_ms']),
    source: str(o['source']),
  };
}

/** Decode a peer's `auction` object; missing ⇒ the all-`null` object. */
export function decodeAuction(raw: unknown): PeerAuction {
  const o = isObject(raw) ? raw : {};
  return {
    status: str(o['status']),
    slot: num(o['slot']),
    since: str(o['since']),
    last_bid_at: str(o['last_bid_at']),
  };
}

/**
 * Decode one peer. Every unrecognised key lands in {@link Peer.extra} rather
 * than being dropped, so a field the server adds later survives.
 *
 * 🔴 `tier: null` is preserved as `null`: this node was not placed, which
 * is NOT tier 0. `connectable` stays a three-valued **string**.
 */
export function decodePeer(raw: unknown): Peer {
  if (!isObject(raw)) {
    throw new DecodeError('expected a peer object', JSON.stringify(raw).slice(0, 512));
  }
  const id = str(raw['id']);
  if (id === null) {
    throw new DecodeError('peer is missing its `id`', JSON.stringify(raw).slice(0, 512));
  }
  const extra: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(raw)) {
    if (!PEER_KEYS.has(k)) extra[k] = v;
  }
  const labels = Array.isArray(raw['labels'])
    ? (raw['labels'] as unknown[]).filter((l): l is string => typeof l === 'string')
    : [];
  const feeders = Array.isArray(raw['feeders'])
    ? (raw['feeders'] as unknown[]).filter((f): f is string => typeof f === 'string')
    : null;
  return {
    id,
    tier: num(raw['tier']),
    state: str(raw['state']),
    role: str(raw['role']),
    cc: str(raw['cc']),
    cc_confidence: str(raw['cc_confidence']),
    cc_source: str(raw['cc_source']),
    region: str(raw['region']),
    asn: str(raw['asn']),
    org: str(raw['org']),
    city: str(raw['city']),
    operator: str(raw['operator']),
    name: str(raw['name']),
    in_deg: num(raw['in_deg']),
    out_deg: num(raw['out_deg']),
    feeders,
    latest_height: num(raw['latest_height']),
    blocks_behind: num(raw['blocks_behind']),
    live: bool(raw['live']),
    connectable: str(raw['connectable']) as Connectable | null,
    labels,
    last_seen: str(raw['last_seen']),
    uptime: num(raw['uptime']),
    version: str(raw['version']),
    mempool: decodeMempool(raw['mempool']),
    auction: decodeAuction(raw['auction']),
    extra,
  };
}

/** Decode the `/network-map/*` tier histogram. */
export function decodeTiers(raw: unknown): NetworkMapTier[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((t) => {
    if (!isObject(t)) return [];
    const tier = num(t['tier']);
    const count = num(t['count']);
    return tier === null || count === null ? [] : [{ tier, count }];
  });
}

/** Decode `/network-map/graph` edges. An edge without both ends is a decode error. */
export function decodeEdges(raw: unknown): NetworkMapEdge[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((e) => {
    const from = isObject(e) ? str(e['from']) : null;
    const to = isObject(e) ? str(e['to']) : null;
    if (!isObject(e) || from === null || to === null) {
      throw new DecodeError('network-map edge is missing `from`/`to`', JSON.stringify(e).slice(0, 512));
    }
    return { from, to, span: num(e['span']) };
  });
}

/**
 * Decode `/network-map/*` `cities`. 🔴 An absent (non-array) value is `null`,
 * kept distinct from `[]`. A cluster missing a required member is a decode error.
 */
export function decodeCities(raw: unknown): CityCluster[] | null {
  if (!Array.isArray(raw)) return null;
  return raw.map((c) => {
    const id = isObject(c) ? str(c['id']) : null;
    const name = isObject(c) ? str(c['name']) : null;
    const cc = isObject(c) ? str(c['cc']) : null;
    const lat = isObject(c) ? num(c['lat']) : null;
    const lon = isObject(c) ? num(c['lon']) : null;
    const peers = isObject(c) ? num(c['peers']) : null;
    if (!isObject(c) || id === null || name === null || cc === null || lat === null ||
        lon === null || peers === null) {
      throw new DecodeError('network-map city cluster is missing a required member',
        JSON.stringify(c).slice(0, 512));
    }
    return { id, name, cc, lat, lon, peers, tiers: decodeCounts(c['tiers']) };
  });
}

/** Decode a `{name: integer}` count map, dropping non-integer members. */
export function decodeCounts(raw: unknown): Record<string, number> {
  if (!isObject(raw)) return {};
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(raw)) {
    if (typeof v === 'number') out[k] = v;
  }
  return out;
}

/** Decode the `facets` map, keeping `available` and `values` distinguishable. */
export function decodeFacets(raw: unknown): Record<string, Facet> {
  if (!isObject(raw)) return {};
  const out: Record<string, Facet> = {};
  for (const [name, value] of Object.entries(raw)) {
    if (!isObject(value)) continue;
    const values = Array.isArray(value['values'])
      ? (value['values'] as unknown[]).flatMap((v) => {
          if (!isObject(v)) return [];
          const label = str(v['value']);
          if (label === null) return [];
          return [{ value: label, count: num(v['count']) ?? 0 }];
        })
      : [];
    out[name] = { available: bool(value['available']) ?? false, values };
  }
  return out;
}

/** Parse a problem+json body; anything else answers `null` (D-K). */
export function decodeProblem(body: unknown): Problem | null {
  if (!isObject(body)) return null;
  if (typeof body['type'] !== 'string' && typeof body['title'] !== 'string') return null;
  return body;
}

/** Truncate to at most `maxBytes` UTF-8 bytes, on a code-point boundary. */
export function truncateUtf8(text: string, maxBytes = 512): string {
  let bytes = 0;
  let out = '';
  for (const ch of text) {
    const size = utf8Length(ch);
    if (bytes + size > maxBytes) break;
    bytes += size;
    out += ch;
  }
  return out;
}

function utf8Length(ch: string): number {
  const cp = ch.codePointAt(0) ?? 0;
  // (a lone surrogate reports its own code unit, which is 3 bytes in UTF-8)
  if (cp < 0x80) return 1;
  if (cp < 0x800) return 2;
  if (cp < 0x10000) return 3;
  return 4;
}

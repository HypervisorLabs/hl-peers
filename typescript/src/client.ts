/**
 * `PeerApiClient` — the HTTP query surface of CLIENT_SPEC.md §1, §4 and §5.
 *
 * Runtime-agnostic: it uses global `fetch` only, takes an injectable `fetch`,
 * clock and PRNG, and touches no filesystem. The file-writing helpers live in
 * the separate `hl-peers/gossip-fs` entry point.
 */
import { cacheKey, LruEtagCache, type EtagCache } from './cache.ts';
import {
  decodeCities,
  decodeCounts,
  decodeEdges,
  decodeEnvelope,
  decodeFacets,
  decodeNetworkMapEnvelope,
  decodePeer,
  decodeProblem,
  decodeTiers,
  isObject,
  num,
  bool,
  parseApiVersion,
  parseRateLimit,
  truncateUtf8,
  type HeaderReader,
} from './decode.ts';
import {
  BadRequestError,
  ChainNotServedError,
  CursorExpiredError,
  DecodeError,
  FilterUnavailableError,
  InvalidError,
  NotFoundError,
  RateLimitedError,
  ServerError,
  TransportError,
  TruncatedError,
  UnauthorizedError,
  UnavailableError,
  NO_RATE_LIMIT,
} from './errors.ts';
import { GossipConfig } from './gossip.ts';
import { isIpv4, looksLikeIpv6 } from './ipv4.ts';
import {
  PeerQuery,
  checkNetwork,
  closedNullableCsv,
  encodeRfc3986,
  type AuctionMember,
  type ServesMempoolMember,
} from './query.ts';
import type {
  Connectable,
  Envelope,
  Problem,
  FacetsResult,
  GossipConfigResult,
  Network,
  NetworkMapGraphResult,
  NetworkMapSummaryBase,
  NetworkMapSummaryResult,
  NotModifiedResult,
  Peer,
  PeerListResult,
  PeerResult,
  RateLimit,
  ResultMeta,
} from './types.ts';

/** The public API host. Override with `baseUrl` or `HL_PEER_API_BASE_URL`. */
export const DEFAULT_BASE_URL = 'https://api.hypervisor.gg';

/**
 * The default route prefix — the ONE place it is spelled. Every route is
 * `basePath + '/peers…'`. The public API serves **plain paths at the host
 * root** (CLIENT_SPEC §10 D-N), so the
 * default is empty; the option remains for a proxy that mounts the API under a
 * prefix.
 */
export const DEFAULT_BASE_PATH = '';

/**
 * CLIENT_SPEC §1 normalization: strip every leading/trailing `/`; empty means
 * no prefix, anything else is `/` + the trimmed value.
 */
export function normalizeBasePath(path: string): string {
  const trimmed = path.replace(/^\/+|\/+$/g, '');
  return trimmed === '' ? '' : '/' + trimmed;
}

/** The library version, as sent in `User-Agent`. */
export const VERSION = '1.1.0';

/**
 * The parameters `facets()` refuses (CLIENT_SPEC §4.4): the endpoint does not
 * take them, and a caller would believe they applied.
 */
const FACETS_REFUSED = new Set([
  'sort',
  'limit',
  'cursor',
  'fields',
  'format',
  'try_new_peers',
  'split_client_blocks',
]);

/** Exactly the shape of the global `fetch`, so any runtime's can be injected. */
export type FetchLike = typeof globalThis.fetch;

/** A warning sink. The client currently emits no warnings. */
export interface Logger {
  warn(message: string): void;
}

export interface PeerApiClientOptions {
  /**
   * Resolution: this argument → `HL_PEER_API_BASE_URL` →
   * {@link DEFAULT_BASE_URL} (`https://api.hypervisor.gg`).
   */
  baseUrl?: string;
  /**
   * Route prefix for every call. Default {@link DEFAULT_BASE_PATH} (`''`:
   * plain paths at the host root); normalized per CLIENT_SPEC §1. For a proxy
   * that mounts the API under a prefix.
   */
  basePath?: string;
  /**
   * Resolution: this argument → `HL_PEER_API_KEY` → `null`. Absent means
   * **anonymous, a supported tier, not an error**. The key is sent as
   * `Authorization: Bearer …` and is never logged, never put in a query string
   * and never included in an error message.
   */
  apiKey?: string | null;
  /** Total per attempt, connect + read. Default 10 000 ms. */
  timeoutMs?: number;
  userAgent?: string;
  /** Appended after a space, for downstream apps. */
  userAgentSuffix?: string | null;
  /**
   * Attempts **after** the first. 🔴 Default **0**: auto-retry is OFF, so a
   * library never sleeps behind your back. Set e.g. `3` to opt in to retrying
   * `429` (honouring `Retry-After` up to `maxRetryAfterMs`), `5xx` and
   * transport failures with jittered exponential backoff.
   */
  maxRetries?: number;
  /**
   * 🔴 A `Retry-After` above this cap raises {@link RateLimitedError}
   * immediately rather than sleeping (CLIENT_SPEC §10, D-I): a library should
   * not silently block a caller for minutes. Default 60 000 ms.
   */
  maxRetryAfterMs?: number;
  /** Guard on {@link PeerApiClient.pollPeers} only. Default 60 000 ms. */
  minPollIntervalMs?: number;
  /** `null` disables conditional caching. Default: 64-entry in-memory LRU. */
  cache?: EtagCache | null;
  /** Injected for tests and non-standard runtimes. */
  fetch?: FetchLike;
  /** Injected clock, in epoch milliseconds. */
  now?: () => number;
  /** Injected PRNG in [0, 1). Retry jitter only; never asserted on. */
  random?: () => number;
  /** Injected sleep, so retry tests never touch the wall clock. */
  sleep?: (ms: number) => Promise<void>;
  /** @deprecated Accepted and ignored: the client emits no warnings. */
  logger?: Logger;
  /**
   * @deprecated Accepted and has no effect.
   */
  suppressPlaceholderWarning?: boolean;
  /** Environment source; defaults to `process.env` where one exists. */
  env?: Record<string, string | undefined>;
  /**
   * The default `network` for every call that does not set its own
   * (CLIENT_SPEC §1, §10 D-U); a per-call value wins. Default `undefined`:
   * nothing is sent, so the server's default (mainnet) governs and a mainnet
   * request is byte-identical to one from a client without this option.
   * Validated at construction: anything but `mainnet` | `testnet` is an
   * {@link InvalidError} with `parameter === "network"`.
   *
   * 🪤 `testnet` is **beta** (see {@link Network}).
   */
  network?: Network | null;
}

/** Per-call options. */
export interface RequestOptions {
  signal?: AbortSignal;
  /**
   * A caller-supplied conditional question. It overrides the cache, and a
   * matching `304` is returned as {@link NotModifiedResult} rather than a body.
   */
  ifNoneMatch?: string;
  /** An overall wall-clock deadline across retries, in epoch milliseconds. */
  deadlineMs?: number;
}

export interface IterOptions extends RequestOptions {
  /** Hard guard. Default **10 000**; never unbounded. */
  maxItems?: number;
  /** `stop` ends the stream and sets `truncated`; `raise` throws. */
  onTruncate?: 'stop' | 'raise';
}

/** The handle `iterPeers` returns: an async iterable that reports truncation. */
export interface PeerIterable extends AsyncIterable<Peer> {
  /** True once the `maxItems` guard stopped the walk. */
  readonly truncated: boolean;
  /** Peers yielded so far. */
  readonly seen: number;
  /** The envelope of the most recent page, once one has arrived. */
  readonly envelope: Envelope | null;
  /** The `API-Version` header of the most recent page, once one has arrived. */
  readonly apiVersion: string | null;
}

interface RawResponse {
  status: number;
  headers: HeaderReader;
  text: string;
  rateLimit: RateLimit;
  apiVersion: string | null;
  /** The injected clock at response time. */
  at: number;
}

/** Options of `networkMapGraph` / `networkMapSummary` (CLIENT_SPEC §4.8). */
export interface NetworkMapOptions {
  /** Tier ceiling, integer ≥ 0. */
  maxTier?: number;
  showUnreached?: boolean;
  hideUnreachable?: boolean;
  /** A closed enum, validated locally like the `/peers` filter. */
  connectable?: readonly Connectable[] | string;
  cc?: string;
  operator?: string;
  label?: string;
  /**
   * Serves the mempool: a closed tri-state, exactly as the `/peers`
   * builder's `servesMempool` — `null` = unknown, **not** `false`.
   */
  servesMempool?: readonly ServesMempoolMember[] | string;
  /**
   * Which network's graph to draw: `mainnet` | `testnet` (closed,
   * case-sensitive). Unset falls back to the client's `network` option, else
   * nothing is sent (mainnet). 🪤 `testnet` is **beta**.
   */
  network?: Network;
  /** Gossip read-priority auction status, as the builder's `auction`. */
  auction?: readonly AuctionMember[] | string;
}

const NETWORK_MAP_CONNECTABLE = ['yes', 'no', 'unknown'];

/**
 * The sorted, percent-encoded query string of a network-map call, after the
 * §2.2-style validation (`max_tier` ≥ 0, `connectable` closed). Unset values
 * are omitted.
 */
export function networkMapQueryString(opts: NetworkMapOptions = {}): string {
  if (opts.maxTier !== undefined && (!Number.isInteger(opts.maxTier) || opts.maxTier < 0)) {
    throw new InvalidError('max_tier', `expected a non-negative integer, got ${opts.maxTier}`);
  }
  let connectable: string | undefined;
  if (opts.connectable !== undefined) {
    const members =
      typeof opts.connectable === 'string'
        ? opts.connectable.split(',').map((m) => m.trim()).filter((m) => m !== '')
        : [...opts.connectable];
    for (const m of members) {
      if (!NETWORK_MAP_CONNECTABLE.includes(m)) {
        throw new InvalidError(
          'connectable',
          `${JSON.stringify(m)} is not a member of the closed enum [yes, no, unknown]`,
        );
      }
    }
    connectable = members.join(',');
  }
  const servesMempool =
    opts.servesMempool === undefined ? undefined : closedNullableCsv('serves_mempool', opts.servesMempool);
  const auction = opts.auction === undefined ? undefined : closedNullableCsv('auction', opts.auction);
  const network = checkNetwork(opts.network);
  const pairs: [string, string | undefined][] = [
    ['auction', auction],
    ['cc', opts.cc],
    ['connectable', connectable],
    ['hide_unreachable', opts.hideUnreachable === undefined ? undefined : String(opts.hideUnreachable)],
    ['label', opts.label],
    ['max_tier', opts.maxTier === undefined ? undefined : String(opts.maxTier)],
    ['network', network],
    ['operator', opts.operator],
    ['serves_mempool', servesMempool],
    ['show_unreached', opts.showUnreached === undefined ? undefined : String(opts.showUnreached)],
  ];
  return pairs
    .filter((p): p is [string, string] => p[1] !== undefined && p[1] !== '')
    .map(([k, v]) => `${encodeRfc3986(k)}=${encodeRfc3986(v)}`)
    .join('&');
}

const RETRYABLE_5XX = (status: number): boolean => status >= 500 && status <= 599;

function defaultEnv(): Record<string, string | undefined> {
  const proc = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  return proc?.env ?? {};
}

function problemSuffix(type: string | undefined): string | null {
  if (typeof type !== 'string' || type.length === 0) return null;
  const path = type.split('?')[0]!.split('#')[0]!;
  const last = path.split('/').pop();
  return last === undefined || last === '' ? null : last;
}

/**
 * The Peer API client.
 *
 * Every successful result carries the decoded body, the envelope,
 * {@link RateLimit}, the `etag`, `fromCache` and the HTTP `status`: nothing
 * about the response is hidden from the caller.
 */
export class PeerApiClient {
  readonly baseUrl: string;
  /** The normalized route prefix; `''` means routes at the host root. */
  readonly basePath: string;
  readonly timeoutMs: number;
  readonly maxRetries: number;
  readonly maxRetryAfterMs: number;
  readonly minPollIntervalMs: number;
  readonly cache: EtagCache | null;
  /** The client-level default `network`; `null` sends nothing (mainnet). */
  readonly network: Network | null;

  private readonly apiKey: string | null;
  private readonly userAgent: string;
  private readonly fetchImpl: FetchLike;
  private readonly now: () => number;
  private readonly random: () => number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly pollJitterMs: number;

  constructor(options: PeerApiClientOptions = {}) {
    const env = options.env ?? defaultEnv();
    // An empty HL_PEER_API_BASE_URL counts as unset.
    const fromEnv = env['HL_PEER_API_BASE_URL'];
    this.baseUrl = (
      options.baseUrl ?? (fromEnv === undefined || fromEnv === '' ? DEFAULT_BASE_URL : fromEnv)
    ).replace(/\/+$/, '');
    this.basePath = normalizeBasePath(options.basePath ?? DEFAULT_BASE_PATH);
    // An explicit `apiKey: null` means "call anonymously", and beats the
    // environment; only an absent option falls through to HL_PEER_API_KEY.
    this.apiKey =
      options.apiKey !== undefined
        ? options.apiKey
        : Object.prototype.hasOwnProperty.call(options, 'apiKey')
          ? null
          : env['HL_PEER_API_KEY'] === undefined || env['HL_PEER_API_KEY'] === ''
            ? null
            : env['HL_PEER_API_KEY'];
    this.timeoutMs = options.timeoutMs ?? 10_000;
    // 🔴 Off by default (CLIENT_SPEC §5.2, §10 D-Q).
    this.maxRetries = options.maxRetries ?? 0;
    this.maxRetryAfterMs = options.maxRetryAfterMs ?? 60_000;
    this.minPollIntervalMs = options.minPollIntervalMs ?? 60_000;
    this.cache = options.cache === undefined ? new LruEtagCache(64) : options.cache;
    this.network = checkNetwork(options.network ?? undefined) ?? null;
    const base = options.userAgent ?? `hl-peers-ts/${VERSION}`;
    this.userAgent = options.userAgentSuffix ? `${base} ${options.userAgentSuffix}` : base;
    const fetchImpl = options.fetch ?? globalThis.fetch;
    if (typeof fetchImpl !== 'function') {
      throw new InvalidError(
        'fetch',
        'no global fetch in this runtime; pass one via `new PeerApiClient({fetch})` ' +
          '(Node >= 18, Bun, Deno and modern browsers all have one)',
      );
    }
    this.fetchImpl = fetchImpl;
    this.now = options.now ?? (() => Date.now());
    this.random = options.random ?? Math.random;
    this.sleep =
      options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
    // 5–15 s, randomised once per client instance (§4.6).
    this.pollJitterMs = 5_000 + Math.floor(this.random() * 10_000);
  }

  /** Whether a key is being sent. The key itself is never exposed. */
  get hasApiKey(): boolean {
    return this.apiKey !== null;
  }

  // -- the endpoints ------------------------------------------------------

  /** One page of peers. `GET <basePath>/peers`. */
  async listPeers(
    query: PeerQuery = new PeerQuery(),
    opts: RequestOptions = {},
  ): Promise<PeerListResult | NotModifiedResult> {
    query.validate();
    // 🔴 §4.1: `format=gossip_config` has no `peers` array. Decoding it as a
    // page would hand the caller an empty peer list that is not a finding about
    // the network — the one mistake §3.1 exists to prevent — so it is refused
    // before the request goes out, not after.
    if (query.toObject()['format'] === 'gossip_config') {
      throw new InvalidError(
        'format',
        'format=gossip_config does not return a peer list; use ' +
          'listPeersAsGossipConfig() instead',
      );
    }
    const raw = await this.requestJson(
      this.basePath + '/peers',
      this.withDefaultNetwork(query).toQueryString(),
      opts,
    );
    if ('notModified' in raw) return raw;
    const body = raw.body;
    const envelope = decodeEnvelope(body);
    const peersRaw = body['peers'];
    if (!Array.isArray(peersRaw)) {
      throw new DecodeError('response has no `peers` array', truncateUtf8(raw.text));
    }
    const peers = peersRaw.map((p) => decodePeer(p));
    return {
      ...raw.meta,
      envelope,
      peers,
      count: typeof body['count'] === 'number' ? body['count'] : peers.length,
      total: typeof body['total'] === 'number' ? body['total'] : null,
      nextCursor: typeof body['next_cursor'] === 'string' ? body['next_cursor'] : null,
    };
  }

  /**
   * Walk cursor pagination to exhaustion, lazily.
   *
   * Stops when `next_cursor` is null, when the page is empty, when
   * `available: false` (🔴 without raising — the caller inspects the flag) or
   * when `maxItems` is reached. It never deduplicates across pages, never
   * reorders, and never restarts the walk on `cursor-expired`: that would mix
   * two graph generations, which is exactly what the server refused to do.
   *
   * 🪤 The whole directory is ~900 nodes — 18 anonymous pages at `limit=50`.
   */
  iterPeers(query: PeerQuery = new PeerQuery(), opts: IterOptions = {}): PeerIterable {
    const maxItems = opts.maxItems ?? 10_000;
    const onTruncate = opts.onTruncate ?? 'stop';
    // The handle is an object literal so `truncated`/`seen` stay readable after
    // the walk; the generator therefore needs the client by name.
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    const self = this;
    const handle = {
      truncated: false,
      seen: 0,
      envelope: null as Envelope | null,
      apiVersion: null as string | null,
      async *[Symbol.asyncIterator](): AsyncGenerator<Peer, void, undefined> {
        let current = query;
        for (;;) {
          const page = await self.listPeers(current, opts);
          if ('notModified' in page) return;
          handle.envelope = page.envelope;
          handle.apiVersion = page.apiVersion;
          if (!page.envelope.available) return;
          if (page.peers.length === 0) return;
          for (const peer of page.peers) {
            if (handle.seen >= maxItems) {
              handle.truncated = true;
              if (onTruncate === 'raise') throw new TruncatedError(handle.seen);
              return;
            }
            handle.seen += 1;
            yield peer;
          }
          if (handle.seen >= maxItems) {
            handle.truncated = true;
            if (onTruncate === 'raise') throw new TruncatedError(handle.seen);
            return;
          }
          if (page.nextCursor === null) return;
          current = current.cursor(page.nextCursor);
        }
      },
    };
    return handle;
  }

  /** Eager convenience over {@link PeerApiClient.iterPeers}, same guard. */
  async listAllPeers(query: PeerQuery = new PeerQuery(), opts: IterOptions = {}): Promise<Peer[]> {
    const out: Peer[] = [];
    for await (const peer of this.iterPeers(query, opts)) out.push(peer);
    return out;
  }

  /**
   * One peer's public dossier. `GET <basePath>/peers/{ip}`.
   *
   * 🔴 A malformed IPv4 is a `400` and a well-formed IPv4 in no source is a
   * `404`; the two never collapse. The shape is checked locally first so a typo
   * does not spend a quota unit.
   */
  async getPeer(
    ip: string,
    opts: RequestOptions & {
      chain?: string;
      fields?: string | readonly string[];
      /** Which network to look the peer up on; falls back to the client option. */
      network?: Network;
    } = {},
  ): Promise<PeerResult | NotModifiedResult> {
    if (!isIpv4(ip)) {
      const raw: string = ip;
      throw new InvalidError(
        'ip',
        looksLikeIpv6(raw)
          ? `${raw} looks like IPv6: the peer API is IPv4-only`
          : `${raw} is not an IPv4 address`,
      );
    }
    let query = new PeerQuery();
    if (opts.chain !== undefined) query = query.chain(opts.chain as 'Mainnet' | 'Testnet');
    if (opts.fields !== undefined) query = query.fields(opts.fields);
    const network = checkNetwork(opts.network) ?? this.network;
    if (network !== null) query = query.network(network);
    query.validate();
    const raw = await this.requestJson(
      `${this.basePath}/peers/${encodeRfc3986(ip)}`,
      query.toQueryString(),
      opts,
    );
    if ('notModified' in raw) return raw;
    const peer = raw.body['peer'];
    if (peer === undefined) {
      throw new DecodeError('response has no `peer`', truncateUtf8(raw.text));
    }
    return { ...raw.meta, envelope: decodeEnvelope(raw.body), peer: decodePeer(peer) };
  }

  /**
   * Facet discovery. `GET <basePath>/peers/facets`.
   *
   * Accepts every **filter** of the `/peers` query — the counts are over the
   * filtered set. `sort`, `limit`, `cursor`, `fields`, `format` and
   * `try_new_peers` are refused with {@link InvalidError}, because the
   * endpoint does not take them and you would believe they applied.
   */
  async facets(
    query: PeerQuery = new PeerQuery(),
    opts: RequestOptions = {},
  ): Promise<FacetsResult | NotModifiedResult> {
    query.validate();
    for (const name of query.parameterNames()) {
      if (FACETS_REFUSED.has(name)) {
        throw new InvalidError(
          name,
          `the facets endpoint does not take ${name}; it would ignore it and you would ` +
            'read the counts as if it applied',
        );
      }
    }
    const raw = await this.requestJson(
      this.basePath + '/peers/facets',
      this.withDefaultNetwork(query).toQueryString(),
      opts,
    );
    if ('notModified' in raw) return raw;
    const facets = decodeFacets(raw.body['facets']);
    const geo = raw.body['geo_coverage'];
    return {
      ...raw.meta,
      envelope: decodeEnvelope(raw.body),
      total: typeof raw.body['total'] === 'number' ? raw.body['total'] : null,
      facets,
      geoCoverage: isObject(geo) ? geo : null,
      isAvailable(name: string): boolean {
        return facets[name]?.available === true;
      },
    };
  }

  /**
   * The server's `format=gossip_config` rendering of a query, parsed into a
   * {@link GossipConfig} (CLIENT_SPEC §4.7).
   *
   * The caller's `format` is overridden; a caller-set `tryNewPeers` is sent.
   * Nothing is written: save the result with the `gossip-fs` `save()`, the one
   * safe write path (validate, atomic rename, `0600`, optional `.bak`). The
   * config re-renders in the canonical §6.3 key order, not the server's.
   *
   * 🔴 The node reads `override_gossip_config.json` when it
   * starts; writing it does not steer a running node.
   */
  async listPeersAsGossipConfig(
    query: PeerQuery = new PeerQuery(),
    opts: RequestOptions = {},
  ): Promise<GossipConfigResult | NotModifiedResult> {
    const q = this.withDefaultNetwork(query.format('gossip_config'));
    q.validate();
    const raw = await this.requestJson(this.basePath + '/peers', q.toQueryString(), opts);
    if ('notModified' in raw) return raw;
    // Parse from the bytes (or a fresh copy of the cached body), so mutating
    // the returned config can never corrupt the ETag cache.
    const config = GossipConfig.parse(raw.text !== '' ? raw.text : JSON.stringify(raw.body));
    return {
      rateLimit: raw.meta.rateLimit,
      etag: raw.meta.etag,
      fromCache: raw.meta.fromCache,
      status: raw.meta.status,
      apiVersion: raw.meta.apiVersion,
      config,
    };
  }

  /**
   * The whole published graph: `GET <basePath>/network-map/graph`.
   *
   * Nodes decode with the same decoder as a `/peers` row. 🔴 `available:
   * false` is returned as-is, never an error — and its empty node list is not
   * a finding about the network.
   */
  async networkMapGraph(
    options: NetworkMapOptions = {},
    opts: RequestOptions = {},
  ): Promise<NetworkMapGraphResult | NotModifiedResult> {
    const raw = await this.requestJson(
      this.basePath + '/network-map/graph',
      networkMapQueryString(this.withDefaultMapNetwork(options)),
      opts,
    );
    if ('notModified' in raw) return raw;
    const nodes = raw.body['nodes'];
    return {
      ...this.networkMapBase(raw.body, raw.meta),
      truncated: bool(raw.body['truncated']),
      nodes: Array.isArray(nodes) ? nodes.map((n) => decodePeer(n)) : [],
      edges: decodeEdges(raw.body['edges']),
    };
  }

  /**
   * The graph envelope and bucket counts, without nodes or edges:
   * `GET <basePath>/network-map/summary`. `available: false` is returned
   * as-is, never an error.
   */
  async networkMapSummary(
    options: NetworkMapOptions = {},
    opts: RequestOptions = {},
  ): Promise<NetworkMapSummaryResult | NotModifiedResult> {
    const raw = await this.requestJson(
      this.basePath + '/network-map/summary',
      networkMapQueryString(this.withDefaultMapNetwork(options)),
      opts,
    );
    if ('notModified' in raw) return raw;
    return {
      ...this.networkMapBase(raw.body, raw.meta),
      connectable: decodeCounts(raw.body['connectable']),
      servesMempool: decodeCounts(raw.body['serves_mempool']),
      auction: decodeCounts(raw.body['auction']),
    };
  }

  /** The client-level `network`, unless the query sets its own (§10 D-U). */
  private withDefaultNetwork(query: PeerQuery): PeerQuery {
    if (this.network === null || query.toObject()['network'] !== undefined) return query;
    return query.network(this.network);
  }

  private withDefaultMapNetwork(options: NetworkMapOptions): NetworkMapOptions {
    if (this.network === null || options.network !== undefined) return options;
    return { ...options, network: this.network };
  }

  private networkMapBase(body: Record<string, unknown>, meta: ResultMeta): NetworkMapSummaryBase {
    const op = body['operator_nodes'];
    return {
      envelope: decodeNetworkMapEnvelope(body),
      rateLimit: meta.rateLimit,
      etag: meta.etag,
      fromCache: meta.fromCache,
      status: meta.status,
      apiVersion: meta.apiVersion,
      operatorNodes: { count: isObject(op) ? num(op['count']) : null },
      nodeCount: num(body['node_count']),
      edgeCount: num(body['edge_count']),
      tierCount: num(body['tier_count']),
      unreached: num(body['unreached']),
      tiers: decodeTiers(body['tiers']),
      cities: decodeCities(body['cities']),
      attribution: typeof body['attribution'] === 'string' ? body['attribution'] : null,
    };
  }

  /**
   * Poll a query, aligned to the data's own clock.
   *
   * Refuses an interval below `minPollIntervalMs` (default 60 s), because the
   * data does not change that often and faster polling returns identical bytes
   * and spends quota. It uses the ETag cache, so an unchanged poll costs no
   * quota unit, and prefers `next_sweep_at` plus a small per-instance jitter.
   */
  async pollPeers(
    query: PeerQuery,
    intervalMs: number,
    callback: (result: PeerListResult) => void | Promise<void>,
    opts: RequestOptions = {},
  ): Promise<void> {
    if (intervalMs < this.minPollIntervalMs) {
      throw new InvalidError(
        'interval',
        `refusing to poll faster than ${this.minPollIntervalMs} ms: the data does not ` +
          'change that often, so a faster poll returns identical bytes and spends quota',
      );
    }
    const aborted = (): boolean => opts.signal?.aborted === true;
    for (;;) {
      if (aborted()) return;
      const page = await this.listPeers(query, opts);
      let waitMs = intervalMs;
      if (!('notModified' in page)) {
        await callback(page);
        const next = page.envelope.next_sweep_at;
        if (next !== null) {
          const due = Date.parse(next);
          if (!Number.isNaN(due)) {
            waitMs = Math.max(this.minPollIntervalMs, due - this.now() + this.pollJitterMs);
          }
        }
      }
      if (aborted()) return;
      await this.sleep(waitMs);
    }
  }

  // -- transport ----------------------------------------------------------

  private async requestJson(
    path: string,
    qs: string,
    opts: RequestOptions,
  ): Promise<
    | { body: Record<string, unknown>; meta: ResultMeta; text: string }
    | NotModifiedResult
  > {
    const url = this.baseUrl + path + (qs === '' ? '' : '?' + qs);
    const key = cacheKey('GET', path, qs, this.apiKey !== null);
    const callerConditional = opts.ifNoneMatch !== undefined;
    const cached = this.cache !== null && !callerConditional ? this.cache.get(key) : undefined;
    const inm = opts.ifNoneMatch ?? cached?.etag;

    const raw = await this.send(url, inm, opts);

    if (raw.status === 304) {
      if (callerConditional || cached === undefined) {
        return {
          notModified: true,
          status: 304,
          etag: raw.headers.get('ETag'),
          rateLimit: raw.rateLimit,
          apiVersion: raw.apiVersion,
        };
      }
      // §4.5: the stored body, with the rate-limit block of THIS response.
      return {
        body: cached.body as Record<string, unknown>,
        text: '',
        meta: {
          envelope: decodeEnvelope(cached.body as Record<string, unknown>),
          rateLimit: raw.rateLimit,
          etag: raw.headers.get('ETag') ?? cached.etag,
          fromCache: true,
          status: 304,
          apiVersion: raw.apiVersion ?? cached.apiVersion ?? null,
        },
      };
    }

    if (raw.status < 200 || raw.status >= 300) {
      throw this.toError(raw);
    }

    let body: unknown;
    try {
      body = JSON.parse(raw.text) as unknown;
    } catch (cause) {
      throw new DecodeError('response body is not JSON', truncateUtf8(raw.text), {
        rateLimit: raw.rateLimit,
        apiVersion: raw.apiVersion,
        cause,
      });
    }
    if (typeof body !== 'object' || body === null || Array.isArray(body)) {
      throw new DecodeError('response body is not a JSON object', truncateUtf8(raw.text), {
        rateLimit: raw.rateLimit,
        apiVersion: raw.apiVersion,
      });
    }

    const etag = raw.headers.get('ETag');
    if (this.cache !== null && etag !== null && !callerConditional) {
      this.cache.set(key, { etag, body, storedAt: this.now(), apiVersion: raw.apiVersion });
    }
    return {
      body: body as Record<string, unknown>,
      text: raw.text,
      meta: {
        envelope: decodeEnvelope(body as Record<string, unknown>),
        rateLimit: raw.rateLimit,
        etag,
        fromCache: false,
        status: raw.status,
        apiVersion: raw.apiVersion,
      },
    };
  }

  /** One request, with the retry policy of §5.2 applied around it. */
  private async send(url: string, ifNoneMatch: string | undefined, opts: RequestOptions): Promise<RawResponse> {
    const headers: Record<string, string> = {
      Accept: 'application/json, application/problem+json',
      'User-Agent': this.userAgent,
    };
    if (this.apiKey !== null) headers['Authorization'] = `Bearer ${this.apiKey}`;
    if (ifNoneMatch !== undefined) headers['If-None-Match'] = ifNoneMatch;

    let attempt = 0;
    for (;;) {
      if (opts.deadlineMs !== undefined && this.now() > opts.deadlineMs) {
        throw new TransportError('the overall deadline passed before the request completed');
      }
      let raw: RawResponse | null = null;
      let transportCause: unknown = null;
      try {
        raw = await this.sendOnce(url, headers, opts.signal);
      } catch (cause) {
        if (isAbort(cause) && opts.signal?.aborted === true) {
          throw new TransportError('the request was aborted by the caller', { cause });
        }
        transportCause = cause;
      }

      const retriesLeft = this.maxRetries - attempt;

      if (raw === null) {
        if (retriesLeft <= 0) {
          throw new TransportError(
            `request failed: ${transportCause instanceof Error ? transportCause.message : String(transportCause)}`,
            { cause: transportCause },
          );
        }
        await this.sleep(this.backoffMs(attempt));
        attempt += 1;
        continue;
      }

      if (raw.status === 429) {
        const retryAfterMs = raw.rateLimit.retryAfter === null ? null : raw.rateLimit.retryAfter * 1000;
        // 🔴 D-I: above the cap we raise rather than block the caller.
        if (retryAfterMs !== null && retryAfterMs > this.maxRetryAfterMs) throw this.toError(raw);
        if (retriesLeft <= 0) return raw;
        await this.sleep(retryAfterMs ?? this.backoffMs(attempt));
        attempt += 1;
        continue;
      }

      // 🔴 Never retry a 4xx other than 429: it is deterministic, and retrying
      // burns quota to receive the same answer.
      if (RETRYABLE_5XX(raw.status) && retriesLeft > 0) {
        await this.sleep(this.backoffMs(attempt));
        attempt += 1;
        continue;
      }

      return raw;
    }
  }

  private async sendOnce(
    url: string,
    headers: Record<string, string>,
    signal: AbortSignal | undefined,
  ): Promise<RawResponse> {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort(new Error(`timeout after ${this.timeoutMs} ms`));
    }, this.timeoutMs);
    const onAbort = (): void => {
      controller.abort(signal?.reason);
    };
    if (signal !== undefined) {
      if (signal.aborted) onAbort();
      else signal.addEventListener('abort', onAbort, { once: true });
    }
    try {
      const res = await this.fetchImpl(url, {
        method: 'GET',
        headers,
        signal: controller.signal,
        redirect: 'follow',
      });
      const text = res.status === 304 ? '' : await res.text();
      const at = this.now();
      return {
        status: res.status,
        headers: res.headers,
        text,
        rateLimit: parseRateLimit(res.headers, at),
        apiVersion: parseApiVersion(res.headers),
        at,
      };
    } finally {
      clearTimeout(timer);
      if (signal !== undefined) signal.removeEventListener('abort', onAbort);
    }
  }

  /** `min(base * 2^n, max) * (0.5 + random()/2)`; never asserted on in tests. */
  private backoffMs(attempt: number): number {
    const raw = Math.min(500 * Math.pow(2, attempt), 30_000);
    return Math.round(raw * (0.5 + this.random() / 2));
  }

  /**
   * Map a failed response to the taxonomy. 🔴 Discrimination is on
   * `problem.type`, by suffix match on the path segment, never on `title` or
   * `detail` — prose is not an API. An absent or unrecognised `type` falls back
   * to the status code alone (D-K), so a legacy `{"error": …}` body still
   * produces the right variant.
   */
  private toError(raw: RawResponse): Error {
    const problem = parseProblemBody(raw.text);
    const opts = { rateLimit: raw.rateLimit, problem, apiVersion: raw.apiVersion };
    const detail = typeof problem?.detail === 'string' ? problem.detail : '';
    const suffix = problemSuffix(problem?.type);
    const message = detail !== '' ? detail : `HTTP ${raw.status}`;

    switch (raw.status) {
      case 400:
        switch (suffix) {
          case 'filter-unavailable':
            return new FilterUnavailableError(
              message + ' (the attribute is not collected; an empty list would have lied)',
              opts,
            );
          case 'chain-not-served':
            return new ChainNotServedError(message, opts);
          case 'cursor-expired':
            return new CursorExpiredError(
              message + ' (catch this and start the walk over; never mix two generations)',
              opts,
            );
          default:
            return new BadRequestError(message, opts);
        }
      case 401:
        return new UnauthorizedError(message, opts);
      case 404:
        return new NotFoundError(message, opts);
      case 429:
        return new RateLimitedError(message, { ...opts, now: raw.at });
      case 503:
        return new UnavailableError(message, opts);
      default:
        if (raw.status >= 500) {
          return new ServerError(
            `HTTP ${raw.status}`,
            raw.status,
            truncateUtf8(raw.text),
            problem === null ? { rateLimit: raw.rateLimit, apiVersion: raw.apiVersion } : opts,
          );
        }
        return new BadRequestError(message, opts);
    }
  }
}

function parseProblemBody(text: string): Problem | null {
  try {
    return decodeProblem(JSON.parse(text) as unknown);
  } catch {
    return null;
  }
}

function isAbort(err: unknown): boolean {
  return err instanceof Error && (err.name === 'AbortError' || err.name === 'TimeoutError');
}

export { NO_RATE_LIMIT };

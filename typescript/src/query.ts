/**
 * The typed, immutable query builder of CLIENT_SPEC.md §2, plus the
 * presets. Every setter returns a **new** `PeerQuery`; nothing mutates.
 */
import { InvalidError } from './errors.ts';
import type {
  AuctionStatus,
  Chain,
  Connectable,
  PeerState,
  ResponseFormat,
  SortKey,
} from './types.ts';

/** `tier` accepts the literal `null` — "this node was not placed". */
export type TierMember = number | null;

/** `serves_mempool` members. `null` = unknown, **not** `false`. */
export type ServesMempoolMember = boolean | null;

/** `auction` members. `null` = never seen in the auction. */
export type AuctionMember = AuctionStatus | null;

/** A CSV parameter takes a list; a pre-joined string is accepted too. */
export type CsvInput = string | readonly string[];

/** The raw parameter bag behind a {@link PeerQuery}. */
export interface PeerQueryState {
  chain?: Chain;
  cc?: CsvInput;
  region?: CsvInput;
  asn?: CsvInput;
  tier?: readonly TierMember[] | string;
  max_tier?: number;
  role?: CsvInput;
  label?: CsvInput;
  connectable?: readonly Connectable[] | string;
  accepting?: boolean;
  state?: readonly PeerState[] | string;
  country?: CsvInput;
  serves_mempool?: readonly ServesMempoolMember[] | string;
  auction?: readonly AuctionMember[] | string;
  try_new_peers?: boolean;
  max_blocks_behind?: number;
  min_uptime?: number;
  max_latency_ms?: number;
  vantage?: string;
  seen_within?: string;
  version?: CsvInput;
  sort?: SortKey;
  limit?: number;
  cursor?: string;
  fields?: CsvInput;
  format?: ResponseFormat;
}

const SEEN_WITHIN = /^[0-9]+[mhd]$/;

/** The closed enums of §2.2(5) — the only sets validated client-side. */
const CLOSED_ENUMS = {
  chain: ['Mainnet', 'Testnet'],
  sort: ['tier', 'blocks_behind', 'out_deg', 'last_seen', 'random'],
  format: ['json', 'gossip_config'],
  connectable: ['yes', 'no', 'unknown'],
  state: ['responsive', 'open', 'full', 'syncing', 'listening', 'unreachable', 'unknown'],
  serves_mempool: ['true', 'false', 'null'],
  auction: ['holds_slot', 'bidding', 'bid_before', 'null'],
} as const;

/** A list whose members may be `null`/booleans, as its wire CSV. */
function nullableCsv(value: readonly (string | number | boolean | null)[] | string): string {
  return typeof value === 'string'
    ? value
    : value.map((m) => (m === null ? 'null' : String(m))).join(',');
}

/**
 * Validate a nullable closed-enum CSV input (`serves_mempool`, `auction`) and
 * return its wire form. Shared by the `/peers` builder's rules and the
 * network-map options (CLIENT_SPEC §2.2(5), §4.8). An empty list is `undefined`.
 * @internal
 */
export function closedNullableCsv(
  key: 'serves_mempool' | 'auction',
  value: readonly (string | boolean | null)[] | string,
): string | undefined {
  const allowed: readonly string[] = CLOSED_ENUMS[key];
  const list =
    typeof value === 'string'
      ? members(value)
      : value.map((m) => (m === null ? 'null' : String(m)));
  for (const v of list) {
    if (!allowed.includes(v)) {
      throw new InvalidError(
        key,
        `${JSON.stringify(v)} is not a member of the closed enum [${allowed.join(', ')}]`,
      );
    }
  }
  return list.length === 0 ? undefined : list.join(',');
}

function csv(value: CsvInput): string {
  return typeof value === 'string' ? value : value.join(',');
}

function members(value: CsvInput): string[] {
  return csv(value)
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/**
 * RFC 3986 percent-encoding. `encodeURIComponent` leaves `!'()*` alone, which
 * are reserved sub-delims; the CSV `,` is encoded as `%2C` like anything else
 * (§2.3 — do not special-case it).
 */
export function encodeRfc3986(value: string): string {
  return encodeURIComponent(value).replace(
    /[!'()*]/g,
    (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase(),
  );
}

/**
 * An immutable, chainable query over `GET <basePath>/peers`.
 *
 * 🪤 `tier` is **exact** and `max_tier` is a **ceiling**. They are different
 * questions and this builder never maps one onto the other; both may be set at
 * once and the server ANDs them.
 */
export class PeerQuery {
  private readonly params: PeerQueryState;

  constructor(params: PeerQueryState = {}) {
    this.params = params;
  }

  private with(patch: PeerQueryState): PeerQuery {
    return new PeerQuery({ ...this.params, ...patch });
  }

  /** 🪤 Only `Mainnet` is served today; `Testnet` is an explicit 400. */
  chain(chain: Chain): PeerQuery {
    return this.with({ chain });
  }

  /**
   * ISO-3166-1 alpha-2 country codes. Upper-cased at serialization. The broad
   * geo key.
   */
  cc(cc: CsvInput): PeerQuery {
    return this.with({ cc });
  }

  /**
   * Alias of `cc` server-side (both given ⇒ the union). A separate field so
   * the URL says what you wrote. Upper-cased at serialization.
   */
  country(country: CsvInput): PeerQuery {
    return this.with({ country });
  }

  /**
   * `provider:region`, e.g. `aws:eu-central-1`.
   *
   * 🪤 `region` is published **only for cloud-hosted peers** (the cloud
   * provider's region), so it selects nothing for a bare-metal peer, and
   * it answers `400 filter-unavailable` while no geo data is loaded — raised as
   * {@link FilterUnavailableError}, never swallowed into an empty list. `cc` is
   * the broad geo key.
   */
  region(region: CsvInput): PeerQuery {
    return this.with({ region });
  }

  /**
   * Autonomous system, e.g. `AS64500`. Answers `400 filter-unavailable` while
   * no geo data is loaded; this client surfaces that, never an empty list.
   */
  asn(asn: CsvInput): PeerQuery {
    return this.with({ asn });
  }

  /**
   * **Exact** tier match. `null` is a selectable member meaning "this node
   * was not placed" and serializes as the string `null`.
   */
  tier(tier: readonly TierMember[] | string): PeerQuery {
    return this.with({ tier });
  }

  /** Tier **ceiling**, inclusive. `maxTier(1)` also returns tier 0. */
  maxTier(maxTier: number): PeerQuery {
    return this.with({ max_tier: maxTier });
  }

  /** Third-party role hint. Prefer {@link PeerQuery.label} for sentries. */
  role(role: CsvInput): PeerQuery {
    return this.with({ role });
  }

  label(label: CsvInput): PeerQuery {
    return this.with({ label });
  }

  connectable(connectable: readonly Connectable[] | string): PeerQuery {
    return this.with({ connectable });
  }

  /**
   * `true` is exactly `connectable=yes`. 🪤 An **estimate** of whether the
   * peer has room for another inbound peer, not a guarantee: it can be wrong
   * in both directions.
   */
  accepting(value = true): PeerQuery {
    return this.with({ accepting: value });
  }

  /**
   * `responsive` | `open` | `full` | `syncing` | `listening` | `unreachable` |
   * `unknown`. `listening`: the peer accepts inbound connections,
   * other details unknown. A closed enum on the filter (validated locally).
   */
  state(state: readonly PeerState[] | string): PeerQuery {
    return this.with({ state });
  }

  /**
   * Filter on the published `mempool.serves`: any of `true`, `false`, `null`.
   *
   * 🔴 `null` selects peers whose `serves` is **unknown**, which is not
   * `false`. `[true]` excludes them; unknown is not "does not serve". Answers
   * `400 filter-unavailable` while the mempool facet carries no data.
   */
  servesMempool(value: readonly ServesMempoolMember[] | string): PeerQuery {
    return this.with({ serves_mempool: value });
  }

  /**
   * Filter on `auction.status`: any of `holds_slot`, `bidding`, `bid_before`,
   * `null` (never seen in the gossip read-priority auction).
   */
  auction(value: readonly AuctionMember[] | string): PeerQuery {
    return this.with({ auction: value });
  }

  /**
   * `try_new_peers` for the server's `format=gossip_config` rendering; only
   * meaningful there (see `listPeersAsGossipConfig`).
   */
  tryNewPeers(value: boolean): PeerQuery {
    return this.with({ try_new_peers: value });
  }

  maxBlocksBehind(value: number): PeerQuery {
    return this.with({ max_blocks_behind: value });
  }

  /** Observed availability floor, in `[0, 1]`. */
  minUptime(value: number): PeerQuery {
    return this.with({ min_uptime: value });
  }

  /** 🪤 Not published today. Requires {@link PeerQuery.vantage}. */
  maxLatencyMs(value: number): PeerQuery {
    return this.with({ max_latency_ms: value });
  }

  vantage(vantage: string): PeerQuery {
    return this.with({ vantage });
  }

  /** `^[0-9]+[mhd]$`. Below `10m` selects on refresh timing, not peer health. */
  seenWithin(value: string): PeerQuery {
    return this.with({ seen_within: value });
  }

  /** 🪤 Not published today. */
  version(version: CsvInput): PeerQuery {
    return this.with({ version });
  }

  /**
   * 🪤 Clients send **no** `sort` unless you set one (CLIENT_SPEC §10, D-J), so
   * the server's default governs.
   */
  sort(sort: SortKey): PeerQuery {
    return this.with({ sort });
  }

  /** Not clamped client-side: the server clamps, per tier. */
  limit(limit: number): PeerQuery {
    return this.with({ limit });
  }

  cursor(cursor: string): PeerQuery {
    return this.with({ cursor });
  }

  fields(fields: CsvInput): PeerQuery {
    return this.with({ fields });
  }

  format(format: ResponseFormat): PeerQuery {
    return this.with({ format });
  }

  /**
   * Client-side validation, deliberately thin (§2.2). Open sets — `cc`,
   * `region`, `asn`, `role`, `label`, `version`, `fields` — are **not**
   * validated here: they may grow additively within v1, and turning a server
   * 400 that names the problem into a client guess would be worse.
   */
  validate(): void {
    const s = this.params;
    if (s.seen_within !== undefined && !SEEN_WITHIN.test(s.seen_within)) {
      throw new InvalidError(
        'seen_within',
        `expected a duration matching ^[0-9]+[mhd]$, got ${JSON.stringify(s.seen_within)}`,
      );
    }
    if (s.min_uptime !== undefined && (s.min_uptime < 0 || s.min_uptime > 1)) {
      throw new InvalidError('min_uptime', `expected a number in [0, 1], got ${s.min_uptime}`);
    }
    for (const key of ['max_tier', 'max_blocks_behind', 'max_latency_ms'] as const) {
      const v = s[key];
      if (v !== undefined && v < 0) {
        throw new InvalidError(key, `expected a non-negative integer, got ${v}`);
      }
    }
    if (s.limit !== undefined && s.limit < 1) {
      throw new InvalidError('limit', `expected an integer >= 1, got ${s.limit}`);
    }
    if (s.max_latency_ms !== undefined && s.vantage === undefined) {
      throw new InvalidError(
        'vantage',
        'max_latency_ms requires an anonymised vantage (e.g. "eu", "apac", "us")',
      );
    }
    for (const [key, allowed] of Object.entries(CLOSED_ENUMS)) {
      const raw = (s as Record<string, unknown>)[key];
      if (raw === undefined) continue;
      const values =
        key === 'chain' || key === 'sort' || key === 'format'
          ? [typeof raw === 'string' ? raw : JSON.stringify(raw)]
          : typeof raw === 'string'
            ? members(raw)
            : (raw as readonly unknown[]).map((m) =>
                m === null ? 'null' : typeof m === 'boolean' ? String(m) : (m as string),
              );
      for (const v of values) {
        if (!(allowed as readonly string[]).includes(v)) {
          throw new InvalidError(
            key,
            `${JSON.stringify(v)} is not a member of the closed enum ` +
              `[${allowed.join(', ')}]`,
          );
        }
      }
    }
  }

  /**
   * The wire parameters, **sorted lexicographically by name** so that two
   * clients producing the same query produce the same URL and the same CDN
   * cache key. Unset values and empty lists are omitted entirely.
   */
  toParams(): [string, string][] {
    const s = this.params;
    const out: [string, string][] = [];
    const put = (k: string, v: string | undefined): void => {
      if (v !== undefined && v !== '') out.push([k, v]);
    };
    const putCsv = (k: string, v: CsvInput | undefined, upper = false): void => {
      if (v === undefined) return;
      const joined = csv(v);
      put(k, upper ? joined.toUpperCase() : joined);
    };

    put('chain', s.chain);
    putCsv('cc', s.cc, true);
    putCsv('region', s.region);
    putCsv('asn', s.asn);
    if (s.tier !== undefined) {
      put(
        'tier',
        typeof s.tier === 'string'
          ? s.tier
          : s.tier.map((t) => (t === null ? 'null' : String(t))).join(','),
      );
    }
    put('max_tier', s.max_tier === undefined ? undefined : String(s.max_tier));
    putCsv('role', s.role);
    putCsv('label', s.label);
    putCsv('connectable', s.connectable);
    put('accepting', s.accepting === undefined ? undefined : String(s.accepting));
    putCsv('state', s.state);
    putCsv('country', s.country, true);
    if (s.serves_mempool !== undefined) put('serves_mempool', nullableCsv(s.serves_mempool));
    if (s.auction !== undefined) put('auction', nullableCsv(s.auction));
    put('try_new_peers', s.try_new_peers === undefined ? undefined : String(s.try_new_peers));
    put(
      'max_blocks_behind',
      s.max_blocks_behind === undefined ? undefined : String(s.max_blocks_behind),
    );
    put('min_uptime', s.min_uptime === undefined ? undefined : String(s.min_uptime));
    put('max_latency_ms', s.max_latency_ms === undefined ? undefined : String(s.max_latency_ms));
    put('vantage', s.vantage);
    put('seen_within', s.seen_within);
    putCsv('version', s.version);
    put('sort', s.sort);
    put('limit', s.limit === undefined ? undefined : String(s.limit));
    put('cursor', s.cursor);
    putCsv('fields', s.fields);
    put('format', s.format);

    out.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
    return out;
  }

  /** The canonical query string, without a leading `?`. */
  toQueryString(): string {
    return this.toParams()
      .map(([k, v]) => `${encodeRfc3986(k)}=${encodeRfc3986(v)}`)
      .join('&');
  }

  /** The parameters as a plain object, for logging and tests. */
  toObject(): Record<string, string> {
    return Object.fromEntries(this.toParams());
  }

  /** Which parameters are set, for the facets whitelist check. */
  parameterNames(): string[] {
    return this.toParams().map(([k]) => k);
  }
}

/** A fresh, empty query. */
export function peerQuery(): PeerQuery {
  return new PeerQuery();
}

/** The geo argument of the geo presets: tagged, never a bare string. */
export type GeoFilter = { cc: CsvInput; region?: never } | { region: CsvInput; cc?: never };

function applyGeo(q: PeerQuery, geo: GeoFilter): PeerQuery {
  // The tagged union makes "neither" unrepresentable in TypeScript, but a
  // JavaScript caller can still pass `{}`, so the runtime check stays.
  const raw = geo as { cc?: CsvInput; region?: CsvInput };
  if (raw.cc !== undefined) return q.cc(raw.cc);
  if (raw.region !== undefined) return q.region(raw.region);
  throw new InvalidError('geo', 'pass exactly one of {cc} or {region}');
}

/** Owner preset: `tier: [1]` — **exact**, not a ceiling. */
export function tier1(): PeerQuery {
  return peerQuery().tier([1]);
}

/**
 * Preset: `label: ["sentry"]`.
 *
 * 🪤 This is `label=sentry`, **not** `tier=0`. They are different questions
 * and may give different answers.
 */
export function sentries(): PeerQuery {
  return peerQuery().label(['sentry']);
}

/**
 * Preset: `serves_mempool: [true]` — peers confirmed to serve the
 * mempool. "Serves the mempool and in DE" is `mempoolServers().cc(['DE'])`.
 *
 * 🔴 Peers with `serves: null` (**unknown**) are **excluded**: unknown is not
 * "does not serve". Add `null` explicitly
 * (`servesMempool([true, null])`) to include the unknowns.
 */
export function mempoolServers(): PeerQuery {
  return peerQuery().servesMempool([true]);
}

/**
 * Preset: peers with a free inbound slot in a geography
 * (`accepting=true`, which is exactly `connectable=yes`).
 *
 * 🪤 **This is an estimate of whether the peer has room for another inbound
 * peer, not a guarantee; it can be wrong in both directions.**
 *
 * 🪤 The geo argument is **tagged**: `{cc: ["DE"]}` or
 * `{region: ["aws:eu-central-1"]}`. `region` is published **only for
 * cloud-hosted peers** (`provider:region`, the cloud provider's region) and
 * answers `400 filter-unavailable` while no geo data is loaded, which this
 * client raises as {@link FilterUnavailableError} rather than swallowing into
 * an empty list. `cc` is the broad geo key.
 */
export function notFull(geo: GeoFilter): PeerQuery {
  return applyGeo(peerQuery().accepting(true), geo);
}

/**
 * Hand-written types for the Peer API v1 schemas
 * (`spec/peer-api-v1.openapi.yaml`), shaped for ergonomics rather than
 * generated fidelity. Every honesty rule of CLIENT_SPEC.md §3 is encoded here:
 * three-state enums are strings with three members and are never folded into a
 * boolean, and `null` always means "not provided", never zero and never false.
 */
import type { GossipConfig } from './gossip.ts';

/** The two chain spellings. The capitalisation is load-bearing. */
export type Chain = 'Mainnet' | 'Testnet';

/**
 * Which network's peer graph to read: the `network` query parameter
 * (CLIENT_SPEC §2.1, §10 D-U). Lower-case and case-sensitive, so it never
 * blurs with the capitalised {@link Chain}. Unset sends nothing, and the
 * server answers for mainnet.
 *
 * 🪤 `testnet` is **beta**: a small graph, no session data yet, an approximate
 * `blocks_behind`, and testnet resets mean `available: false` for a while is
 * normal. A deployment that does not serve testnet answers `400
 * network-not-served`, raised as {@link BadRequestError} with
 * `parameter === "network"`.
 */
export type Network = 'mainnet' | 'testnet';

/**
 * 🪤 Three-state **strings** on the wire — `"yes"` / `"no"` / `"unknown"`, never
 * booleans — and an ESTIMATE of whether the peer has room for another inbound
 * peer, not a guarantee. `unknown` is not `no`.
 */
export type Connectable = 'yes' | 'no' | 'unknown';

/**
 * Peer state. An **open** enum: an unrecognised member decodes as-is and is
 * rendered plain. `responsive` = answered our scan; `open` = accepting new
 * inbound peers; `full` = not accepting new inbound peers right now; `syncing`
 * = catching up; `listening` = accepts inbound connections, other
 * details unknown; `unreachable` = not reachable; `unknown` = never observed.
 */
export type PeerState =
  | 'responsive'
  | 'open'
  | 'full'
  | 'syncing'
  | 'listening'
  | 'unreachable'
  | 'unknown'
  | (string & {});

/**
 * The `auction` filter's closed enum (CLIENT_SPEC §2.1). On a decoded peer,
 * {@link PeerAuction.status} is the same set but **open**: an unknown member
 * passes through.
 */
export type AuctionStatus = 'holds_slot' | 'bidding' | 'bid_before';

/**
 * Does this peer serve the mempool?
 *
 * 🔴 Every published peer carries this object; **all-`null` means unknown**.
 * `serves: null` means *unknown* — it is **not** `false`, and unknown is not
 * "does not serve". A peer from an older server that omits the key
 * decodes to the all-`null` object, never to an error.
 */
export interface PeerMempool {
  readonly serves: boolean | null;
  /** ISO-8601 instant the verdict was last observed, kept as the wire string. */
  readonly last_checked: string | null;
  /**
   * Machine token for the outcome (e.g. `serves`). An **open** set, never
   * validated: render an unknown token as plain text.
   */
  readonly verdict: string | null;
  /** Milliseconds to the first mempool message, or `null`. */
  readonly first_frame_ms: number | null;
  /**
   * `probe` | `relay`: which of our measurements produced the verdict. Open set.
   */
  readonly source: 'probe' | 'relay' | (string & {}) | null;
}

/**
 * Standing in the on-chain gossip read-priority auction (public chain data).
 * All-`null` means the peer was never seen in the auction. `slot` / `since`
 * are set only for `holds_slot`.
 */
export interface PeerAuction {
  readonly status: AuctionStatus | (string & {}) | null;
  readonly slot: number | null;
  /** ISO-8601, kept as the wire string. */
  readonly since: string | null;
  /** ISO-8601, kept as the wire string. */
  readonly last_bid_at: string | null;
}

/** Sort keys. A closed enum; an unknown value is a server 400. */
export type SortKey = 'tier' | 'blocks_behind' | 'out_deg' | 'last_seen' | 'random';

/** Response rendering. */
export type ResponseFormat = 'json' | 'gossip_config';

/** One peer as published. */
export interface Peer {
  /** The peer's IPv4. Stable identity; the graph is keyed on it. */
  readonly id: string;
  /**
   * Distance from the network's sentry nodes in the peer graph; tier 0 =
   * sentries.
   *
   * 🔴 `null` means **this node was not placed**. It is NOT tier 0, not
   * -1 and not any sentinel; clients never synthesise a value for it.
   */
  readonly tier: number | null;
  readonly state: PeerState | null;
  /** Third-party role hint — "a hint, never truth". Open set. */
  readonly role: string | null;
  /** ISO-3166-1 alpha-2. Third-party geo data; advisory. */
  readonly cc: string | null;
  /** How confident the country label is: `high` \| `medium` \| `low`. Open set. */
  readonly cc_confidence: 'high' | 'medium' | 'low' | (string & {}) | null;
  /** Source of `cc`, as published: `cloud-range` \| `db` \| `db+rtt-confirmed`. Open set. */
  readonly cc_source: 'cloud-range' | 'db' | 'db+rtt-confirmed' | (string & {}) | null;
  /**
   * `provider:region` (e.g. `aws:eu-central-1`). 🪤 Set **only for
   * cloud-hosted peers** (the cloud provider's region); `null`
   * for everyone else. Documented, not asserted.
   */
  readonly region: string | null;
  /** Autonomous system, e.g. `AS64500`. */
  readonly asn: string | null;
  /** The AS / hosting organisation name. */
  readonly org: string | null;
  /**
   * The id of this peer's city cluster (`<CC>-<city>`, e.g. `JP-tokyo`) when the
   * producer serves city positions (see {@link NetworkMapGraphResult.cities});
   * otherwise `null`. Never a coordinate.
   */
  readonly city: string | null;
  readonly operator: string | null;
  readonly name: string | null;
  readonly in_deg: number | null;
  readonly out_deg: number | null;
  /** IPv4s feeding this peer (possibly empty). `null` only when the key is absent. */
  readonly feeders: readonly string[] | null;
  readonly latest_height: number | null;
  readonly blocks_behind: number | null;
  readonly live: boolean | null;
  readonly connectable: Connectable | null;
  /** Allowlisted public tags. An unknown label is renderable, never an error. */
  readonly labels: readonly string[];
  readonly last_seen: string | null;
  /** Observed availability, a number in `[0, 1]`. `null` = not computed. */
  readonly uptime: number | null;
  /**
   * On mainnet **always `null`** (not published, upstream D-9). On
   * `network: "testnet"` (beta) the node's reported **hardfork version**, e.g.
   * `"1550"`, or `null` when the node did not report one. An opaque string,
   * never a number.
   */
  readonly version: string | null;
  /** Mempool service. 🔴 All-`null` = unknown; `serves: null` is never `false`. */
  readonly mempool: PeerMempool;
  /** Read-priority auction standing. All-`null` = never seen in the auction. */
  readonly auction: PeerAuction;
  /** Every key the server added that this version does not know about. */
  readonly extra: Readonly<Record<string, unknown>>;
}

/** The standing fields on every successful response. */
export interface Envelope {
  readonly version: string;
  /**
   * 🔴 `false` means *the producer could not build a graph*. The empty `peers`
   * list that comes with it is **not a finding about the network**, and is
   * never converted into an error by the query surface. Check this **before**
   * {@link Envelope.stale}: a dark subsystem reports both as false.
   */
  readonly available: boolean;
  readonly stale: boolean;
  readonly chain: Chain | null;
  /**
   * The body's `network`: `"testnet"` on a `network=testnet` answer, `null`
   * when absent — every mainnet body. Decoded as an open string. A testnet body
   * also states `chain: "Testnet"`.
   */
  readonly network: string | null;
  readonly generated_at: string | null;
  /** When the data is next refreshed; {@link PeerApiClient.pollPeers} aligns to it. */
  readonly next_sweep_at: string | null;
  /** The geo database's attribution line, to be shown wherever `cc` is. */
  readonly attribution: string | null;
}

/** Parsed `RateLimit-*` / `Retry-After` state. A missing header is `null`. */
export interface RateLimit {
  readonly limit: number | null;
  readonly remaining: number | null;
  /** Seconds **from now**, not an epoch. */
  readonly reset: number | null;
  /** The raw policy string, unparsed. */
  readonly policy: string | null;
  /**
   * Delay in seconds, already resolved from either form of `Retry-After`
   * (§5.2): all digits are seconds, anything else is parsed as an IMF-fixdate
   * against the client's injectable clock, and a value that is neither is
   * `null` — absent, never guessed.
   */
  readonly retryAfter: number | null;
  /** The raw header, kept for diagnostics. */
  readonly retryAfterRaw: string | null;
}

/** RFC 9457 problem document. */
export interface Problem {
  readonly type?: string;
  readonly title?: string;
  readonly status?: number;
  /** 🔴 Prose. Never parsed for numbers (CLIENT_SPEC §10, D-A). */
  readonly detail?: string;
  readonly instance?: string;
  readonly parameter?: string;
  readonly allowed?: readonly string[];
  readonly signup_url?: string;
  readonly [key: string]: unknown;
}

/** What every successful call exposes besides its body. */
export interface ResultMeta {
  readonly envelope: Envelope;
  readonly rateLimit: RateLimit;
  readonly etag: string | null;
  readonly fromCache: boolean;
  /** 200 or 304. */
  readonly status: number;
  /**
   * The `API-Version` response header (`"1"` today), or `null` if absent.
   * Informational: a different value is never an error; a breaking change
   * ships under `/v2` paths.
   */
  readonly apiVersion: string | null;
}

/** One page of peers. */
export interface PeerListResult extends ResultMeta {
  readonly peers: readonly Peer[];
  readonly count: number;
  readonly total: number | null;
  readonly nextCursor: string | null;
}

/** One peer's public dossier. */
export interface PeerResult extends ResultMeta {
  readonly peer: Peer;
}

/** One facet. */
export interface Facet {
  /**
   * 🔴 `false` means the attribute is **not collected**. An empty
   * {@link Facet.values} under `available: true` means it *is* collected and
   * nothing currently matches. Two different answers.
   */
  readonly available: boolean;
  readonly values: readonly FacetValue[];
}

export interface FacetValue {
  /** Always a string, including the selectable absent member, `"null"`. */
  readonly value: string;
  readonly count: number;
}

/** Facet discovery. */
export interface FacetsResult extends ResultMeta {
  readonly total: number | null;
  /**
   * An **open** map: `tier`, `cc`, `region`, `asn`, `role`, `label`, `state`,
   * `connectable`, `version`, `uptime`, `serves_mempool`, `auction` today, and
   * any name the server adds later.
   */
  readonly facets: Readonly<Record<string, Facet>>;
  /**
   * The geo coverage report (`peers`, `cc`, `asn`, `region`,
   * `cc_confidence`, `source`, `db_version`, …), kept loosely typed. `null`
   * when the server sends none.
   */
  readonly geoCoverage: Readonly<Record<string, unknown>> | null;
  /**
   * `true` when the named facet is collected. Use it to pre-check a filter
   * rather than discover `filter-unavailable` on a 400. An unknown facet name
   * answers `false`.
   */
  isAvailable(name: string): boolean;
}

/**
 * The result of a caller-supplied `If-None-Match` that matched: the caller
 * asked the conditional question itself, so it gets the answer, not a body.
 */
export interface NotModifiedResult {
  readonly notModified: true;
  readonly status: 304;
  readonly etag: string | null;
  readonly rateLimit: RateLimit;
  readonly apiVersion: string | null;
}

/**
 * `listPeersAsGossipConfig`: the server's `format=gossip_config` rendering,
 * parsed into a {@link GossipConfig}, plus the response metadata. The body
 * carries no envelope. Nothing is written — save it with the `gossip-fs`
 * `save()`, the one safe write path.
 */
export interface GossipConfigResult extends Omit<ResultMeta, 'envelope'> {
  /** Parsed with `GossipConfig.parse`; unknown keys preserved, not validated. */
  readonly config: GossipConfig;
}

/** The standing fields of a `/network-map/*` body. It carries no `chain`. */
export interface NetworkMapEnvelope {
  readonly version: string;
  /** 🔴 `false` = the producer could not build a graph. Returned as-is, never an error. */
  readonly available: boolean;
  readonly stale: boolean;
  /** `"testnet"` on a `network=testnet` answer; `null` when absent (mainnet). */
  readonly network: string | null;
  readonly generated_at: string | null;
}

/** One bar of the tier histogram. */
export interface NetworkMapTier {
  readonly tier: number;
  readonly count: number;
}

/** One directed edge. `span` = tier(to) - tier(from); `null` if either is unplaced. */
export interface NetworkMapEdge {
  readonly from: string;
  readonly to: string;
  readonly span: number | null;
}

/**
 * A server-side city cluster over the returned nodes. `lat`/`lon` are
 * the city's own point rounded to 0.1° — the only coordinates in the API.
 */
export interface CityCluster {
  /** Stable cluster id, `<CC>-<city>`; equals the `city` of its peers. */
  readonly id: string;
  readonly name: string;
  /** ISO-3166-1 alpha-2; always the `cc` of its peers. */
  readonly cc: string;
  readonly lat: number;
  readonly lon: number;
  /** Peers in this cluster, after any filter. */
  readonly peers: number;
  /** Peers per published tier; the key `"null"` = unreached. */
  readonly tiers: Readonly<Record<string, number>>;
}

/** What `networkMapSummary` and `networkMapGraph` share. */
export interface NetworkMapSummaryBase {
  readonly envelope: NetworkMapEnvelope;
  readonly rateLimit: RateLimit;
  readonly etag: string | null;
  readonly fromCache: boolean;
  readonly status: number;
  readonly apiVersion: string | null;
  /** `count: null` when the producer publishes its own nodes as ordinary rows. */
  readonly operatorNodes: { readonly count: number | null };
  readonly nodeCount: number | null;
  readonly edgeCount: number | null;
  readonly tierCount: number | null;
  readonly unreached: number | null;
  readonly tiers: readonly NetworkMapTier[];
  /**
   * City clusters, biggest first. 🔴 `null` when the server sent no `cities`
   * key (the producer serves no city positions) — distinct from `[]`.
   */
  readonly cities: readonly CityCluster[] | null;
  /** The credits for every geo source in use; `null` when absent. Show it with the map. */
  readonly attribution: string | null;
}

/** `GET <basePath>/network-map/graph`. */
export interface NetworkMapGraphResult extends NetworkMapSummaryBase {
  readonly truncated: boolean | null;
  /** Decoded with the same decoder as a `/peers` row. */
  readonly nodes: readonly Peer[];
  readonly edges: readonly NetworkMapEdge[];
}

/** `GET <basePath>/network-map/summary`. */
export interface NetworkMapSummaryResult extends NetworkMapSummaryBase {
  /** Zero-filled `{yes, no, unknown}` counts. An open map. */
  readonly connectable: Readonly<Record<string, number>>;
  /** Zero-filled `{true, false, null}` counts. `{}` when absent. */
  readonly servesMempool: Readonly<Record<string, number>>;
  /** Zero-filled `{holds_slot, bidding, bid_before, null}` counts. `{}` when absent. */
  readonly auction: Readonly<Record<string, number>>;
}

/** The peer-shaped input `mergePeers` / `fromPeers` accept besides bare IPs. */
export interface PeerLike {
  readonly id: string;
}

/**
 * `hl-peers` — a client for the Hyperliquid Peer API and an
 * offline `override_gossip_config.json` file algebra.
 *
 * The default host is `https://api.hypervisor.gg` (override with `baseUrl` or
 * `HL_PEER_API_BASE_URL`), with plain paths at the host root.
 *
 * Two namespaces, always separate:
 *
 * - {@link PeerApiClient} — the HTTP query surface. Needs the network.
 * - {@link GossipConfig} — pure, offline, deterministic file algebra. **Never**
 *   touches the network, a clock or the environment. The file-writing helpers
 *   live in `hl-peers/gossip-fs`, a Node-only entry point,
 *   so this module stays runtime-agnostic and browser-safe.
 *
 * 🔴 The node reads `override_gossip_config.json` **when it
 * starts**. Writing it does not steer a running node, and nothing in this
 * library will ever restart, signal or otherwise disturb your node.
 *
 * @module
 */
export { LruEtagCache, cacheKey, type CacheEntry, type EtagCache } from './cache.ts';
export {
  DEFAULT_BASE_URL,
  DEFAULT_BASE_PATH,
  normalizeBasePath,
  networkMapQueryString,
  PeerApiClient,
  VERSION,
  type FetchLike,
  type IterOptions,
  type Logger,
  type NetworkMapOptions,
  type PeerApiClientOptions,
  type PeerIterable,
  type RequestOptions,
} from './client.ts';
export type { HeaderReader } from './decode.ts';
export type { PeerApiErrorOptions } from './errors.ts';
export {
  ENVELOPE_FIELDS,
  NETWORK_MAP_CITY_FIELDS,
  PEER_AUCTION_FIELDS,
  PEER_FIELDS,
  PEER_MEMPOOL_FIELDS,
  decodeAuction,
  decodeCities,
  decodeEnvelope,
  decodeFacets,
  decodeMempool,
  decodePeer,
  parseApiVersion,
  parseRateLimit,
  parseRetryAfter,
  truncateUtf8,
} from './decode.ts';
export { unifiedDiff } from './diff.ts';
export {
  BadRequestError,
  ChainNotServedError,
  CursorExpiredError,
  DecodeError,
  FilterUnavailableError,
  InvalidError,
  NO_RATE_LIMIT,
  NotFoundError,
  PeerApiError,
  RateLimitedError,
  ServerError,
  TransportError,
  TruncatedError,
  UnauthorizedError,
  UnavailableError,
} from './errors.ts';
export type { AnyPeerApiError, ErrorKind } from './errors.ts';
export {
  GossipConfig,
  type MergeMode,
  type MergeOptions,
  type MergeResult,
  type PeerInput,
  type ValidationResult,
  type Warning,
  type WarningCode,
} from './gossip.ts';
export { IPV6_REASON, isIpv4, looksLikeIpv6, notIpv4Reason } from './ipv4.ts';
export {
  PeerQuery,
  encodeRfc3986,
  mempoolServers,
  notFull,
  peerQuery,
  sentries,
  tier1,
  type AuctionMember,
  type ServesMempoolMember,
  type CsvInput,
  type PeerQueryState,
  type GeoFilter,
  type TierMember,
} from './query.ts';
export {
  KNOWN_KEY_ORDER,
  compareCodePoints,
  encodeJsonString,
  renderGossipConfig,
} from './serialize.ts';
export type {
  AuctionStatus,
  Chain,
  CityCluster,
  Connectable,
  Envelope,
  NetworkMapEdge,
  NetworkMapEnvelope,
  NetworkMapGraphResult,
  NetworkMapSummaryBase,
  NetworkMapSummaryResult,
  NetworkMapTier,
  PeerAuction,
  PeerMempool,
  Facet,
  FacetValue,
  FacetsResult,
  GossipConfigResult,
  NotModifiedResult,
  Peer,
  PeerLike,
  PeerListResult,
  PeerResult,
  PeerState,
  Problem,
  RateLimit,
  ResponseFormat,
  ResultMeta,
  SortKey,
} from './types.ts';

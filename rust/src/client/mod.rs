//! The HTTP query surface — `CLIENT_SPEC.md` §1–§5.
//!
//! This namespace is always separate from [`crate::GossipConfig`], which never
//! touches the network. Only [`crate::update_gossip_config`] joins the two.
//!
//! ```no_run
//! # async fn run() -> hl_peers::Result<()> {
//! use hl_peers::{Client, Geo, PeerQuery};
//!
//! // Defaults to https://api.hypervisor.gg (or $HL_PEER_API_BASE_URL).
//! let client = Client::new()?;
//! let page = client.list_peers(&PeerQuery::not_full(Geo::cc(["DE"]))).await?;
//!
//! // Check `available` BEFORE `stale`: a dark subsystem reports both as false.
//! if !page.envelope().available {
//!     eprintln!("the producer could not build a graph; this is not 'no peers matched'");
//!     return Ok(());
//! }
//! for peer in &page.peers {
//!     println!("{} tier={:?}", peer.id, peer.tier);
//! }
//! # Ok(())
//! # }
//! ```

pub mod cache;
pub mod network_map;
pub mod query;
pub mod retry;
pub mod stream;
pub mod transport;
pub mod types;

#[cfg(feature = "blocking")]
pub mod blocking;

pub use network_map::{
    CityCluster, NetworkMapEdge, NetworkMapGraph, NetworkMapOpts, NetworkMapSummary, OperatorNodes,
    TierCount,
};
pub use query::{Format, Geo, PeerQuery, Sort, TierFilter};
pub use retry::{DefaultJitter, Jitter, NoJitter, RecordingSleeper, Sleeper, TokioSleeper};
pub use stream::{IterOpts, OnTruncate, PeerStream};
pub use transport::{BoxFuture, HttpRequest, HttpResponse, ReqwestTransport, Transport};
pub use types::{
    AuctionStatus, Chain, Conditional, Connectable, Envelope, Facet, FacetValue, Facets, Network,
    Peer, PeerAuction, PeerEnvelope, PeerList, PeerMempool, PeerState, Response,
};

/// Per-call options for [`Client::get_peer_with`] (§4.3).
///
/// Every field unset is exactly [`Client::get_peer`].
///
/// ```
/// # use hl_peers::{GetPeerOpts, Network};
/// let o = GetPeerOpts::new().network(Network::Testnet);
/// assert_eq!(o.network, Some(Network::Testnet));
/// ```
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct GetPeerOpts {
    /// `network` — which network's graph to look the peer up in. **Testnet
    /// (beta)**. [`None`] falls back to the client's
    /// [`ClientBuilder::network`] default and, when that is unset too, sends
    /// no query string at all (mainnet).
    pub network: Option<Network>,
}

impl GetPeerOpts {
    /// Every option unset.
    pub fn new() -> Self {
        GetPeerOpts::default()
    }

    /// Set `network`.
    pub fn network(mut self, v: Network) -> Self {
        self.network = Some(v);
        self
    }
}

use std::sync::Arc;
use std::time::Duration;

use serde::de::DeserializeOwned;

use crate::error::{body_excerpt, Error, Problem, RateLimit, Result};
use cache::EtagCache;

/// The public API host (§1). Resolution order, once, at construction:
/// [`ClientBuilder::base_url`] → `HL_PEER_API_BASE_URL` → this.
pub const DEFAULT_BASE_URL: &str = "https://api.hypervisor.gg";

/// The default route prefix — the ONE place it is spelled. The public API uses
/// **plain paths at the host root** (no `/api`, no `/v1`: the version is in
/// every body and in the `API-Version` header; CLIENT_SPEC §10 D-N).
/// [`ClientBuilder::base_path`] remains for a proxy that
/// mounts the API under a prefix.
pub const DEFAULT_BASE_PATH: &str = "";

/// `CLIENT_SPEC.md` §1: strip every leading/trailing `/`; empty means no
/// prefix, anything else is `/` + the trimmed value.
///
/// ```
/// assert_eq!(hl_peers::normalize_base_path("peer-api/v1/"), "/peer-api/v1");
/// assert_eq!(hl_peers::normalize_base_path("/"), "");
/// ```
pub fn normalize_base_path(path: &str) -> String {
    let trimmed = path.trim_matches('/');
    if trimmed.is_empty() {
        String::new()
    } else {
        format!("/{trimmed}")
    }
}

/// Route suffix under the base path.
const PEERS_PATH: &str = "/peers";
/// Route suffix under the base path.
const NETWORK_MAP_GRAPH_PATH: &str = "/network-map/graph";
/// Route suffix under the base path.
const NETWORK_MAP_SUMMARY_PATH: &str = "/network-map/summary";

/// 🔴 §4.1: `format=gossip_config` has no `peers` array.
///
/// Decoding it as a page would hand the caller an empty peer list that is not a
/// finding about the network — the one mistake §3.1 exists to prevent — so the
/// request is refused before it is sent, and the caller is pointed at
/// [`Client::list_peers_as_gossip_config`].
fn refuse_gossip_config_format(query: &PeerQuery) -> Result<()> {
    if query.format == Some(Format::GossipConfig) {
        return Err(crate::Error::Invalid {
            parameter: "format".into(),
            reason: "format=gossip_config does not return a peer list; use \
                     list_peers_as_gossip_config() instead"
                .into(),
        });
    }
    Ok(())
}
/// Route suffix under the base path.
const FACETS_PATH: &str = "/peers/facets";

fn default_user_agent() -> String {
    format!("hl-peers-rust/{}", env!("CARGO_PKG_VERSION"))
}

#[derive(Debug)]
struct Inner {
    base_url: String,
    base_path: String,
    api_key: Option<String>,
    timeout: Duration,
    user_agent: String,
    max_retries: u32,
    max_retry_after: Duration,
    min_poll_interval: Duration,
    cache: Option<EtagCache>,
    transport: Arc<dyn Transport>,
    sleeper: Arc<dyn Sleeper>,
    jitter: Arc<dyn Jitter>,
    clock: Arc<dyn crate::Clock>,
    network: Option<Network>,
}

/// The Peer API client (§1).
///
/// Cheap to clone: every clone shares one connection pool and one ETag cache.
///
/// ```
/// # use hl_peers::Client;
/// let client = Client::builder()
///     .base_url("https://api.hypervisor.gg/")
///     .user_agent_suffix("my-fleet-tool/1.0")
///     .build()
///     .unwrap();
/// assert_eq!(client.base_url(), "https://api.hypervisor.gg");
/// assert_eq!(client.base_path(), "");
/// ```
///
/// Calling without a key is **anonymous**, which is a supported tier and not
/// an error state.
#[derive(Debug, Clone)]
pub struct Client {
    inner: Arc<Inner>,
}

/// Builder for [`Client`].
pub struct ClientBuilder {
    base_url: Option<String>,
    base_path: Option<String>,
    api_key: Option<String>,
    timeout: Duration,
    user_agent: Option<String>,
    user_agent_suffix: Option<String>,
    max_retries: u32,
    max_retry_after: Duration,
    min_poll_interval: Duration,
    cache_capacity: usize,
    cache_enabled: bool,
    transport: Option<Arc<dyn Transport>>,
    sleeper: Option<Arc<dyn Sleeper>>,
    jitter: Option<Arc<dyn Jitter>>,
    clock: Option<Arc<dyn crate::Clock>>,
    network: Option<Network>,
}

impl std::fmt::Debug for ClientBuilder {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ClientBuilder")
            .field("base_url", &self.base_url)
            .field("api_key", &self.api_key.as_ref().map(|_| "hlp_live_…"))
            .field("timeout", &self.timeout)
            .finish_non_exhaustive()
    }
}

impl Default for ClientBuilder {
    fn default() -> Self {
        ClientBuilder {
            base_url: None,
            base_path: None,
            api_key: None,
            timeout: Duration::from_secs(10),
            user_agent: None,
            user_agent_suffix: None,
            max_retries: 0,
            max_retry_after: Duration::from_secs(60),
            min_poll_interval: Duration::from_secs(60),
            cache_capacity: 64,
            cache_enabled: true,
            transport: None,
            sleeper: None,
            jitter: None,
            clock: None,
            network: None,
        }
    }
}

impl ClientBuilder {
    /// Set the base URL explicitly. Highest precedence, ahead of
    /// `HL_PEER_API_BASE_URL` and [`DEFAULT_BASE_URL`].
    pub fn base_url(mut self, url: impl Into<String>) -> Self {
        self.base_url = Some(url.into());
        self
    }

    /// Set the route prefix. Default [`DEFAULT_BASE_PATH`] (`""`: plain paths
    /// at the host root); normalized with [`normalize_base_path`]. Only for a
    /// proxy that mounts the API under a prefix. Explicit only — there is no
    /// environment variable.
    pub fn base_path(mut self, path: impl Into<String>) -> Self {
        self.base_path = Some(path.into());
        self
    }

    /// Set the API key. Ahead of `HL_PEER_API_KEY`.
    ///
    /// Absent is **anonymous**, which is a supported tier and not an error.
    /// The key is sent as `Authorization: Bearer …` and is never logged, never
    /// put in a query string and never included in an error message.
    pub fn api_key(mut self, key: impl Into<String>) -> Self {
        self.api_key = Some(key.into());
        self
    }

    /// Total per-attempt timeout (connect + read). Default 10 s.
    pub fn timeout(mut self, t: Duration) -> Self {
        self.timeout = t;
        self
    }

    /// Replace the whole `User-Agent`.
    pub fn user_agent(mut self, ua: impl Into<String>) -> Self {
        self.user_agent = Some(ua.into());
        self
    }

    /// Append a suffix to the `User-Agent`, after a space, for a downstream app.
    pub fn user_agent_suffix(mut self, suffix: impl Into<String>) -> Self {
        self.user_agent_suffix = Some(suffix.into());
        self
    }

    /// Attempts **after** the first. 🔴 Default **0**: auto-retry is **off**
    /// (D-Q), so a `429` raises [`Error::RateLimited`] (with
    /// `reset_at`) and a `5xx` or transport fault raises at once — the library
    /// never sleeps behind your back unless asked. Set e.g. `3` to opt in to
    /// the §5.2 policy: `429` honours `Retry-After` up to
    /// [`ClientBuilder::max_retry_after`], `5xx`/transport back off
    /// exponentially, and no other `4xx` is ever retried.
    pub fn max_retries(mut self, n: u32) -> Self {
        self.max_retries = n;
        self
    }

    /// The `Retry-After` cap. Above it, `429` raises instead of sleeping (D-I).
    pub fn max_retry_after(mut self, d: Duration) -> Self {
        self.max_retry_after = d;
        self
    }

    /// The floor the polling helper enforces. Default 60 s.
    pub fn min_poll_interval(mut self, d: Duration) -> Self {
        self.min_poll_interval = d;
        self
    }

    /// ETag cache capacity. Default 64 entries, LRU.
    pub fn cache_capacity(mut self, n: usize) -> Self {
        self.cache_capacity = n;
        self
    }

    /// Turn the ETag cache off. It is **on** by default.
    pub fn cache_enabled(mut self, on: bool) -> Self {
        self.cache_enabled = on;
        self
    }

    /// Inject a [`Transport`]. This is the seam the fixture tests use.
    pub fn transport(mut self, t: Arc<dyn Transport>) -> Self {
        self.transport = Some(t);
        self
    }

    /// Inject a [`Sleeper`], so retry tests never sleep.
    pub fn sleeper(mut self, s: Arc<dyn Sleeper>) -> Self {
        self.sleeper = Some(s);
        self
    }

    /// Inject a [`Jitter`] source, so backoff is deterministic in a test.
    pub fn jitter(mut self, j: Arc<dyn Jitter>) -> Self {
        self.jitter = Some(j);
        self
    }

    /// The default `network` for every call that does not set its own
    /// (§1, §10 D-U). **Testnet (beta).** Default: unset, which sends no
    /// `network` key at all, so the server's default (mainnet) governs and
    /// every mainnet URL is byte-identical to a 1.1 request. A per-call value
    /// ([`PeerQuery::network`], [`NetworkMapOpts::network`],
    /// [`GetPeerOpts::network`]) wins.
    pub fn network(mut self, network: Network) -> Self {
        self.network = Some(network);
        self
    }

    /// Inject the [`Clock`](crate::Clock) §5.2 resolves an HTTP-date
    /// `Retry-After` against. The same trait §6.6 uses for `.bak` names, so a
    /// test injects one instant and both are deterministic.
    pub fn clock(mut self, clock: Arc<dyn crate::Clock>) -> Self {
        self.clock = Some(clock);
        self
    }

    /// Accepted and ignored (§1 MAY).
    pub fn suppress_placeholder_warning(self, _yes: bool) -> Self {
        self
    }

    /// Resolve the configuration and build the client.
    ///
    /// `base_url`: explicit argument → `HL_PEER_API_BASE_URL` →
    /// [`DEFAULT_BASE_URL`]. `api_key`: explicit argument → `HL_PEER_API_KEY` → none.
    /// Both are resolved **once**, here.
    pub fn build(self) -> Result<Client> {
        let base_url = self
            .base_url
            .or_else(|| std::env::var("HL_PEER_API_BASE_URL").ok())
            .unwrap_or_else(|| DEFAULT_BASE_URL.to_string());
        let api_key = self
            .api_key
            .or_else(|| std::env::var("HL_PEER_API_KEY").ok());

        let mut ua = self.user_agent.unwrap_or_else(default_user_agent);
        if let Some(sfx) = self.user_agent_suffix {
            ua.push(' ');
            ua.push_str(&sfx);
        }

        let transport: Arc<dyn Transport> = match self.transport {
            Some(t) => t,
            None => Arc::new(ReqwestTransport::new()?),
        };

        Ok(Client {
            inner: Arc::new(Inner {
                base_url: base_url.trim_end_matches('/').to_string(),
                base_path: normalize_base_path(
                    self.base_path.as_deref().unwrap_or(DEFAULT_BASE_PATH),
                ),
                api_key,
                timeout: self.timeout,
                user_agent: ua,
                max_retries: self.max_retries,
                max_retry_after: self.max_retry_after,
                min_poll_interval: self.min_poll_interval,
                cache: self
                    .cache_enabled
                    .then(|| EtagCache::new(self.cache_capacity)),
                transport,
                sleeper: self.sleeper.unwrap_or_else(|| Arc::new(TokioSleeper)),
                jitter: self
                    .jitter
                    .unwrap_or_else(|| Arc::new(DefaultJitter::default())),
                clock: self.clock.unwrap_or_else(|| Arc::new(crate::SystemClock)),
                network: self.network,
            }),
        })
    }
}

impl Client {
    /// A client with every default, resolving `HL_PEER_API_BASE_URL` and
    /// `HL_PEER_API_KEY` from the environment.
    pub fn new() -> Result<Client> {
        Client::builder().build()
    }

    /// Start configuring a client.
    pub fn builder() -> ClientBuilder {
        ClientBuilder::default()
    }

    /// The resolved base URL.
    pub fn base_url(&self) -> &str {
        &self.inner.base_url
    }

    /// The normalized route prefix; `""` means routes at the host root.
    pub fn base_path(&self) -> &str {
        &self.inner.base_path
    }

    /// Is a key being sent? (The key itself is never exposed.)
    pub fn has_api_key(&self) -> bool {
        self.inner.api_key.is_some()
    }

    /// The user agent sent on every request.
    pub fn user_agent(&self) -> &str {
        &self.inner.user_agent
    }

    /// Attempts after the first (default 0: retries off).
    pub fn max_retries(&self) -> u32 {
        self.inner.max_retries
    }

    /// The client-level default `network` ([`ClientBuilder::network`]);
    /// [`None`] = unset (nothing is sent, mainnet).
    pub fn network(&self) -> Option<Network> {
        self.inner.network
    }

    /// The polling floor this client enforces.
    pub fn min_poll_interval(&self) -> Duration {
        self.inner.min_poll_interval
    }

    // ------------------------------------------------------------- §4.1 ---

    /// One page of peers (§4.1).
    ///
    /// 🔴 An `available: false` envelope is returned **as-is** and is never
    /// converted into an error: the empty `peers` list that comes with it is
    /// not a finding about the network. Check
    /// [`Envelope::available`] before [`Envelope::stale`].
    pub async fn list_peers(&self, query: &PeerQuery) -> Result<Response<PeerList>> {
        refuse_gossip_config_format(query)?;
        let query = query.with_default_network(self.inner.network);
        self.get_json(PEERS_PATH, &query.to_query_string()?, None)
            .await
            .map(|c| match c {
                Conditional::Modified(r) => r,
                Conditional::NotModified { .. } => unreachable!("no caller validator was sent"),
            })
    }

    /// One page, with the caller asking the conditional question themselves.
    ///
    /// Because the caller supplied the validator, a `304` comes back as
    /// [`Conditional::NotModified`] rather than as a replayed body (§4.5).
    pub async fn list_peers_conditional(
        &self,
        query: &PeerQuery,
        if_none_match: &str,
    ) -> Result<Conditional<PeerList>> {
        refuse_gossip_config_format(query)?;
        let query = query.with_default_network(self.inner.network);
        self.get_json(PEERS_PATH, &query.to_query_string()?, Some(if_none_match))
            .await
    }

    /// `GET <base_path>/peers?format=gossip_config`, parsed into a
    /// [`GossipConfig`](crate::GossipConfig) (§4.7).
    ///
    /// The caller's `format` is overridden; a caller-set `try_new_peers` is
    /// sent. The body is parsed with [`GossipConfig::parse`](crate::GossipConfig::parse)
    /// and returned with the response metadata (`rate_limit`, `etag`,
    /// `from_cache`, `api_version`, `status`), so you can edit it offline
    /// before writing it.
    ///
    /// This does **not** write. Write with the one safe write path,
    /// [`GossipConfig::save`](crate::GossipConfig::save) — validate, atomic
    /// rename, mode `0600`, optional `.bak`:
    ///
    /// ```no_run
    /// # async fn run() -> hl_peers::Result<()> {
    /// use hl_peers::{Client, GossipConfig, PeerQuery, SaveOpts};
    ///
    /// let client = Client::new()?;
    /// let resp = client
    ///     .list_peers_as_gossip_config(&PeerQuery::mempool_servers().cc(["DE"]))
    ///     .await?;
    /// print!("{}", resp.body.to_json()); // the safest option: place it yourself
    /// if let Some(path) = GossipConfig::default_config_path() {
    ///     let saved = resp.body.save(path, &SaveOpts::default())?;
    ///     for w in &saved.warnings {
    ///         eprintln!("warning: {w}"); // never swallow these
    ///     }
    /// }
    /// # Ok(())
    /// # }
    /// ```
    ///
    /// 🔴 The node reads the file when it starts; this library
    /// never restarts, signals or reloads anything. The server never emits an
    /// empty root list — it answers `400 empty-root-list`, a
    /// [`Error::BadRequest`]. 🪤 `format=gossip_config` ignores `fields` and
    /// applies its own ordering and size cap: it is a *rendering* of the
    /// result, not a projection of it.
    pub async fn list_peers_as_gossip_config(
        &self,
        query: &PeerQuery,
    ) -> Result<Response<crate::GossipConfig>> {
        let q = query
            .with_default_network(self.inner.network)
            .into_owned()
            .format(Format::GossipConfig);
        let raw = self
            .get_raw(PEERS_PATH, &q.to_query_string()?, None)
            .await?;
        let RawOutcome::Body {
            bytes,
            rate_limit,
            etag,
            from_cache,
            status,
            api_version,
        } = raw
        else {
            unreachable!("no caller validator was sent")
        };
        let text = std::str::from_utf8(&bytes).map_err(|e| Error::Decode {
            cause: format!("the gossip_config body is not UTF-8: {e}"),
            body_excerpt: body_excerpt(&bytes, 512),
        })?;
        let cfg = crate::GossipConfig::parse(text)?;
        Ok(Response {
            body: cfg,
            rate_limit,
            etag,
            from_cache,
            status,
            api_version,
        })
    }

    // ------------------------------------------------------------- §4.2 ---

    /// A lazy stream over every page (§4.2).
    ///
    /// The walk terminates when `next_cursor` is `null`, when `peers` is empty,
    /// when `available == false` — **without raising** — or when `max_items`
    /// is reached.
    ///
    /// 🪤 A cursor is bound to the graph generation it was issued in. Paging
    /// across a data refresh answers `400 cursor-expired`, which this stream
    /// surfaces as [`Error::CursorExpired`] and never auto-restarts: restarting
    /// would mix two generations into one result set, which is exactly what the
    /// server refused to do. Catch it and start the walk over.
    ///
    /// The whole directory is ~900 nodes, which is 18 anonymous pages at
    /// `limit=50`.
    pub fn iter_peers(&self, query: &PeerQuery, opts: IterOpts) -> PeerStream {
        PeerStream::new(
            self.clone(),
            query.with_default_network(self.inner.network).into_owned(),
            opts,
        )
    }

    /// Eager convenience over [`Client::iter_peers`], with the same guard.
    pub async fn list_all_peers(&self, query: &PeerQuery, opts: IterOpts) -> Result<Vec<Peer>> {
        self.iter_peers(query, opts).collect_all().await
    }

    // ------------------------------------------------------------- §4.3 ---

    /// One peer's public dossier (§4.3).
    ///
    /// The IPv4 shape is checked locally first, so a typo does not spend a
    /// quota unit — but a server `400` still maps to [`Error::BadRequest`] and
    /// a `404` to [`Error::NotFound`], distinctly. A malformed IPv4 is a `400`;
    /// a well-formed IPv4 that appears in no source is a `404`. IPv6 is
    /// rejected locally: the API is IPv4-only.
    pub async fn get_peer(&self, ip: &str) -> Result<Response<PeerEnvelope>> {
        self.get_peer_with(ip, &GetPeerOpts::default()).await
    }

    /// [`Client::get_peer`] with per-call options — today only `network`
    /// (**Testnet (beta)**, §4.3). `network` is the only query parameter this
    /// route takes; unset (here and on the client) sends none.
    ///
    /// ```no_run
    /// # async fn run() -> hl_peers::Result<()> {
    /// use hl_peers::{Client, GetPeerOpts, Network};
    /// let client = Client::new()?;
    /// let r = client
    ///     .get_peer_with("192.0.2.50", &GetPeerOpts::new().network(Network::Testnet))
    ///     .await?;
    /// println!("hardfork version: {:?}", r.body.peer.version);
    /// # Ok(())
    /// # }
    /// ```
    pub async fn get_peer_with(
        &self,
        ip: &str,
        opts: &GetPeerOpts,
    ) -> Result<Response<PeerEnvelope>> {
        if !crate::gossip_is_ipv4(ip) {
            let reason = if ip.contains(':') {
                "the peer API is IPv4-only; an IPv6 literal is out of contract".to_string()
            } else {
                format!("{ip} is not an IPv4 address")
            };
            return Err(Error::invalid("ip", reason));
        }
        let path = format!("{PEERS_PATH}/{}", query::percent_encode(ip));
        let qs = match opts.network.or(self.inner.network) {
            Some(n) => format!("network={}", n.as_str()),
            None => String::new(),
        };
        self.get_json(&path, &qs, None).await.map(|c| match c {
            Conditional::Modified(r) => r,
            Conditional::NotModified { .. } => unreachable!("no caller validator was sent"),
        })
    }

    // ------------------------------------------------------------- §4.4 ---

    /// Facet counts (§4.4), over the set the query's filters select.
    ///
    /// Takes every **filter** of the query builder. `sort`, `limit`,
    /// `cursor`, `fields`, `format` and `try_new_peers` are an
    /// [`Error::Invalid`] client-side, because the endpoint would ignore them
    /// and you would believe they applied.
    ///
    /// 🔴 A facet with `available: false` is **not collected**; an empty
    /// `values` under `available: true` means it is collected and nothing
    /// matches. Use [`Facets::available`] to pre-check a filter.
    pub async fn facets(&self, query: Option<&PeerQuery>) -> Result<Response<Facets>> {
        let qs = match (query, self.inner.network) {
            (Some(q), fallback) => {
                q.validate_for_facets()?;
                q.with_default_network(fallback).to_query_string()?
            }
            (None, Some(n)) => PeerQuery::new().network(n).to_query_string()?,
            (None, None) => String::new(),
        };
        self.get_json(FACETS_PATH, &qs, None)
            .await
            .map(|c| match c {
                Conditional::Modified(r) => r,
                Conditional::NotModified { .. } => unreachable!("no caller validator was sent"),
            })
    }

    // ------------------------------------------------------------- §4.8 ---

    /// `GET <base_path>/network-map/graph` (§4.8): nodes (decoded as [`Peer`]),
    /// edges and the tier histogram.
    ///
    /// 🔴 `available: false` is returned as-is, never an error.
    pub async fn network_map_graph(
        &self,
        opts: &NetworkMapOpts,
    ) -> Result<Response<NetworkMapGraph>> {
        let opts = opts.with_default_network(self.inner.network);
        self.get_json(NETWORK_MAP_GRAPH_PATH, &opts.to_query_string()?, None)
            .await
            .map(|c| match c {
                Conditional::Modified(r) => r,
                Conditional::NotModified { .. } => unreachable!("no caller validator was sent"),
            })
    }

    /// `GET <base_path>/network-map/summary` (§4.8): the graph envelope and
    /// bucket counts without nodes or edges.
    ///
    /// 🔴 `available: false` is returned as-is, never an error.
    pub async fn network_map_summary(
        &self,
        opts: &NetworkMapOpts,
    ) -> Result<Response<NetworkMapSummary>> {
        let opts = opts.with_default_network(self.inner.network);
        self.get_json(NETWORK_MAP_SUMMARY_PATH, &opts.to_query_string()?, None)
            .await
            .map(|c| match c {
                Conditional::Modified(r) => r,
                Conditional::NotModified { .. } => unreachable!("no caller validator was sent"),
            })
    }

    // ------------------------------------------------------------- §4.6 ---

    /// The polling helper (§4.6).
    ///
    /// Refuses an interval below `min_poll_interval` (default 60 s), because
    /// polling faster than the data refreshes returns identical
    /// bytes and spends quota. It uses the ETag cache, so an unchanged poll
    /// costs no quota unit, and it prefers the data's own clock: when
    /// `next_sweep_at` is present it sleeps until then plus 5–15 s of jitter.
    ///
    /// The callback returns `false` to stop.
    pub async fn poll_peers<F>(
        &self,
        query: &PeerQuery,
        interval: Duration,
        mut callback: F,
    ) -> Result<()>
    where
        F: FnMut(Result<Response<PeerList>>) -> bool + Send,
    {
        if interval < self.inner.min_poll_interval {
            return Err(Error::invalid(
                "interval",
                format!(
                    "{:?} is below min_poll_interval ({:?}); polling faster than the data refreshes returns identical bytes and spends quota",
                    interval, self.inner.min_poll_interval
                ),
            ));
        }
        loop {
            let result = self.list_peers(query).await;
            let next_sweep = result
                .as_ref()
                .ok()
                .and_then(|r| r.body.envelope.next_sweep_at.clone());
            if !callback(result) {
                return Ok(());
            }
            // 5–15 s of jitter past the refresh, randomised per client instance.
            let jitter =
                Duration::from_secs_f64(5.0 + 10.0 * (self.inner.jitter.factor() - 0.5) * 2.0);
            let delay = match next_sweep {
                Some(_) => interval.max(self.inner.min_poll_interval) + jitter,
                None => interval,
            };
            self.inner.sleeper.sleep(delay).await;
        }
    }

    // --------------------------------------------------------- internals ---

    pub(crate) async fn get_json<T: DeserializeOwned>(
        &self,
        path: &str,
        query_string: &str,
        caller_if_none_match: Option<&str>,
    ) -> Result<Conditional<T>> {
        let raw = self
            .get_raw(path, query_string, caller_if_none_match)
            .await?;
        match raw {
            RawOutcome::NotModified {
                etag,
                rate_limit,
                api_version,
            } => Ok(Conditional::NotModified {
                etag,
                rate_limit,
                api_version,
            }),
            RawOutcome::Body {
                bytes,
                rate_limit,
                etag,
                from_cache,
                status,
                api_version,
            } => {
                let body: T = serde_json::from_slice(&bytes).map_err(|e| Error::Decode {
                    cause: e.to_string(),
                    body_excerpt: body_excerpt(&bytes, 512),
                })?;
                Ok(Conditional::Modified(Response {
                    body,
                    rate_limit,
                    etag,
                    from_cache,
                    status,
                    api_version,
                }))
            }
        }
    }

    async fn get_raw(
        &self,
        path: &str,
        query_string: &str,
        caller_if_none_match: Option<&str>,
    ) -> Result<RawOutcome> {
        let path = &format!("{}{}", self.inner.base_path, path);
        let url = if query_string.is_empty() {
            format!("{}{}", self.inner.base_url, path)
        } else {
            format!("{}{}?{}", self.inner.base_url, path, query_string)
        };

        let cache_key = EtagCache::key("GET", path, query_string, self.inner.api_key.is_some());
        let cached = match (&self.inner.cache, caller_if_none_match) {
            (Some(c), None) => c.get(&cache_key),
            _ => None,
        };

        let mut headers: Vec<(String, String)> = vec![
            ("Accept".into(), "application/json".into()),
            ("User-Agent".into(), self.inner.user_agent.clone()),
        ];
        if let Some(key) = &self.inner.api_key {
            headers.push(("Authorization".into(), format!("Bearer {key}")));
        }
        if let Some(inm) = caller_if_none_match {
            headers.push(("If-None-Match".into(), inm.to_string()));
        } else if let Some(entry) = &cached {
            headers.push(("If-None-Match".into(), entry.etag.clone()));
        }

        let request = transport::HttpRequest {
            method: "GET",
            url,
            headers,
            timeout: self.inner.timeout,
        };

        let mut attempt: u32 = 0;
        loop {
            let outcome = self.inner.transport.execute(request.clone()).await;
            let remaining = self.inner.max_retries.saturating_sub(attempt);

            let response = match outcome {
                Ok(r) => r,
                Err(e) => {
                    if remaining > 0 {
                        let d = retry::backoff(
                            attempt,
                            Duration::from_millis(500),
                            Duration::from_secs(30),
                            self.inner.jitter.as_ref(),
                        );
                        self.inner.sleeper.sleep(d).await;
                        attempt += 1;
                        continue;
                    }
                    return Err(e);
                }
            };

            let now = self.inner.clock.now_unix();
            let rate_limit = RateLimit::from_headers_at(response.headers.clone(), now);
            let etag = response.header("ETag").map(str::to_string);
            let api_version = response.header("API-Version").map(str::to_string);

            if response.status == 304 {
                if caller_if_none_match.is_some() {
                    return Ok(RawOutcome::NotModified {
                        etag,
                        rate_limit,
                        api_version,
                    });
                }
                // The cache's own validator matched: replay the stored body,
                // with the rate-limit headers from the 304 itself.
                let Some(entry) = cached else {
                    return Err(Error::Decode {
                        cause: "304 without a cached body; the cache entry was evicted mid-flight"
                            .into(),
                        body_excerpt: String::new(),
                    });
                };
                return Ok(RawOutcome::Body {
                    bytes: entry.body,
                    rate_limit,
                    etag,
                    from_cache: true,
                    status: 304,
                    api_version,
                });
            }

            if (200..300).contains(&response.status) {
                if let (Some(cache), Some(tag)) = (&self.inner.cache, &etag) {
                    cache.put(&cache_key, tag.clone(), response.body.clone());
                }
                return Ok(RawOutcome::Body {
                    bytes: response.body,
                    rate_limit,
                    etag,
                    from_cache: false,
                    status: response.status,
                    api_version,
                });
            }

            let error = map_error(&response, rate_limit, api_version, now);
            match self.retry_decision(&error, attempt, remaining) {
                retry::Decision::Raise => return Err(error),
                retry::Decision::RetryAfter(ms) => {
                    self.inner.sleeper.sleep(Duration::from_millis(ms)).await;
                    attempt += 1;
                }
            }
        }
    }

    fn retry_decision(&self, error: &Error, attempt: u32, remaining: u32) -> retry::Decision {
        if remaining == 0 {
            return retry::Decision::Raise;
        }
        match error {
            // 🔴 Never retry a 4xx other than 429.
            Error::RateLimited { retry_after, .. } => match retry_after {
                Some(secs) => {
                    let d = Duration::from_secs(*secs);
                    if d > self.inner.max_retry_after {
                        // D-I: raise rather than block the caller for minutes.
                        retry::Decision::Raise
                    } else {
                        retry::Decision::RetryAfter(d.as_millis() as u64)
                    }
                }
                None => retry::Decision::RetryAfter(
                    retry::backoff(
                        attempt,
                        Duration::from_millis(500),
                        Duration::from_secs(30),
                        self.inner.jitter.as_ref(),
                    )
                    .as_millis() as u64,
                ),
            },
            Error::Unavailable { .. } | Error::Server { .. } => retry::Decision::RetryAfter(
                retry::backoff(
                    attempt,
                    Duration::from_millis(500),
                    Duration::from_secs(30),
                    self.inner.jitter.as_ref(),
                )
                .as_millis() as u64,
            ),
            _ => retry::Decision::Raise,
        }
    }
}

enum RawOutcome {
    NotModified {
        etag: Option<String>,
        rate_limit: RateLimit,
        api_version: Option<String>,
    },
    Body {
        bytes: Vec<u8>,
        rate_limit: RateLimit,
        etag: Option<String>,
        from_cache: bool,
        status: u16,
        api_version: Option<String>,
    },
}

/// Map an error response to its taxonomy variant (§5.1).
///
/// Discrimination is on `problem.type`, by suffix match on the path segment,
/// never on `title` or `detail`. D-K: a body that is not problem+json still
/// produces the right variant, by status alone.
///
/// `now_unix` is the client's injectable clock at response time, which
/// `RateLimited.reset_at` is computed against (§5.1).
fn map_error(
    response: &transport::HttpResponse,
    rate_limit: RateLimit,
    api_version: Option<String>,
    now_unix: i64,
) -> Error {
    let problem: Option<Problem> = serde_json::from_slice::<Problem>(&response.body)
        .ok()
        .filter(|p| p.type_.is_some() || p.title.is_some() || p.status.is_some());
    let detail = problem.as_ref().and_then(|p| p.detail.clone());
    let parameter = problem.as_ref().and_then(|p| p.parameter.clone());
    let allowed = problem.as_ref().and_then(|p| p.allowed.clone());
    let signup_url = problem.as_ref().and_then(|p| p.signup_url.clone());
    let suffix = problem
        .as_ref()
        .and_then(|p| p.type_suffix())
        .unwrap_or_default()
        .to_string();
    let problem = problem.map(Box::new);

    match response.status {
        429 => Error::RateLimited {
            reset_at: rate_limit
                .retry_after
                .or(rate_limit.reset)
                .and_then(|secs| {
                    let at = u64::try_from(now_unix).ok()?.checked_add(secs)?;
                    std::time::UNIX_EPOCH.checked_add(Duration::from_secs(at))
                }),
            retry_after: rate_limit.retry_after,
            limit: rate_limit.limit,
            remaining: rate_limit.remaining,
            reset: rate_limit.reset,
            policy: rate_limit.policy.clone(),
            signup_url,
            problem,
            rate_limit,
            api_version,
        },
        400 => match suffix.as_str() {
            "filter-unavailable" => Error::FilterUnavailable {
                filter: parameter,
                detail,
                problem,
                rate_limit,
                api_version,
            },
            "chain-not-served" => Error::ChainNotServed {
                allowed: allowed.unwrap_or_default(),
                problem,
                rate_limit,
                api_version,
            },
            "cursor-expired" => Error::CursorExpired {
                problem,
                rate_limit,
                api_version,
            },
            _ => Error::BadRequest {
                parameter,
                allowed,
                detail,
                problem,
                rate_limit,
                api_version,
            },
        },
        401 => Error::Unauthorized {
            detail,
            problem,
            rate_limit,
            api_version,
        },
        404 => Error::NotFound {
            detail,
            path: None,
            problem,
            rate_limit,
            api_version,
        },
        503 => Error::Unavailable {
            detail,
            problem,
            rate_limit,
            api_version,
        },
        s if (400..500).contains(&s) => Error::BadRequest {
            parameter,
            allowed,
            detail,
            problem,
            rate_limit,
            api_version,
        },
        s => Error::Server {
            status: s,
            body_excerpt: body_excerpt(&response.body, 512),
            rate_limit,
            api_version,
        },
    }
}

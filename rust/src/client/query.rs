//! The typed, immutable, chainable query builder — `CLIENT_SPEC.md` §2.
//!
//! Every setter takes `self` and returns a new value; nothing mutates in
//! place. Closed enums are Rust enums; open sets (`cc`, `region`, `asn`,
//! `role`, `label`, `version`, `fields`) are strings, because validating them
//! locally would turn a server `400` that *names* the problem into a
//! client-side error that guesses at it.

use crate::client::types::{AuctionStatus, Chain, Connectable, Network, PeerState};
use crate::error::{Error, Result};

/// Sort key (§2.1). Clients send **no** `sort` unless the caller sets one, so
/// the server's default governs (D-J).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum Sort {
    /// Ascending tier.
    Tier,
    /// Ascending blocks behind.
    BlocksBehind,
    /// Descending out-degree.
    OutDeg,
    /// Most recently seen first.
    LastSeen,
    /// Seeded per key per data refresh. It exists so that not every client picks the
    /// same head peers and turns this API into a centralising force on the
    /// network it describes.
    Random,
}

impl Sort {
    /// The wire spelling.
    pub fn as_str(&self) -> &'static str {
        match self {
            Sort::Tier => "tier",
            Sort::BlocksBehind => "blocks_behind",
            Sort::OutDeg => "out_deg",
            Sort::LastSeen => "last_seen",
            Sort::Random => "random",
        }
    }

    /// Parse the wire spelling.
    pub fn parse(s: &str) -> Result<Sort> {
        match s {
            "tier" => Ok(Sort::Tier),
            "blocks_behind" => Ok(Sort::BlocksBehind),
            "out_deg" => Ok(Sort::OutDeg),
            "last_seen" => Ok(Sort::LastSeen),
            "random" => Ok(Sort::Random),
            other => Err(Error::invalid(
                "sort",
                format!("{other} is not one of tier, blocks_behind, out_deg, last_seen, random"),
            )),
        }
    }
}

/// Response format (§2.1).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum Format {
    /// The normal JSON peer list.
    Json,
    /// The drop-in `override_gossip_config.json`. 🪤 It ignores `fields` and
    /// applies its own ordering and size cap: it is a *rendering* of the
    /// result, not a projection of it.
    GossipConfig,
}

impl Format {
    /// The wire spelling.
    pub fn as_str(&self) -> &'static str {
        match self {
            Format::Json => "json",
            Format::GossipConfig => "gossip_config",
        }
    }

    /// Parse the wire spelling. The CLI also accepts `gossip-config`.
    pub fn parse(s: &str) -> Result<Format> {
        match s {
            "json" => Ok(Format::Json),
            "gossip_config" | "gossip-config" => Ok(Format::GossipConfig),
            other => Err(Error::invalid(
                "format",
                format!("{other} is not one of json, gossip_config"),
            )),
        }
    }
}

/// An exact `tier` member. `null` is a **selectable** member meaning "never placed
/// in the peer graph" — it is not tier 0.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TierFilter {
    /// An exact tier.
    Exact(i64),
    /// The literal `null` member.
    Unplaced,
}

impl TierFilter {
    fn as_wire(&self) -> String {
        match self {
            TierFilter::Exact(n) => n.to_string(),
            TierFilter::Unplaced => "null".to_string(),
        }
    }
}

/// The geo argument of the [`PeerQuery::not_full`] preset — **tagged**, never a bare string.
///
/// 🪤 `region` is published **only for cloud-hosted peers** (`provider:region`,
/// for cloud-hosted peers) and answers `400 filter-unavailable` while
/// no geo data is loaded; `cc` is the broad geo key. That `400` is surfaced as
/// [`Error::FilterUnavailable`] and is never swallowed into an empty list.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Geo {
    /// ISO-3166-1 alpha-2 country codes. The broad geo key.
    Cc(Vec<String>),
    /// `provider:region` labels, e.g. `aws:eu-central-1`. Cloud-hosted peers
    /// only; `filter-unavailable` while no geo data is loaded.
    Region(Vec<String>),
}

impl Geo {
    /// Country codes.
    pub fn cc<I, S>(codes: I) -> Geo
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        Geo::Cc(codes.into_iter().map(|s| s.as_ref().to_string()).collect())
    }

    /// `provider:region` labels. 🪤 Cloud-hosted peers only, and
    /// `filter-unavailable` while no geo data is loaded.
    pub fn region<I, S>(labels: I) -> Geo
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        Geo::Region(labels.into_iter().map(|s| s.as_ref().to_string()).collect())
    }
}

fn strings<I, S>(v: I) -> Vec<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    v.into_iter().map(|s| s.as_ref().to_string()).collect()
}

/// A query over `GET <base_path>/peers` (§2).
///
/// ```
/// # use hl_peers::{PeerQuery, Geo};
/// let q = PeerQuery::not_full(Geo::cc(["DE"]));
/// assert_eq!(q.to_query_string().unwrap(), "accepting=true&cc=DE");
/// ```
#[derive(Debug, Clone, Default, PartialEq)]
#[non_exhaustive]
pub struct PeerQuery {
    /// `chain`.
    pub chain: Option<Chain>,
    /// `network` — which network's graph to read. **Testnet (beta)**, §10
    /// D-U. [`None`] falls back to the client's
    /// [`ClientBuilder::network`](crate::ClientBuilder::network) default and,
    /// when that is unset too, sends nothing (the server reads mainnet).
    pub network: Option<Network>,
    /// `cc`, CSV, upper-cased at serialization.
    pub cc: Vec<String>,
    /// `region` (`provider:region`). 🪤 Cloud-hosted peers only.
    pub region: Vec<String>,
    /// `asn`.
    pub asn: Vec<String>,
    /// `tier` — **exact** match, not a ceiling.
    pub tier: Vec<TierFilter>,
    /// `max_tier` — a **ceiling**, not a synonym for `tier`.
    pub max_tier: Option<i64>,
    /// `role`. Open set.
    pub role: Vec<String>,
    /// `label`. Open set; prefer it over `role` for sentry selection.
    pub label: Vec<String>,
    /// `connectable`.
    pub connectable: Vec<crate::client::types::Connectable>,
    /// `accepting` — exactly `connectable=yes`.
    pub accepting: Option<bool>,
    /// `state`. Closed as a filter: an `Other` member is refused locally.
    pub state: Vec<crate::client::types::PeerState>,
    /// `country`, CSV, upper-cased. A server-side alias of `cc` (both given ⇒
    /// union); a separate field so the URL says what the caller wrote.
    pub country: Vec<String>,
    /// `serves_mempool`: `Some(true)`, `Some(false)`, or [`None`] for the
    /// literal `null` member — **unknown**, which is not `false`.
    pub serves_mempool: Vec<Option<bool>>,
    /// `auction`: a status, or [`None`] for the literal `null` member (never
    /// seen in the auction). Closed as a filter.
    pub auction: Vec<Option<AuctionStatus>>,
    /// `try_new_peers`. Only meaningful with `format=gossip_config`.
    pub try_new_peers: Option<bool>,
    /// `max_blocks_behind`.
    pub max_blocks_behind: Option<i64>,
    /// `min_uptime`, 0..1.
    pub min_uptime: Option<f64>,
    /// `max_latency_ms`. Requires `vantage`.
    pub max_latency_ms: Option<i64>,
    /// `vantage`.
    pub vantage: Option<String>,
    /// `seen_within`, matching `^[0-9]+[mhd]$`.
    pub seen_within: Option<String>,
    /// `version`. Open set; not published today.
    pub version: Vec<String>,
    /// `sort`. Unset by default (D-J).
    pub sort: Option<Sort>,
    /// `limit`. **Not** clamped client-side — the server clamps, and clamping
    /// locally would hide the tier ceiling from the caller.
    pub limit: Option<i64>,
    /// `cursor`, opaque.
    pub cursor: Option<String>,
    /// `fields`. Open set.
    pub fields: Vec<String>,
    /// `format`.
    pub format: Option<Format>,
    /// Parameters this build does not model, sent verbatim. See
    /// [`PeerQuery::extra_param`].
    pub extra: Vec<(String, String)>,
}

/// Every wire parameter of `GET <base_path>/peers` this build models (§2.1).
const KNOWN_PARAMS: &[&str] = &[
    "accepting",
    "asn",
    "auction",
    "cc",
    "chain",
    "connectable",
    "country",
    "cursor",
    "fields",
    "format",
    "label",
    "limit",
    "max_blocks_behind",
    "max_latency_ms",
    "max_tier",
    "min_uptime",
    "network",
    "region",
    "role",
    "seen_within",
    "serves_mempool",
    "sort",
    "state",
    "tier",
    "try_new_peers",
    "vantage",
    "version",
];

macro_rules! setter {
    ($(#[$m:meta])* $name:ident, $field:ident, $ty:ty) => {
        $(#[$m])*
        pub fn $name(mut self, value: $ty) -> Self {
            self.$field = Some(value);
            self
        }
    };
}

macro_rules! list_setter {
    ($(#[$m:meta])* $name:ident, $field:ident) => {
        $(#[$m])*
        pub fn $name<I, S>(mut self, values: I) -> Self
        where
            I: IntoIterator<Item = S>,
            S: AsRef<str>,
        {
            self.$field = strings(values);
            self
        }
    };
}

impl PeerQuery {
    /// An empty query: every filter unset.
    pub fn new() -> Self {
        PeerQuery::default()
    }

    setter! {
        /// Set `chain`.
        chain, chain, Chain
    }
    setter! {
        /// Set `network` — which network's graph to read. **Testnet (beta)**.
        ///
        /// ```
        /// # use hl_peers::{Network, PeerQuery};
        /// let q = PeerQuery::new().network(Network::Testnet).cc(["jp"]);
        /// assert_eq!(q.to_query_string().unwrap(), "cc=JP&network=testnet");
        /// ```
        network, network, Network
    }
    list_setter! {
        /// Set `cc`. A list, never a hand-built CSV — though a pre-joined
        /// `"DE,NL"` is accepted as a single member too.
        cc, cc
    }
    list_setter! {
        /// Set `region` (`provider:region`). 🪤 Published only for cloud-hosted
        /// peers, and `filter-unavailable` while no geo data is loaded.
        region, region
    }
    list_setter! {
        /// Set `asn`.
        asn, asn
    }
    list_setter! {
        /// Set `country` — upper-cased at serialization. The server treats it
        /// as an alias of `cc` (both given ⇒ union).
        country, country
    }
    list_setter! {
        /// Set `role`.
        role, role
    }
    list_setter! {
        /// Set `label`.
        label, label
    }
    list_setter! {
        /// Set `version`.
        version, version
    }
    list_setter! {
        /// Set `fields`. `id` is always included by the server.
        fields, fields
    }
    setter! {
        /// Set `max_tier` — a ceiling. `max_tier=1` also returns tier 0.
        max_tier, max_tier, i64
    }
    setter! {
        /// Set `accepting`.
        accepting, accepting, bool
    }
    setter! {
        /// Set `max_blocks_behind`.
        max_blocks_behind, max_blocks_behind, i64
    }
    setter! {
        /// Set `min_uptime`, `0..=1`.
        min_uptime, min_uptime, f64
    }
    setter! {
        /// Set `max_latency_ms`. Requires `vantage`.
        max_latency_ms, max_latency_ms, i64
    }
    setter! {
        /// Set `try_new_peers`. Only meaningful with `format=gossip_config`
        /// ([`Client::list_peers_as_gossip_config`](crate::Client::list_peers_as_gossip_config)).
        try_new_peers, try_new_peers, bool
    }
    setter! {
        /// Set `sort`.
        sort, sort, Sort
    }
    setter! {
        /// Set `limit`.
        limit, limit, i64
    }
    setter! {
        /// Set `format`.
        format, format, Format
    }

    /// Set `tier` — **exact** tiers. `tier` and `max_tier` are different
    /// questions and this crate will not map one to the other.
    pub fn tier<I: IntoIterator<Item = TierFilter>>(mut self, tiers: I) -> Self {
        self.tier = tiers.into_iter().collect();
        self
    }

    /// Set `connectable`.
    pub fn connectable<I: IntoIterator<Item = crate::client::types::Connectable>>(
        mut self,
        v: I,
    ) -> Self {
        self.connectable = v.into_iter().collect();
        self
    }

    /// Set `state`.
    pub fn state<I: IntoIterator<Item = crate::client::types::PeerState>>(mut self, v: I) -> Self {
        self.state = v.into_iter().collect();
        self
    }

    /// Set `serves_mempool`. [`None`] is the literal `null` member: peers
    /// **unknown** — "unknown" is not "does not serve".
    ///
    /// ```
    /// # use hl_peers::PeerQuery;
    /// let q = PeerQuery::new().serves_mempool([Some(true), None]);
    /// assert_eq!(q.to_query_string().unwrap(), "serves_mempool=true%2Cnull");
    /// ```
    pub fn serves_mempool<I: IntoIterator<Item = Option<bool>>>(mut self, v: I) -> Self {
        self.serves_mempool = v.into_iter().collect();
        self
    }

    /// Set `auction`. [`None`] is the literal `null` member: never seen in the
    /// auction.
    ///
    /// ```
    /// # use hl_peers::{AuctionStatus, PeerQuery};
    /// let q = PeerQuery::new().auction([Some(AuctionStatus::HoldsSlot), Some(AuctionStatus::Bidding)]);
    /// assert_eq!(q.to_query_string().unwrap(), "auction=holds_slot%2Cbidding");
    /// ```
    pub fn auction<I: IntoIterator<Item = Option<AuctionStatus>>>(mut self, v: I) -> Self {
        self.auction = v.into_iter().collect();
        self
    }

    /// Add a parameter this build does not model, sent verbatim and sorted
    /// with the rest (§2.3).
    ///
    /// For a filter the server added within v1 before this crate knows it, or
    /// to observe how the server answers a retired one (the old `mempool=`
    /// answers `400 filter-removed`, a [`Error::BadRequest`]). A name this
    /// build *does* model is refused by [`PeerQuery::validate`] — use its
    /// typed setter, which carries the §2.2 checks.
    pub fn extra_param(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.extra.push((name.into(), value.into()));
        self
    }

    /// Set `vantage`.
    pub fn vantage(mut self, v: impl Into<String>) -> Self {
        self.vantage = Some(v.into());
        self
    }

    /// Set `seen_within`, e.g. `1h`. A value shorter than the data's refresh interval selects on refresh timing rather than peer health.
    pub fn seen_within(mut self, v: impl Into<String>) -> Self {
        self.seen_within = Some(v.into());
        self
    }

    /// Set `cursor`.
    pub fn cursor(mut self, v: impl Into<String>) -> Self {
        self.cursor = Some(v.into());
        self
    }

    /// Replace `cursor`, leaving everything else byte-identical. This is what
    /// pagination does between pages.
    pub fn with_cursor(&self, cursor: Option<String>) -> Self {
        let mut q = self.clone();
        q.cursor = cursor;
        q
    }

    /// Fill `network` from a fallback (the client default) when this query
    /// sets none; a query's own value wins.
    pub(crate) fn with_default_network(
        &self,
        fallback: Option<Network>,
    ) -> std::borrow::Cow<'_, Self> {
        match (self.network, fallback) {
            (None, Some(n)) => {
                let mut q = self.clone();
                q.network = Some(n);
                std::borrow::Cow::Owned(q)
            }
            _ => std::borrow::Cow::Borrowed(self),
        }
    }

    // ------------------------------------------------------------- 2.4 ----

    /// Preset: `tier: [1]`.
    ///
    /// `tier` is **exact**. `max_tier=1` would also return tier 0.
    pub fn tier1() -> Self {
        PeerQuery::new().tier([TierFilter::Exact(1)])
    }

    /// Preset: `label: ["sentry"]`.
    ///
    /// This uses `label=sentry`, **not** `tier=0`. They are equivalent today
    /// because they are different questions and may diverge.
    pub fn sentries() -> Self {
        PeerQuery::new().label(["sentry"])
    }

    /// Preset: `accepting: true` plus a geo argument — "not full".
    ///
    /// 🪤 **This is an estimate of whether the peer has room for another inbound peer, not a guarantee; it can be wrong in both directions.**
    ///
    /// 🪤 The geo argument is tagged. `region` is published **only for
    /// cloud-hosted peers** (`provider:region`, for cloud-hosted peers) and answers `400 filter-unavailable` while no geo data is loaded;
    /// `cc` is the broad geo key. That error is never swallowed into an empty
    /// list.
    pub fn not_full(geo: Geo) -> Self {
        PeerQuery::new().accepting(true).with_geo(geo)
    }

    /// Preset: `serves_mempool: [true]` — peers observed to serve the mempool.
    ///
    /// Owner query 1, "serves mempool AND in country X", is
    /// `PeerQuery::mempool_servers().cc(["DE"])`.
    ///
    /// 🪤 Peers with `serves: null` — **unknown** — are **excluded**.
    /// "unknown" is not "does not serve": to include the unknowns, ask for
    /// `serves_mempool([Some(true), None])`.
    ///
    /// ```
    /// # use hl_peers::PeerQuery;
    /// let q = PeerQuery::mempool_servers().cc(["DE"]);
    /// assert_eq!(q.to_query_string().unwrap(), "cc=DE&serves_mempool=true");
    /// ```
    pub fn mempool_servers() -> Self {
        PeerQuery::new().serves_mempool([Some(true)])
    }

    /// Apply a tagged geo argument.
    pub fn with_geo(mut self, geo: Geo) -> Self {
        match geo {
            Geo::Cc(v) => self.cc = v,
            Geo::Region(v) => self.region = v,
        }
        self
    }

    // ------------------------------------------------------- 2.2 / 2.3 ----

    /// The deliberately thin client-side validation of §2.2.
    ///
    /// Only five things are refused locally. Everything else is sent, so that
    /// a server `400` naming the problem reaches the caller instead of a
    /// client-side guess. The fifth is a value outside a **closed** enum:
    /// `chain`, `network`, `sort` and `format` are unrepresentable here by construction;
    /// `connectable`, `state` and `auction` refuse an `Other` member, because
    /// those types are open for *decoding* a peer but closed as a filter.
    pub fn validate(&self) -> Result<()> {
        if let Some(sw) = &self.seen_within {
            let ok = !sw.is_empty()
                && sw[..sw.len() - 1].chars().all(|c| c.is_ascii_digit())
                && sw.len() >= 2
                && matches!(sw.as_bytes()[sw.len() - 1], b'm' | b'h' | b'd');
            if !ok {
                return Err(Error::invalid(
                    "seen_within",
                    format!("{sw} does not match ^[0-9]+[mhd]$"),
                ));
            }
        }
        if let Some(u) = self.min_uptime {
            if !(0.0..=1.0).contains(&u) {
                return Err(Error::invalid(
                    "min_uptime",
                    format!("{u} is outside [0, 1]"),
                ));
            }
        }
        if self.max_tier.is_some_and(|v| v < 0) {
            return Err(Error::invalid("max_tier", "must be >= 0"));
        }
        if self.max_blocks_behind.is_some_and(|v| v < 0) {
            return Err(Error::invalid("max_blocks_behind", "must be >= 0"));
        }
        if self.max_latency_ms.is_some_and(|v| v < 0) {
            return Err(Error::invalid("max_latency_ms", "must be >= 0"));
        }
        if self.limit.is_some_and(|v| v < 1) {
            return Err(Error::invalid("limit", "must be >= 1"));
        }
        if self.max_latency_ms.is_some() && self.vantage.is_none() {
            return Err(Error::invalid(
                "vantage",
                "vantage is required when max_latency_ms is set",
            ));
        }
        if let Some(bad) = self
            .connectable
            .iter()
            .find(|c| matches!(c, Connectable::Other(_)))
        {
            return Err(Error::invalid(
                "connectable",
                format!("{bad} is not one of yes, no, unknown (case matters)"),
            ));
        }
        if let Some(bad) = self.state.iter().find(|c| matches!(c, PeerState::Other(_))) {
            return Err(Error::invalid(
                "state",
                format!(
                    "{bad} is not one of responsive, open, full, syncing, listening, unreachable, unknown (case matters)"
                ),
            ));
        }
        if let Some(bad) = self
            .auction
            .iter()
            .flatten()
            .find(|c| matches!(c, AuctionStatus::Other(_)))
        {
            return Err(Error::invalid(
                "auction",
                format!("{bad} is not one of holds_slot, bidding, bid_before, null (case matters)"),
            ));
        }
        if let Some((name, _)) = self
            .extra
            .iter()
            .find(|(n, _)| KNOWN_PARAMS.contains(&n.as_str()))
        {
            return Err(Error::invalid(
                name.clone(),
                format!("{name} is a modelled parameter; use its typed setter, not extra_param"),
            ));
        }
        Ok(())
    }

    /// The wire pairs, **sorted lexicographically by parameter name** (§2.3),
    /// so two clients producing the same query produce the same URL and the
    /// same CDN cache key. Unset values and empty lists are omitted entirely.
    pub fn to_pairs(&self) -> Result<Vec<(String, String)>> {
        self.validate()?;
        let mut p: Vec<(String, String)> = Vec::new();
        let mut put = |k: &str, v: String| p.push((k.to_string(), v));

        if let Some(c) = self.chain {
            put("chain", c.as_str().to_string());
        }
        if let Some(n) = self.network {
            put("network", n.as_str().to_string());
        }
        if !self.cc.is_empty() {
            put("cc", self.cc.join(",").to_uppercase());
        }
        if !self.region.is_empty() {
            put("region", self.region.join(","));
        }
        if !self.asn.is_empty() {
            put("asn", self.asn.join(","));
        }
        if !self.tier.is_empty() {
            put(
                "tier",
                self.tier
                    .iter()
                    .map(TierFilter::as_wire)
                    .collect::<Vec<_>>()
                    .join(","),
            );
        }
        if let Some(v) = self.max_tier {
            put("max_tier", v.to_string());
        }
        if !self.role.is_empty() {
            put("role", self.role.join(","));
        }
        if !self.label.is_empty() {
            put("label", self.label.join(","));
        }
        if !self.connectable.is_empty() {
            put("connectable", join_wire(&self.connectable, |x| x.as_str()));
        }
        if let Some(v) = self.accepting {
            put("accepting", v.to_string());
        }
        if !self.state.is_empty() {
            put("state", join_wire(&self.state, |x| x.as_str()));
        }
        if !self.country.is_empty() {
            put("country", self.country.join(",").to_uppercase());
        }
        if !self.serves_mempool.is_empty() {
            put(
                "serves_mempool",
                self.serves_mempool
                    .iter()
                    .map(|m| match m {
                        Some(true) => "true",
                        Some(false) => "false",
                        None => "null",
                    })
                    .collect::<Vec<_>>()
                    .join(","),
            );
        }
        if !self.auction.is_empty() {
            put(
                "auction",
                self.auction
                    .iter()
                    .map(|m| m.as_ref().map_or("null", AuctionStatus::as_str))
                    .collect::<Vec<_>>()
                    .join(","),
            );
        }
        if let Some(v) = self.try_new_peers {
            put("try_new_peers", v.to_string());
        }
        if let Some(v) = self.max_blocks_behind {
            put("max_blocks_behind", v.to_string());
        }
        if let Some(v) = self.min_uptime {
            put("min_uptime", format_number(v));
        }
        if let Some(v) = self.max_latency_ms {
            put("max_latency_ms", v.to_string());
        }
        if let Some(v) = &self.vantage {
            put("vantage", v.clone());
        }
        if let Some(v) = &self.seen_within {
            put("seen_within", v.clone());
        }
        if !self.version.is_empty() {
            put("version", self.version.join(","));
        }
        if let Some(v) = self.sort {
            put("sort", v.as_str().to_string());
        }
        if let Some(v) = self.limit {
            put("limit", v.to_string());
        }
        if let Some(v) = &self.cursor {
            put("cursor", v.clone());
        }
        if !self.fields.is_empty() {
            put("fields", self.fields.join(","));
        }
        if let Some(v) = self.format {
            put("format", v.as_str().to_string());
        }
        for (k, v) in &self.extra {
            put(k, v.clone());
        }

        // Stable: two `extra_param`s of one name keep the caller's order.
        p.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(p)
    }

    /// The canonical query string: pairs in lexicographic order, values
    /// percent-encoded per RFC 3986. The CSV `,` **is** encoded (`%2C`) — no
    /// special case.
    pub fn to_query_string(&self) -> Result<String> {
        Ok(self
            .to_pairs()?
            .into_iter()
            .map(|(k, v)| format!("{}={}", percent_encode(&k), percent_encode(&v)))
            .collect::<Vec<_>>()
            .join("&"))
    }

    /// §4.4: the facets endpoint takes every **filter** of §2.1 (the counts
    /// are over the filtered set), but not `sort`, `limit`, `cursor`,
    /// `fields`, `format` or `try_new_peers` — it would ignore them and the
    /// caller would believe they applied. The first offender, in that order,
    /// is the `Invalid` parameter.
    pub(crate) fn validate_for_facets(&self) -> Result<()> {
        let offenders: Vec<&str> = [
            ("sort", self.sort.is_some()),
            ("limit", self.limit.is_some()),
            ("cursor", self.cursor.is_some()),
            ("fields", !self.fields.is_empty()),
            ("format", self.format.is_some()),
            ("try_new_peers", self.try_new_peers.is_some()),
        ]
        .into_iter()
        .filter_map(|(name, set)| set.then_some(name))
        .collect();
        if offenders.is_empty() {
            Ok(())
        } else {
            Err(Error::invalid(
                offenders[0],
                format!(
                    "the facets endpoint takes filters only; it would ignore {} and you would believe it applied",
                    offenders.join(", ")
                ),
            ))
        }
    }
}

fn join_wire<T>(items: &[T], f: impl Fn(&T) -> &str) -> String {
    items.iter().map(&f).collect::<Vec<_>>().join(",")
}

fn format_number(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{}", v as i64)
    } else {
        format!("{v}")
    }
}

/// RFC 3986 percent-encoding over the unreserved set.
pub(crate) fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(*b as char)
            }
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parameters_are_lexicographic_and_commas_are_encoded() {
        let q = PeerQuery::new()
            .cc(["de", "nl"])
            .tier([TierFilter::Exact(1)]);
        assert_eq!(q.to_query_string().unwrap(), "cc=DE%2CNL&tier=1");
    }

    #[test]
    fn null_is_a_selectable_tier_member() {
        let q = PeerQuery::new().tier([TierFilter::Exact(2), TierFilter::Unplaced]);
        assert_eq!(q.to_query_string().unwrap(), "tier=2%2Cnull");
    }

    #[test]
    fn empty_lists_and_unset_values_are_omitted() {
        assert_eq!(
            PeerQuery::new()
                .cc(Vec::<String>::new())
                .to_query_string()
                .unwrap(),
            ""
        );
    }

    #[test]
    fn the_five_local_refusals() {
        assert!(PeerQuery::new().seen_within("1w").validate().is_err());
        assert!(PeerQuery::new().min_uptime(1.5).validate().is_err());
        assert!(PeerQuery::new().limit(0).validate().is_err());
        assert!(PeerQuery::new().max_tier(-1).validate().is_err());
        assert!(PeerQuery::new().max_latency_ms(80).validate().is_err());
        assert!(PeerQuery::new()
            .max_latency_ms(80)
            .vantage("eu")
            .validate()
            .is_ok());
    }

    #[test]
    fn the_1_0_filters_serialize() {
        let q = PeerQuery::new()
            .country(["jp", "de"])
            .serves_mempool([Some(true), Some(false), None])
            .auction([Some(AuctionStatus::BidBefore), None])
            .try_new_peers(false)
            .state([PeerState::Responsive, PeerState::Open]);
        assert_eq!(
            q.to_query_string().unwrap(),
            "auction=bid_before%2Cnull&country=JP%2CDE&serves_mempool=true%2Cfalse%2Cnull&state=responsive%2Copen&try_new_peers=false"
        );
    }

    #[test]
    fn closed_enums_refuse_an_unknown_member() {
        let mut q = PeerQuery::new();
        q.state.push(PeerState::Other("FULL".into()));
        assert_eq!(q.validate().unwrap_err().parameter(), Some("state"));
        let q = PeerQuery::new().connectable([Connectable::from_wire("bogus")]);
        assert_eq!(q.validate().unwrap_err().parameter(), Some("connectable"));
        let q = PeerQuery::new().auction([Some(AuctionStatus::from_wire("HOLDS_SLOT"))]);
        assert_eq!(q.validate().unwrap_err().parameter(), Some("auction"));
    }

    #[test]
    fn extra_params_are_sorted_in_and_cannot_shadow_a_modelled_one() {
        let q = PeerQuery::new()
            .cc(["DE"])
            .extra_param("mempool", "serving");
        assert_eq!(q.to_query_string().unwrap(), "cc=DE&mempool=serving");
        let q = PeerQuery::new().extra_param("cc", "DE");
        assert_eq!(q.validate().unwrap_err().parameter(), Some("cc"));
    }

    #[test]
    fn facets_take_every_filter_but_not_the_page_controls() {
        let q = PeerQuery::mempool_servers()
            .label(["sentry"])
            .auction([None])
            .country(["DE"]);
        assert!(q.validate_for_facets().is_ok());
        for (q, p) in [
            (PeerQuery::new().sort(Sort::Tier), "sort"),
            (PeerQuery::new().limit(5), "limit"),
            (PeerQuery::new().cursor("c"), "cursor"),
            (PeerQuery::new().fields(["id"]), "fields"),
            (PeerQuery::new().format(Format::Json), "format"),
            (PeerQuery::new().try_new_peers(true), "try_new_peers"),
        ] {
            assert_eq!(q.validate_for_facets().unwrap_err().parameter(), Some(p));
        }
    }

    #[test]
    fn network_is_sent_only_when_set() {
        assert_eq!(PeerQuery::new().to_query_string().unwrap(), "");
        assert_eq!(
            PeerQuery::new()
                .network(Network::Testnet)
                .to_query_string()
                .unwrap(),
            "network=testnet"
        );
        let q = PeerQuery::new().network(Network::Mainnet);
        assert_eq!(
            q.with_default_network(Some(Network::Testnet)).network,
            Some(Network::Mainnet)
        );
        let q = PeerQuery::new();
        assert_eq!(
            q.with_default_network(Some(Network::Testnet)).network,
            Some(Network::Testnet)
        );
        assert_eq!(q.with_default_network(None).network, None);
        let q = PeerQuery::new().extra_param("network", "testnet");
        assert_eq!(q.validate().unwrap_err().parameter(), Some("network"));
    }

    #[test]
    fn no_sort_is_sent_unless_asked_for() {
        assert!(!PeerQuery::tier1()
            .to_query_string()
            .unwrap()
            .contains("sort"));
    }
}

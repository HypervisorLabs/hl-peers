//! Response types — `CLIENT_SPEC.md` §3.
//!
//! The honesty rules are load-bearing here and are enforced by the types
//! themselves: `tier: null` is [`None`] and never `0`; the three-state enums
//! keep `unknown` distinct from `no`/`false`; `mempool.serves: null` (unknown) is [`None`] and never `false`; open enums decode an unrecognised
//! member as-is rather than dropping it or failing.

use std::ops::Deref;

use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value};

use crate::error::RateLimit;

/// The chain a request or a response is about.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[non_exhaustive]
pub enum Chain {
    /// Mainnet. The only chain served today.
    Mainnet,
    /// Testnet. Answered with `400 chain-not-served` today.
    Testnet,
}

impl Chain {
    /// The wire spelling. Case matters.
    pub fn as_str(&self) -> &'static str {
        match self {
            Chain::Mainnet => "Mainnet",
            Chain::Testnet => "Testnet",
        }
    }

    /// Parse the wire spelling.
    pub fn parse(s: &str) -> crate::Result<Chain> {
        match s {
            "Mainnet" => Ok(Chain::Mainnet),
            "Testnet" => Ok(Chain::Testnet),
            other => Err(crate::Error::invalid(
                "chain",
                format!("{other} is not one of Mainnet, Testnet (case matters)"),
            )),
        }
    }
}

impl std::fmt::Display for Chain {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Which network's peer graph to read (§2.1, §10 D-U). **Testnet (beta).**
///
/// The wire form is lower-case, `mainnet` | `testnet`, and parsing is
/// **case-sensitive**: `Testnet` is the capitalised [`Chain`] spelling and is
/// refused here, so the two questions are never blurred. [`Network`] picks
/// which graph is read; [`Chain`] filters on (and asserts) what the server
/// states — a testnet body states `chain: Testnet`.
///
/// Unset (the default everywhere) sends no `network` key at all, so the
/// server's default — mainnet — governs and a mainnet URL is byte-identical
/// to a 1.1 request.
///
/// ```
/// # use hl_peers::Network;
/// assert_eq!("testnet".parse::<Network>().unwrap(), Network::Testnet);
/// assert_eq!(
///     "Testnet".parse::<Network>().unwrap_err().parameter(),
///     Some("network")
/// );
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
#[non_exhaustive]
pub enum Network {
    /// Mainnet — what the server answers when no `network` is sent.
    Mainnet,
    /// Testnet (beta): a small graph, no session data, approximate
    /// `blocks_behind`, and frequent resets (`available: false` for a while
    /// is normal).
    Testnet,
}

impl Network {
    /// The wire spelling. Lower-case.
    pub fn as_str(&self) -> &'static str {
        match self {
            Network::Mainnet => "mainnet",
            Network::Testnet => "testnet",
        }
    }

    /// Parse the wire spelling. Case matters: an unknown value (including
    /// `Testnet`) is [`Error::Invalid`](crate::Error::Invalid) with
    /// parameter `network`.
    pub fn parse(s: &str) -> crate::Result<Network> {
        match s {
            "mainnet" => Ok(Network::Mainnet),
            "testnet" => Ok(Network::Testnet),
            other => Err(crate::Error::invalid(
                "network",
                format!("{other} is not one of mainnet, testnet (case matters)"),
            )),
        }
    }
}

impl std::str::FromStr for Network {
    type Err = crate::Error;

    fn from_str(s: &str) -> crate::Result<Network> {
        Network::parse(s)
    }
}

impl std::fmt::Display for Network {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Build a closed-on-the-wire, open-on-decode enum with a string wire form.
macro_rules! tri_state {
    ($(#[$meta:meta])* $name:ident { $($(#[$vmeta:meta])* $variant:ident => $wire:literal),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        #[non_exhaustive]
        pub enum $name {
            $($(#[$vmeta])* $variant,)+
            /// A member this version of the crate does not know. It is **not**
            /// `unknown`: it is a value the server added after this build.
            Other(String),
        }

        impl $name {
            /// The wire spelling.
            pub fn as_str(&self) -> &str {
                match self {
                    $($name::$variant => $wire,)+
                    $name::Other(s) => s.as_str(),
                }
            }

            /// Decode from the wire spelling. Never fails: an unrecognised
            /// member becomes `Other`, because these sets may grow additively.
            pub fn from_wire(s: &str) -> Self {
                match s {
                    $($wire => $name::$variant,)+
                    other => $name::Other(other.to_string()),
                }
            }
        }

        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str(self.as_str())
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                Ok($name::from_wire(&String::deserialize(d)?))
            }
        }

        impl Serialize for $name {
            fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
                s.serialize_str(self.as_str())
            }
        }
    };
}

tri_state! {
    /// 🪤 An **estimate**, never a guarantee.
    ///
    /// [`Connectable::No`] means "probably no room for another inbound peer".
    /// It can be wrong in both directions. The wire values are the strings `"yes"`, `"no"`, `"unknown"`,
    /// never booleans.
    Connectable {
        /// Probably has room for another inbound peer.
        Yes => "yes",
        /// Probably has no room for another inbound peer.
        No => "no",
        /// Never observed.
        Unknown => "unknown",
    }
}

tri_state! {
    /// Peer state. An **open** enum: an unrecognised member decodes as
    /// [`PeerState::Other`] and renders plain.
    ///
    /// 🪤 `open` / `full` are observations, not a promise. For the estimate
    /// "has room for another inbound peer" use [`Connectable`].
    PeerState {
        /// Answered our scan.
        Responsive => "responsive",
        /// Accepting new inbound peers.
        Open => "open",
        /// Not accepting new inbound peers right now.
        Full => "full",
        /// Catching up.
        Syncing => "syncing",
        /// Accepts inbound connections; other details unknown, so the tier is
        /// typically [`None`].
        Listening => "listening",
        /// Not reachable.
        Unreachable => "unreachable",
        /// Never observed.
        Unknown => "unknown",
    }
}

tri_state! {
    /// Standing in the on-chain gossip read-priority auction (§3.2, §2.1).
    ///
    /// On a [`Peer`] this is an **open** enum: an unrecognised member decodes as
    /// [`AuctionStatus::Other`]. As a query filter it is **closed** —
    /// [`PeerQuery::validate`](crate::PeerQuery::validate) refuses `Other`.
    /// "Never seen in the auction" is not a member: it is [`None`].
    AuctionStatus {
        /// Holds a read-priority slot now.
        HoldsSlot => "holds_slot",
        /// Has an open bid.
        Bidding => "bidding",
        /// Bid at some point, holds nothing now.
        BidBefore => "bid_before",
    }
}

impl Connectable {
    /// `true` only for the positive confirmation [`Connectable::Yes`].
    /// `unknown` is excluded.
    pub fn is_yes(&self) -> bool {
        matches!(self, Connectable::Yes)
    }
}

/// The standing fields on every successful response (§3.1).
///
/// 🔴 Check [`Envelope::available`] **before** [`Envelope::stale`]: a dark
/// subsystem reports both as false.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct Envelope {
    /// Contract version, `v1`.
    pub version: String,
    /// `false` = the producer could not build a graph. The empty payload that
    /// comes with it is **not** a finding about the network.
    pub available: bool,
    /// The served snapshot is older than the staleness bound.
    pub stale: bool,
    /// The chain this answer is about. A `network=testnet` body states
    /// `Testnet`.
    pub chain: String,
    /// The network this answer is about: `Some("testnet")` on a
    /// `network=testnet` body (beta), [`None`] when the key is absent — which
    /// is every mainnet body. Decoded as an open string.
    #[serde(default)]
    pub network: Option<String>,
    /// When the data was generated.
    #[serde(default)]
    pub generated_at: Option<String>,
    /// When the data is next refreshed — the data's own clock, which the polling
    /// helper prefers over a caller's interval.
    #[serde(default)]
    pub next_sweep_at: Option<String>,
    /// The geo-data attribution the server asks to be shown with the data
    /// (e.g. the IPinfo CC BY-SA 4.0 notice). Display it wherever `cc`/`asn`/`org`
    /// are shown.
    #[serde(default)]
    pub attribution: Option<String>,
}

/// Decode `null` (as well as an absent key, via `#[serde(default)]`) to the
/// type's default. Used for the `mempool`/`auction` objects, whose all-null
/// value is the documented "unknown".
fn null_as_default<'de, D, T>(d: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(d)?.unwrap_or_default())
}

/// Does this peer serve the mempool? (§3.2)
///
/// Every published peer carries the object; an all-[`None`] value means
/// **unknown**. 🔴 `serves: None` is *unknown* — it is not `false`, and a
/// caller that treats it as "does not serve" is reading a finding into an
/// absence. A peer from an older server that omits the key decodes to the
/// all-`None` value. Unknown keys inside the object are ignored.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct PeerMempool {
    /// `true` = serves, `false` = does not, [`None`] = unknown.
    pub serves: Option<bool>,
    /// When the verdict was last refreshed (ISO-8601, as sent).
    pub last_checked: Option<String>,
    /// Short machine verdict, `^[a-z0-9_-]{1,32}$`. Open set.
    pub verdict: Option<String>,
    /// Milliseconds to the first mempool message, when measured.
    pub first_frame_ms: Option<i64>,
    /// `probe` (observed by our network probing), or `None` when there is
    /// no verdict. Open set.
    pub source: Option<String>,
}

/// Standing in the on-chain gossip read-priority auction (§3.2).
///
/// Every published peer carries the object; an all-[`None`] value means
/// **never seen in the auction**. `slot` / `since` are set only for
/// [`AuctionStatus::HoldsSlot`]. Unknown keys inside the object are ignored.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct PeerAuction {
    /// Open enum; [`None`] = never seen in the auction.
    pub status: Option<AuctionStatus>,
    /// The slot held, for `holds_slot`.
    pub slot: Option<i64>,
    /// Since when the slot has been held (ISO-8601, as sent).
    pub since: Option<String>,
    /// The most recent bid (ISO-8601, as sent).
    pub last_bid_at: Option<String>,
}

/// One peer as published (§3.2).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct Peer {
    /// The peer's IPv4. Stable identity; the graph is keyed on it.
    pub id: String,
    /// Distance from the network's sentry nodes in the peer graph.
    ///
    /// 🔴 [`None`] means **this node was never placed in the peer graph**. It is *not* tier
    /// 0, not `-1`, and not a sentinel.
    #[serde(default)]
    pub tier: Option<i64>,
    /// Sync state (open enum).
    #[serde(default)]
    pub state: Option<PeerState>,
    /// Third-party role hint. Free-form, open set; never truth.
    #[serde(default)]
    pub role: Option<String>,
    /// ISO-3166-1 alpha-2 country code, from the geo data.
    /// Third-party geo; advisory.
    #[serde(default)]
    pub cc: Option<String>,
    /// How much to trust [`Peer::cc`]: `high` | `medium` | `low` (open set).
    #[serde(default)]
    pub cc_confidence: Option<String>,
    /// Where [`Peer::cc`] came from: `db` (an IP geolocation database) |
    /// `cloud-range` (the provider's published IP ranges) (open set).
    #[serde(default)]
    pub cc_source: Option<String>,
    /// Cloud provider region (`provider:region`). 🪤 Set **only for cloud-hosted
    /// peers** — documented, not asserted.
    #[serde(default)]
    pub region: Option<String>,
    /// `AS…`, from the geo data.
    #[serde(default)]
    pub asn: Option<String>,
    /// The organisation owning [`Peer::asn`].
    #[serde(default)]
    pub org: Option<String>,
    /// The id of this peer's city cluster (`<CC>-<city>`, e.g. `JP-tokyo`)
    /// when the producer serves city positions; [`None`] otherwise.
    /// Never a coordinate — the cluster's point is on
    /// [`NetworkMapGraph::cities`](crate::NetworkMapGraph::cities).
    #[serde(default)]
    pub city: Option<String>,
    /// Hosting or operator label. Not on-chain-verified.
    #[serde(default)]
    pub operator: Option<String>,
    /// Public display name. `null` means nothing public names this peer and a
    /// UI should render the bare IP.
    #[serde(default)]
    pub name: Option<String>,
    /// In-degree over the published edge set.
    #[serde(default)]
    pub in_deg: Option<i64>,
    /// Out-degree over the published edge set.
    #[serde(default)]
    pub out_deg: Option<i64>,
    /// Peers that feed this one (IPv4 strings), intersected with the published
    /// node set. Possibly empty.
    #[serde(default)]
    pub feeders: Option<Vec<String>>,
    /// Last observed block height.
    #[serde(default)]
    pub latest_height: Option<i64>,
    /// Height delta against the network median tip.
    #[serde(default)]
    pub blocks_behind: Option<i64>,
    /// Observed recently. `null` = no observation, never `false`.
    #[serde(default)]
    pub live: Option<bool>,
    /// Three-state connectability **inference**.
    #[serde(default)]
    pub connectable: Option<Connectable>,
    /// Allowlisted public tags. An unknown label is renderable, never an error.
    #[serde(default)]
    pub labels: Vec<String>,
    /// When this peer last answered our scan.
    #[serde(default)]
    pub last_seen: Option<String>,
    /// Share of our checks the peer answered, `0..=1`; `None` = too few to say.
    #[serde(default)]
    pub uptime: Option<f64>,
    /// The node's version, an opaque string (never a number). On
    /// `network=testnet` (beta) it is the node's reported **hardfork version**
    /// (e.g. `"1550"`), [`None`] when the node did not report one. On mainnet
    /// it is always [`None`] (not published, upstream D-9).
    #[serde(default)]
    pub version: Option<String>,
    /// Mempool service (§3.2). All-[`None`] = unknown; never an error when
    /// the key is missing.
    #[serde(default, deserialize_with = "null_as_default")]
    pub mempool: PeerMempool,
    /// Read-priority auction standing (§3.2). All-[`None`] = never seen.
    #[serde(default, deserialize_with = "null_as_default")]
    pub auction: PeerAuction,
    /// Every key the server adds that this build does not model.
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// A page of peers (§3.1, §4.1).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct PeerList {
    /// The standing envelope fields.
    #[serde(flatten)]
    pub envelope: Envelope,
    /// Peers in **this** page.
    #[serde(default)]
    pub count: Option<i64>,
    /// Peers matching the filter across all pages.
    #[serde(default)]
    pub total: Option<i64>,
    /// Pass as `cursor` for the next page. `null` = last page.
    #[serde(default)]
    pub next_cursor: Option<String>,
    /// The page itself.
    pub peers: Vec<Peer>,
}

/// One peer's public dossier (§4.3).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct PeerEnvelope {
    /// The standing envelope fields.
    #[serde(flatten)]
    pub envelope: Envelope,
    /// The peer.
    pub peer: Peer,
}

/// One facet: the counts for one attribute (§3.4).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct Facet {
    /// 🔴 `false` = the attribute is **not collected**. An empty
    /// [`Facet::values`] under `available: true` means it *is* collected and
    /// nothing currently matches. The two are different answers and this crate
    /// keeps them distinguishable.
    pub available: bool,
    /// The values and their counts.
    pub values: Vec<FacetValue>,
}

/// One facet member.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct FacetValue {
    /// Always a string, including the selectable absent-value member, which is
    /// the literal `"null"`.
    pub value: String,
    /// How many peers carry it.
    pub count: i64,
}

/// The facets response (§3.4).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct Facets {
    /// The standing envelope fields. `generated_at` may be absent when no
    /// data has been generated yet.
    #[serde(flatten)]
    pub envelope: Envelope,
    /// Total peers behind these counts.
    #[serde(default)]
    pub total: Option<i64>,
    /// Attribute name to facet. An **open** map: `tier`, `cc`, `region`,
    /// `asn`, `role`, `label`, `state`, `connectable`, `version`, `uptime`,
    /// `serves_mempool`, `auction` today, and any name the server adds.
    pub facets: std::collections::BTreeMap<String, Facet>,
    /// Geo coverage (fractions per attribute, source, db version).
    /// Nullable and deliberately loosely typed: it is diagnostics, not a
    /// contract a caller should branch on.
    #[serde(default)]
    pub geo_coverage: Option<Value>,
}

impl Facets {
    /// Is this attribute collected at all?
    ///
    /// Use it to pre-check a filter rather than discovering
    /// `filter-unavailable` on a `400`. [`None`] means the server did not
    /// mention the attribute.
    pub fn available(&self, name: &str) -> Option<bool> {
        self.facets.get(name).map(|f| f.available)
    }
}

/// A successful response, with everything the HTTP layer saw (§5.3).
///
/// Nothing about the response is hidden from the caller: the envelope, the
/// rate-limit block, the `ETag`, whether it came from the cache, and the HTTP
/// status are all here.
#[derive(Debug, Clone)]
#[non_exhaustive]
pub struct Response<T> {
    /// The decoded body.
    pub body: T,
    /// Parsed from **every** response, including `304` and every error.
    pub rate_limit: RateLimit,
    /// The strong validator, when the server sent one.
    pub etag: Option<String>,
    /// Was this body replayed from the client's ETag cache after a `304`?
    pub from_cache: bool,
    /// `200` or `304`.
    pub status: u16,
    /// The `API-Version` header (`"1"` today). Informational: a different
    /// value is never an error — a breaking change ships under `/v2` paths.
    pub api_version: Option<String>,
}

impl<T> Deref for Response<T> {
    type Target = T;
    fn deref(&self) -> &T {
        &self.body
    }
}

impl Response<PeerList> {
    /// The envelope of this page.
    pub fn envelope(&self) -> &Envelope {
        &self.body.envelope
    }
}

/// The answer to a **caller-supplied** `If-None-Match` (§4.5).
///
/// When the caller asks the conditional question itself, a `304` is returned as
/// its own outcome rather than as a replayed body.
#[derive(Debug, Clone)]
#[non_exhaustive]
pub enum Conditional<T> {
    /// The validator matched; there is no body.
    NotModified {
        /// The validator the server echoed.
        etag: Option<String>,
        /// The rate-limit block from the `304` itself.
        rate_limit: RateLimit,
        /// The `API-Version` header of the `304`.
        api_version: Option<String>,
    },
    /// The validator did not match; here is the body.
    Modified(Response<T>),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_null_tier_stays_null() {
        let p: Peer = serde_json::from_str(r#"{"id":"192.0.2.10","tier":null}"#).unwrap();
        assert_eq!(p.tier, None);
    }

    #[test]
    fn unknown_is_not_no() {
        let p: Peer =
            serde_json::from_str(r#"{"id":"192.0.2.10","connectable":"unknown"}"#).unwrap();
        let c = p.connectable.unwrap();
        assert_eq!(c, Connectable::Unknown);
        assert_ne!(c, Connectable::No);
    }

    #[test]
    fn a_missing_or_null_mempool_is_unknown_not_an_error() {
        let p: Peer = serde_json::from_str(r#"{"id":"192.0.2.10"}"#).unwrap();
        assert_eq!(p.mempool, PeerMempool::default());
        assert_eq!(p.auction, PeerAuction::default());
        let p: Peer =
            serde_json::from_str(r#"{"id":"192.0.2.10","mempool":null,"auction":null}"#).unwrap();
        assert_eq!(p.mempool.serves, None);
        assert_eq!(p.auction.status, None);
    }

    #[test]
    fn unknown_keys_inside_mempool_are_ignored_not_carried() {
        let p: Peer = serde_json::from_str(
            r#"{"id":"192.0.2.10","mempool":{"serves":false,"stream_quality":"x"}}"#,
        )
        .unwrap();
        assert_eq!(p.mempool.serves, Some(false));
        assert!(!p.extra.contains_key("stream_quality"));
    }

    #[test]
    fn a_dropped_field_lands_in_extra() {
        let p: Peer = serde_json::from_str(r#"{"id":"192.0.2.10","some_new_field":1}"#).unwrap();
        assert!(p.extra.contains_key("some_new_field"));
    }

    #[test]
    fn an_unrecognised_open_enum_member_survives() {
        let p: Peer = serde_json::from_str(r#"{"id":"192.0.2.10","state":"quarantined"}"#).unwrap();
        assert_eq!(p.state.unwrap().as_str(), "quarantined");
    }

    #[test]
    fn unknown_peer_keys_are_kept() {
        let p: Peer = serde_json::from_str(r#"{"id":"192.0.2.10","future_key":{"a":1}}"#).unwrap();
        assert!(p.extra.contains_key("future_key"));
    }
}

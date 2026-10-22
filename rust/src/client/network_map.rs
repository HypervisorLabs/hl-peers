//! The network map — `CLIENT_SPEC.md` §4.8.
//!
//! `GET <base_path>/network-map/graph` and `/network-map/summary`: the whole
//! published graph (nodes, edges, the tier histogram) and its envelope with
//! bucket counts. Same ETag cache, same error mapping, same rate-limit and
//! `api_version` exposure as the `/peers` family.
//!
//! 🔴 `available: false` is returned as-is, never an error (§3.1): an empty
//! node list that comes with it is not a finding about the network.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use super::query::percent_encode;
use super::types::{AuctionStatus, Connectable, Network, Peer};
use crate::error::{Error, Result};

/// Options for [`Client::network_map_graph`](crate::Client::network_map_graph)
/// and [`Client::network_map_summary`](crate::Client::network_map_summary).
///
/// Unset options are omitted; the rest are serialized in lexicographic order
/// and percent-encoded exactly as a [`PeerQuery`](crate::PeerQuery) is (§2.3).
///
/// ```
/// # use hl_peers::NetworkMapOpts;
/// let o = NetworkMapOpts::new().max_tier(2).hide_unreachable(true);
/// assert_eq!(o.to_query_string().unwrap(), "hide_unreachable=true&max_tier=2");
/// ```
#[derive(Debug, Clone, Default, PartialEq)]
#[non_exhaustive]
pub struct NetworkMapOpts {
    /// Tier ceiling, `>= 0`.
    pub max_tier: Option<i64>,
    /// Include nodes never placed in the peer graph (server default `true`).
    pub show_unreached: Option<bool>,
    /// Drop nodes that did not answer (server default `false`).
    pub hide_unreachable: Option<bool>,
    /// Closed enum, as in §2.2: an [`Connectable::Other`] member is refused.
    pub connectable: Vec<Connectable>,
    /// Country code.
    pub cc: Option<String>,
    /// Operator label.
    pub operator: Option<String>,
    /// Public label, e.g. `sentry`.
    pub label: Option<String>,
    /// `serves_mempool`, exactly as
    /// [`PeerQuery::serves_mempool`](crate::PeerQuery::serves_mempool):
    /// [`None`] is the literal `null` member (unknown — not "no").
    pub serves_mempool: Vec<Option<bool>>,
    /// `auction`, exactly as
    /// [`PeerQuery::auction`](crate::PeerQuery::auction): closed as a filter,
    /// [`None`] is the literal `null` member (never seen in the auction).
    pub auction: Vec<Option<AuctionStatus>>,
    /// `network` — which network's graph to draw. **Testnet (beta)**, §10
    /// D-U. [`None`] falls back to the client's
    /// [`ClientBuilder::network`](crate::ClientBuilder::network) default and,
    /// when that is unset too, sends nothing (mainnet).
    pub network: Option<Network>,
}

impl NetworkMapOpts {
    /// Every option unset: the whole graph, as the server defaults it.
    pub fn new() -> Self {
        NetworkMapOpts::default()
    }

    /// Set `max_tier`.
    pub fn max_tier(mut self, v: i64) -> Self {
        self.max_tier = Some(v);
        self
    }

    /// Set `show_unreached`.
    pub fn show_unreached(mut self, v: bool) -> Self {
        self.show_unreached = Some(v);
        self
    }

    /// Set `hide_unreachable`.
    pub fn hide_unreachable(mut self, v: bool) -> Self {
        self.hide_unreachable = Some(v);
        self
    }

    /// Set `connectable`.
    pub fn connectable<I: IntoIterator<Item = Connectable>>(mut self, v: I) -> Self {
        self.connectable = v.into_iter().collect();
        self
    }

    /// Set `cc`.
    pub fn cc(mut self, v: impl Into<String>) -> Self {
        self.cc = Some(v.into());
        self
    }

    /// Set `operator`.
    pub fn operator(mut self, v: impl Into<String>) -> Self {
        self.operator = Some(v.into());
        self
    }

    /// Set `label`.
    pub fn label(mut self, v: impl Into<String>) -> Self {
        self.label = Some(v.into());
        self
    }

    /// Set `serves_mempool`.
    ///
    /// ```
    /// # use hl_peers::NetworkMapOpts;
    /// let o = NetworkMapOpts::new().serves_mempool([Some(true), None]);
    /// assert_eq!(o.to_query_string().unwrap(), "serves_mempool=true%2Cnull");
    /// ```
    pub fn serves_mempool<I: IntoIterator<Item = Option<bool>>>(mut self, v: I) -> Self {
        self.serves_mempool = v.into_iter().collect();
        self
    }

    /// Set `auction`.
    pub fn auction<I: IntoIterator<Item = Option<AuctionStatus>>>(mut self, v: I) -> Self {
        self.auction = v.into_iter().collect();
        self
    }

    /// Set `network`. **Testnet (beta)**.
    ///
    /// ```
    /// # use hl_peers::{Network, NetworkMapOpts};
    /// let o = NetworkMapOpts::new().network(Network::Testnet).max_tier(2);
    /// assert_eq!(o.to_query_string().unwrap(), "max_tier=2&network=testnet");
    /// ```
    pub fn network(mut self, v: Network) -> Self {
        self.network = Some(v);
        self
    }

    /// Fill `network` from the client default when unset; an explicit value
    /// wins.
    pub(crate) fn with_default_network(
        &self,
        fallback: Option<Network>,
    ) -> std::borrow::Cow<'_, Self> {
        match (self.network, fallback) {
            (None, Some(n)) => {
                let mut o = self.clone();
                o.network = Some(n);
                std::borrow::Cow::Owned(o)
            }
            _ => std::borrow::Cow::Borrowed(self),
        }
    }

    /// The thin client-side validation: a negative `max_tier`, and a
    /// `connectable` or `auction` member outside its closed enum.
    pub fn validate(&self) -> Result<()> {
        if self.max_tier.is_some_and(|v| v < 0) {
            return Err(Error::invalid("max_tier", "must be >= 0"));
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
        Ok(())
    }

    /// The canonical query string (§2.3): lexicographic, percent-encoded, the
    /// CSV `,` encoded.
    pub fn to_query_string(&self) -> Result<String> {
        self.validate()?;
        let mut p: Vec<(&str, String)> = Vec::new();
        if !self.auction.is_empty() {
            p.push((
                "auction",
                self.auction
                    .iter()
                    .map(|m| m.as_ref().map_or("null", AuctionStatus::as_str))
                    .collect::<Vec<_>>()
                    .join(","),
            ));
        }
        if !self.serves_mempool.is_empty() {
            p.push((
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
            ));
        }
        if let Some(v) = &self.cc {
            p.push(("cc", v.clone()));
        }
        if !self.connectable.is_empty() {
            p.push((
                "connectable",
                self.connectable
                    .iter()
                    .map(Connectable::as_str)
                    .collect::<Vec<_>>()
                    .join(","),
            ));
        }
        if let Some(v) = self.hide_unreachable {
            p.push(("hide_unreachable", v.to_string()));
        }
        if let Some(v) = &self.label {
            p.push(("label", v.clone()));
        }
        if let Some(v) = self.max_tier {
            p.push(("max_tier", v.to_string()));
        }
        if let Some(v) = self.network {
            p.push(("network", v.as_str().to_string()));
        }
        if let Some(v) = &self.operator {
            p.push(("operator", v.clone()));
        }
        if let Some(v) = self.show_unreached {
            p.push(("show_unreached", v.to_string()));
        }
        p.sort_by(|a, b| a.0.cmp(b.0));
        Ok(p.into_iter()
            .map(|(k, v)| format!("{}={}", percent_encode(k), percent_encode(&v)))
            .collect::<Vec<_>>()
            .join("&"))
    }
}

/// `operator_nodes` — the producer's own nodes.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
#[non_exhaustive]
pub struct OperatorNodes {
    /// [`None`] when the producer publishes its own nodes as ordinary
    /// rows.
    pub count: Option<i64>,
}

/// One bucket of the tier histogram.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct TierCount {
    /// The tier.
    pub tier: i64,
    /// Nodes at that tier.
    pub count: i64,
}

/// One server-side city cluster (§4.8). Present only when the
/// producer serves city positions.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct CityCluster {
    /// Stable cluster id, `<CC>-<city>` — what [`Peer::city`] carries.
    pub id: String,
    /// Display name.
    pub name: String,
    /// ISO-3166-1 alpha-2; always the `cc` of its peers.
    pub cc: String,
    /// The city's own latitude, rounded to 0.1°.
    pub lat: f64,
    /// The city's own longitude, rounded to 0.1°.
    pub lon: f64,
    /// Peers in this cluster (after any filter).
    pub peers: i64,
    /// Peers per published tier; key `"null"` = unreached. An open map.
    #[serde(default)]
    pub tiers: BTreeMap<String, i64>,
}

/// One published edge.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct NetworkMapEdge {
    /// Source node id (a published peer id; not validated as IPv4).
    pub from: String,
    /// Target node id.
    pub to: String,
    /// `tier(to) - tier(from)`; [`None`] if either end is unplaced.
    #[serde(default)]
    pub span: Option<i64>,
}

/// `GET <base_path>/network-map/graph` (§4.8).
///
/// Check [`NetworkMapGraph::available`] before [`NetworkMapGraph::stale`].
/// The counts are [`Option`]: a count the server did not send is not `0`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct NetworkMapGraph {
    /// Contract version, `v1`.
    pub version: String,
    /// `false` = the producer could not build a graph.
    pub available: bool,
    /// The served snapshot is older than the staleness bound.
    pub stale: bool,
    /// `Some("testnet")` on a `network=testnet` body (beta); [`None`] when
    /// absent — every mainnet body. An open string.
    #[serde(default)]
    pub network: Option<String>,
    /// When the data was generated.
    #[serde(default)]
    pub generated_at: Option<String>,
    /// The producer's own nodes.
    #[serde(default)]
    pub operator_nodes: OperatorNodes,
    /// Nodes in the graph.
    #[serde(default)]
    pub node_count: Option<i64>,
    /// Edges in the graph.
    #[serde(default)]
    pub edge_count: Option<i64>,
    /// Distinct tiers.
    #[serde(default)]
    pub tier_count: Option<i64>,
    /// Nodes never placed in the peer graph.
    #[serde(default)]
    pub unreached: Option<i64>,
    /// Did the server cap the node list?
    #[serde(default)]
    pub truncated: Option<bool>,
    /// The tier histogram.
    #[serde(default)]
    pub tiers: Vec<TierCount>,
    /// The nodes, decoded with the same [`Peer`] decoder as `/peers`.
    #[serde(default)]
    pub nodes: Vec<Peer>,
    /// The edges.
    #[serde(default)]
    pub edges: Vec<NetworkMapEdge>,
    /// City clusters, biggest first. 🔴 [`None`] = the key was
    /// absent (the producer serves no city positions), which is not the
    /// same as an empty list.
    #[serde(default)]
    pub cities: Option<Vec<CityCluster>>,
    /// The credits for every geo source in use; present with
    /// `cities`. Show it wherever the map is shown.
    #[serde(default)]
    pub attribution: Option<String>,
}

/// `GET <base_path>/network-map/summary` (§4.8): the graph envelope without
/// `nodes` / `edges` / `truncated`, plus zero-filled `connectable`,
/// `serves_mempool` and `auction` counts, and (§10 D-S) the same `cities` /
/// `attribution` as the graph.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct NetworkMapSummary {
    /// Contract version, `v1`.
    pub version: String,
    /// `false` = the producer could not build a graph.
    pub available: bool,
    /// The served snapshot is older than the staleness bound.
    pub stale: bool,
    /// `Some("testnet")` on a `network=testnet` body (beta); [`None`] when
    /// absent — every mainnet body. An open string.
    #[serde(default)]
    pub network: Option<String>,
    /// When the data was generated.
    #[serde(default)]
    pub generated_at: Option<String>,
    /// The producer's own nodes.
    #[serde(default)]
    pub operator_nodes: OperatorNodes,
    /// Nodes in the graph.
    #[serde(default)]
    pub node_count: Option<i64>,
    /// Edges in the graph.
    #[serde(default)]
    pub edge_count: Option<i64>,
    /// Distinct tiers.
    #[serde(default)]
    pub tier_count: Option<i64>,
    /// Nodes never placed in the peer graph.
    #[serde(default)]
    pub unreached: Option<i64>,
    /// The tier histogram.
    #[serde(default)]
    pub tiers: Vec<TierCount>,
    /// `yes` / `no` / `unknown` → count. An open map.
    #[serde(default)]
    pub connectable: BTreeMap<String, i64>,
    /// `true` / `false` / `null` → count. An open map; empty when
    /// absent.
    #[serde(default)]
    pub serves_mempool: BTreeMap<String, i64>,
    /// `holds_slot` / `bidding` / `bid_before` / `null` → count. An
    /// open map; empty when absent.
    #[serde(default)]
    pub auction: BTreeMap<String, i64>,
    /// As [`NetworkMapGraph::cities`]: served, though the upstream schema
    /// does not list it on the summary (§10 D-S). [`None`] = absent.
    #[serde(default)]
    pub cities: Option<Vec<CityCluster>>,
    /// As [`NetworkMapGraph::attribution`].
    #[serde(default)]
    pub attribution: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn options_are_lexicographic_and_validated() {
        let o = NetworkMapOpts::new()
            .show_unreached(false)
            .connectable([Connectable::Yes, Connectable::Unknown])
            .cc("DE")
            .label("sentry");
        assert_eq!(
            o.to_query_string().unwrap(),
            "cc=DE&connectable=yes%2Cunknown&label=sentry&show_unreached=false"
        );
        assert_eq!(NetworkMapOpts::new().to_query_string().unwrap(), "");
        let e = NetworkMapOpts::new().max_tier(-1).validate().unwrap_err();
        assert_eq!(e.parameter(), Some("max_tier"));
        let e = NetworkMapOpts::new()
            .connectable([Connectable::Other("maybe".into())])
            .validate()
            .unwrap_err();
        assert_eq!(e.parameter(), Some("connectable"));
        let e = NetworkMapOpts::new()
            .auction([Some(AuctionStatus::from_wire("HOLDS_SLOT"))])
            .validate()
            .unwrap_err();
        assert_eq!(e.parameter(), Some("auction"));
        assert_eq!(
            NetworkMapOpts::new()
                .serves_mempool([Some(true)])
                .auction([Some(AuctionStatus::HoldsSlot), None])
                .cc("DE")
                .to_query_string()
                .unwrap(),
            "auction=holds_slot%2Cnull&cc=DE&serves_mempool=true"
        );
    }
}

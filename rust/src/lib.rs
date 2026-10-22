#![deny(missing_docs)]
#![forbid(unsafe_code)]
//! A client for the Hyperliquid **Peer API**, and an offline editor for
//! hl-node's `override_gossip_config.json`.
//!
//! Two namespaces, always separate:
//!
//! | namespace | nature | needs network? |
//! |---|---|---|
//! | [`Client`] | the HTTP query surface | yes |
//! | [`GossipConfig`] | pure, offline, deterministic file algebra | **never** |
//!
//! [`GossipConfig`] does not depend on the HTTP layer at all — build with
//! `default-features = false` and you get the config editor with no HTTP
//! dependency, for an operator on a box with no API access. Only
//! [`update_gossip_config`] joins the two.
//!
//! # Defaults
//!
//! The client talks to `https://api.hypervisor.gg` ([`DEFAULT_BASE_URL`]) at
//! plain paths (`/peers`, `/peers/{ip}`, `/peers/facets`,
//! `/network-map/graph`, `/network-map/summary`); override with
//! [`ClientBuilder::base_url`] or `HL_PEER_API_BASE_URL`. Anonymous access is
//! a supported tier. 🔴 **Auto-retry is off by default** (`max_retries = 0`):
//! a `429` raises [`Error::RateLimited`] carrying `reset_at`, and nothing
//! sleeps behind your back unless you opt in with
//! [`ClientBuilder::max_retries`]. The ETag cache is on: a `304` is replayed
//! transparently and costs no quota unit.
//!
//! # 🔴 The node reads the config file when it starts
//!
//! Writing `override_gossip_config.json` does **not** steer a running node: the
//! change takes effect at the node's next start. This library will **never**
//! restart, signal, or otherwise disturb your node — that is the operator's
//! decision, at the operator's moment. Restarting a node *to apply a config*
//! while it is behind can make a lagging box worse.
//!
//! The node's file is `$HOME/override_gossip_config.json`.
//!
//! # Offline quickstart
//!
//! ```
//! use hl_peers::{GossipConfig, MergeOpts};
//!
//! let opts = MergeOpts::default().with_chain("Mainnet");
//! let (cfg, warnings) = GossipConfig::from_peers(
//!     ["192.0.2.10", "192.0.2.11", "198.51.100.7"],
//!     &opts,
//! )
//! .unwrap();
//! assert!(warnings.is_empty());
//! print!("{}", cfg.to_json());
//! ```
//!
//! The async quickstart lives on [`Client`], and the README has the full one.

pub mod error;
pub mod gossip;
pub(crate) mod time;

#[cfg(feature = "client")]
pub mod client;
#[cfg(feature = "client")]
mod update;

pub use error::{Error, Problem, RateLimit, Result, Warning, WarningCode};
pub use gossip::{
    Candidate, Clock, FixedClock, GossipConfig, MergeMode, MergeOpts, RootIp, SaveOpts, SaveResult,
    SystemClock,
};

#[cfg(feature = "blocking")]
pub use client::blocking::{BlockingClient, BlockingClientBuilder};
#[cfg(feature = "client")]
pub use client::{
    normalize_base_path, AuctionStatus, BoxFuture, Chain, CityCluster, Client, ClientBuilder,
    Conditional, Connectable, DefaultJitter, Envelope, Facet, FacetValue, Facets, Format, Geo,
    GetPeerOpts, HttpRequest, HttpResponse, IterOpts, Jitter, Network, NetworkMapEdge,
    NetworkMapGraph, NetworkMapOpts, NetworkMapSummary, NoJitter, OnTruncate, OperatorNodes, Peer,
    PeerAuction, PeerEnvelope, PeerList, PeerMempool, PeerQuery, PeerState, PeerStream,
    RecordingSleeper, ReqwestTransport, Response, Sleeper, Sort, TierCount, TierFilter,
    TokioSleeper, Transport, DEFAULT_BASE_PATH, DEFAULT_BASE_URL,
};
#[cfg(feature = "client")]
pub use update::{update_gossip_config, update_gossip_config_from_peers, UpdateOpts, UpdateResult};

/// `PeerQuery` under the shorter name the spec's prose uses.
#[cfg(feature = "client")]
pub use client::PeerQuery as Query;

/// Re-exported so a caller can check an address the same way this crate does.
#[cfg(feature = "client")]
pub(crate) fn gossip_is_ipv4(s: &str) -> bool {
    s.parse::<std::net::Ipv4Addr>().is_ok()
}

/// The README's examples are compiled and run as doctests, so the quickstart
/// cannot rot. They need `blocking` (the README shows the sync facade too), so
/// `cargo test --all-features` runs them.
#[cfg(all(doctest, feature = "blocking"))]
#[doc = include_str!("../README.md")]
mod readme_doctests {}

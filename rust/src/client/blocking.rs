//! A synchronous facade, behind the `blocking` feature.
//!
//! It drives the async [`Client`](crate::Client) on a private current-thread
//! runtime, so there is exactly one implementation of every behaviour and the
//! two surfaces cannot drift.
//!
//! 🪤 Do not call these from inside an async runtime — use the async client
//! there.

use std::sync::Arc;
use std::time::Duration;

use crate::client::{
    Conditional, Facets, IterOpts, Jitter, NetworkMapGraph, NetworkMapOpts, NetworkMapSummary,
    Peer, PeerEnvelope, PeerList, PeerQuery, Response, Sleeper, Transport,
};
use crate::error::{Error, Result};
use crate::GossipConfig;

/// The synchronous client.
#[derive(Debug)]
pub struct BlockingClient {
    inner: crate::Client,
    runtime: tokio::runtime::Runtime,
}

/// Builder for [`BlockingClient`].
#[derive(Debug, Default)]
pub struct BlockingClientBuilder {
    inner: crate::ClientBuilder,
}

impl BlockingClientBuilder {
    /// See [`crate::ClientBuilder::base_url`].
    pub fn base_url(mut self, url: impl Into<String>) -> Self {
        self.inner = self.inner.base_url(url);
        self
    }
    /// See [`crate::ClientBuilder::base_path`].
    pub fn base_path(mut self, path: impl Into<String>) -> Self {
        self.inner = self.inner.base_path(path);
        self
    }
    /// See [`crate::ClientBuilder::api_key`].
    pub fn api_key(mut self, key: impl Into<String>) -> Self {
        self.inner = self.inner.api_key(key);
        self
    }
    /// See [`crate::ClientBuilder::timeout`].
    pub fn timeout(mut self, t: Duration) -> Self {
        self.inner = self.inner.timeout(t);
        self
    }
    /// See [`crate::ClientBuilder::max_retries`].
    pub fn max_retries(mut self, n: u32) -> Self {
        self.inner = self.inner.max_retries(n);
        self
    }
    /// See [`crate::ClientBuilder::user_agent_suffix`].
    pub fn user_agent_suffix(mut self, s: impl Into<String>) -> Self {
        self.inner = self.inner.user_agent_suffix(s);
        self
    }
    /// See [`crate::ClientBuilder::cache_enabled`].
    pub fn cache_enabled(mut self, on: bool) -> Self {
        self.inner = self.inner.cache_enabled(on);
        self
    }
    /// See [`crate::ClientBuilder::transport`].
    pub fn transport(mut self, t: Arc<dyn Transport>) -> Self {
        self.inner = self.inner.transport(t);
        self
    }
    /// See [`crate::ClientBuilder::max_retry_after`].
    pub fn max_retry_after(mut self, d: Duration) -> Self {
        self.inner = self.inner.max_retry_after(d);
        self
    }
    /// See [`crate::ClientBuilder::sleeper`].
    pub fn sleeper(mut self, s: Arc<dyn Sleeper>) -> Self {
        self.inner = self.inner.sleeper(s);
        self
    }
    /// See [`crate::ClientBuilder::jitter`].
    pub fn jitter(mut self, j: Arc<dyn Jitter>) -> Self {
        self.inner = self.inner.jitter(j);
        self
    }
    /// See [`crate::ClientBuilder::clock`].
    pub fn clock(mut self, c: Arc<dyn crate::Clock>) -> Self {
        self.inner = self.inner.clock(c);
        self
    }
    /// Accepted and ignored; see
    /// [`crate::ClientBuilder::suppress_placeholder_warning`].
    pub fn suppress_placeholder_warning(mut self, yes: bool) -> Self {
        self.inner = self.inner.suppress_placeholder_warning(yes);
        self
    }

    /// Build it, along with its private runtime.
    pub fn build(self) -> Result<BlockingClient> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| Error::Transport {
                cause: format!("building the blocking runtime: {e}"),
            })?;
        Ok(BlockingClient {
            inner: self.inner.build()?,
            runtime,
        })
    }
}

impl BlockingClient {
    /// A blocking client with every default.
    pub fn new() -> Result<BlockingClient> {
        BlockingClient::builder().build()
    }

    /// Start configuring one.
    pub fn builder() -> BlockingClientBuilder {
        BlockingClientBuilder::default()
    }

    /// The async client underneath, if you need it.
    pub fn async_client(&self) -> &crate::Client {
        &self.inner
    }

    /// See [`crate::Client::list_peers`].
    pub fn list_peers(&self, query: &PeerQuery) -> Result<Response<PeerList>> {
        self.runtime.block_on(self.inner.list_peers(query))
    }

    /// See [`crate::Client::list_peers_conditional`].
    pub fn list_peers_conditional(
        &self,
        query: &PeerQuery,
        if_none_match: &str,
    ) -> Result<Conditional<PeerList>> {
        self.runtime
            .block_on(self.inner.list_peers_conditional(query, if_none_match))
    }

    /// See [`crate::Client::list_all_peers`].
    pub fn list_all_peers(&self, query: &PeerQuery, opts: IterOpts) -> Result<Vec<Peer>> {
        self.runtime
            .block_on(self.inner.list_all_peers(query, opts))
    }

    /// See [`crate::Client::get_peer`].
    pub fn get_peer(&self, ip: &str) -> Result<Response<PeerEnvelope>> {
        self.runtime.block_on(self.inner.get_peer(ip))
    }

    /// See [`crate::Client::facets`].
    pub fn facets(&self, query: Option<&PeerQuery>) -> Result<Response<Facets>> {
        self.runtime.block_on(self.inner.facets(query))
    }

    /// See [`crate::Client::list_peers_as_gossip_config`].
    pub fn list_peers_as_gossip_config(&self, query: &PeerQuery) -> Result<Response<GossipConfig>> {
        self.runtime
            .block_on(self.inner.list_peers_as_gossip_config(query))
    }

    /// See [`crate::Client::network_map_graph`].
    pub fn network_map_graph(&self, opts: &NetworkMapOpts) -> Result<Response<NetworkMapGraph>> {
        self.runtime.block_on(self.inner.network_map_graph(opts))
    }

    /// See [`crate::Client::network_map_summary`].
    pub fn network_map_summary(
        &self,
        opts: &NetworkMapOpts,
    ) -> Result<Response<NetworkMapSummary>> {
        self.runtime.block_on(self.inner.network_map_summary(opts))
    }

    /// See [`crate::update_gossip_config`].
    pub fn update_gossip_config(
        &self,
        query: &PeerQuery,
        path: Option<&std::path::Path>,
        opts: &crate::UpdateOpts,
    ) -> Result<crate::UpdateResult> {
        self.runtime
            .block_on(crate::update_gossip_config(&self.inner, query, path, opts))
    }
}

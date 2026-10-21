//! `update_gossip_config` — `CLIENT_SPEC.md` §7.
//!
//! The one helper that joins the two namespaces. Exactly one round of work.

use std::path::{Path, PathBuf};

use crate::client::{Client, IterOpts, Peer, PeerQuery};
use crate::error::{Error, Result, Warning};
use crate::gossip::{GossipConfig, MergeOpts, SaveOpts};

/// Options for [`update_gossip_config`].
#[derive(Debug, Clone)]
#[non_exhaustive]
pub struct UpdateOpts {
    /// How the peers are folded into the root list.
    pub merge: MergeOpts,
    /// How the file is written.
    pub save: SaveOpts,
    /// Render only: collect, merge and validate, but write nothing.
    pub no_write: bool,
    /// Candidates fetched per root slot, so that `exclude`/`keep_existing`
    /// /validity filtering still leaves enough. Default **4**.
    pub overfetch: usize,
}

impl Default for UpdateOpts {
    fn default() -> Self {
        UpdateOpts {
            merge: MergeOpts::default(),
            save: SaveOpts::default(),
            no_write: false,
            overfetch: 4,
        }
    }
}

impl UpdateOpts {
    /// Set [`UpdateOpts::merge`].
    pub fn with_merge(mut self, merge: MergeOpts) -> Self {
        self.merge = merge;
        self
    }
    /// Set [`UpdateOpts::save`].
    pub fn with_save(mut self, save: SaveOpts) -> Self {
        self.save = save;
        self
    }
    /// Set [`UpdateOpts::no_write`].
    pub fn with_no_write(mut self, v: bool) -> Self {
        self.no_write = v;
        self
    }
    /// Set [`UpdateOpts::overfetch`].
    pub fn with_overfetch(mut self, n: usize) -> Self {
        self.overfetch = n;
        self
    }
}

/// What [`update_gossip_config`] produced.
#[derive(Debug, Clone)]
#[non_exhaustive]
pub struct UpdateResult {
    /// The resulting config.
    pub config: GossipConfig,
    /// Its rendered bytes. **Always present**, even when nothing was written,
    /// so "generate to stdout and let the operator place it" is a first-class
    /// path.
    pub json: String,
    /// Do the rendered bytes differ from what was already on disk?
    pub changed: bool,
    /// The unified diff, when there was a change and a path was given.
    pub diff: String,
    /// Where it was written, if it was.
    pub written_path: Option<PathBuf>,
    /// Every warning from the merge and from `validate`. Never swallow these.
    pub warnings: Vec<Warning>,
    /// The envelope of the peer query that fed the merge.
    pub envelope: Option<crate::client::Envelope>,
}

/// Query the API and produce (or update) an `override_gossip_config.json` (§7).
///
/// 🔴 If the response carries `available: false` this raises
/// [`Error::Unavailable`] **before touching any file** (D-G). An unbuildable
/// graph is not an empty peer set, and turning it into one would empty an
/// operator's root list — the crash-loop shape.
///
/// 🔴 It never restarts, signals or reloads anything. hl-node reads the file
/// only at node start; the change takes effect on the node's next
/// start, which is yours to schedule.
///
/// The `chain` for a fresh config comes from `opts.merge.chain`, else the
/// response envelope's `chain`, else this raises `Invalid { parameter: "chain" }`.
/// It is **never** defaulted to `Mainnet`: a chain that disagrees with the
/// node's `--chain` stops the node from starting, and guessing is how that happens.
pub async fn update_gossip_config(
    client: &Client,
    query: &PeerQuery,
    path: Option<&Path>,
    opts: &UpdateOpts,
) -> Result<UpdateResult> {
    // (1) collect, with overfetch so filtering still leaves candidates.
    let max_items = opts.merge.max_roots.saturating_mul(opts.overfetch).max(1);
    let mut stream = client.iter_peers(query, IterOpts::default().with_max_items(max_items));
    let mut peers: Vec<Peer> = Vec::new();
    while let Some(item) = stream.next().await {
        peers.push(item?);
    }
    let envelope = stream.envelope().cloned();

    // (2) an unbuildable graph is not an empty peer set.
    if let Some(env) = &envelope {
        if !env.available {
            return Err(Error::Unavailable {
                detail: Some(
                    "the producer could not build a graph (available: false); refusing to build a config from an empty peer set, which would be the crash-loop shape"
                        .into(),
                ),
                problem: None,
                rate_limit: Default::default(),
                api_version: None,
            });
        }
    }

    update_gossip_config_from_peers(&peers, envelope, path, opts)
}

/// The offline half of [`update_gossip_config`]: the same merge, validate and
/// save, over a peer list you already have. No network at all.
pub fn update_gossip_config_from_peers(
    peers: &[Peer],
    envelope: Option<crate::client::Envelope>,
    path: Option<&Path>,
    opts: &UpdateOpts,
) -> Result<UpdateResult> {
    let mut merge = opts.merge.clone();
    if merge.chain.is_none() {
        merge.chain = envelope.as_ref().map(|e| e.chain.clone());
    }

    // (3) load, or start fresh.
    let mut config = match path {
        Some(p) if p.exists() => GossipConfig::load(p)?,
        _ => {
            let chain = merge.chain.as_deref().ok_or_else(|| {
                Error::invalid(
                    "chain",
                    "no chain to write: pass it explicitly. Never default it to Mainnet — a chain that disagrees with the node's --chain stops the node from starting",
                )
            })?;
            GossipConfig::new(chain, Vec::<String>::new())
        }
    };

    // (4)(5) merge, then validate and collect warnings.
    let mut warnings = config.merge_peers(peers.iter(), &merge)?;
    warnings.extend(config.validate()?);

    let json = config.to_json();

    // (6) write, unless asked not to.
    let (changed, diff, written_path) = match (path, opts.no_write) {
        (Some(p), false) => {
            let res = config.save(p, &opts.save)?;
            (res.changed, res.diff, res.written.then(|| res.path.clone()))
        }
        (Some(p), true) => {
            let existing = std::fs::read_to_string(p).unwrap_or_default();
            (existing != json, String::new(), None)
        }
        (None, _) => (true, String::new(), None),
    };

    Ok(UpdateResult {
        config,
        json,
        changed,
        diff,
        written_path,
        warnings,
        envelope,
    })
}

//! `GossipConfig` — a pure, offline, deterministic editor for hl-node's
//! `override_gossip_config.json` (`CLIENT_SPEC.md` §6).
//!
//! No network, no clock (except the one injectable read behind
//! [`SaveOpts::backup`]), no environment lookups, no restart of anything. Given
//! the same input it produces the same bytes on every platform, in every
//! language, forever. This module compiles with `default-features = false`, so
//! an operator on a box with no API access can still use it.
//!
//! # 🔴 The node reads this file when it starts
//!
//! Writing `override_gossip_config.json` does **not** steer a running node: the
//! change takes effect at the node's next start. This library will **never**
//! restart, signal, or otherwise disturb your node — that is the operator's
//! decision, at the operator's moment. Restarting a node *to apply a config*
//! while it is behind can make a lagging box worse.
//!
//! The node's file is `$HOME/override_gossip_config.json`.
//!
//! ```
//! use hl_peers::GossipConfig;
//!
//! let mut cfg = GossipConfig::new("Mainnet", ["192.0.2.10"]);
//! cfg.set_try_new_peers(false);
//! assert_eq!(
//!     cfg.to_json(),
//!     "{\n  \"chain\": \"Mainnet\",\n  \"root_node_ips\": [\n    {\n      \"Ip\": \"192.0.2.10\"\n    }\n  ],\n  \"try_new_peers\": false\n}\n"
//! );
//! ```

mod diff;
mod ip;
mod merge;
mod save;
mod ser;
mod validate;

pub use merge::{Candidate, MergeMode, MergeOpts};
pub use save::{Clock, FixedClock, SaveOpts, SaveResult, SystemClock};

use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::error::{Error, Result, Warning};

/// One entry of `root_node_ips` on the way in.
///
/// A bare IPv4 string and a `{"Ip": …}` object are both accepted as
/// *constructor input* (§6.1); the config always **stores** the object shape. A
/// bare string found inside a *parsed* file is a different thing entirely and
/// [`GossipConfig::validate`] refuses it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RootIp(Value);

impl RootIp {
    /// The stored entry, ready to go into `root_node_ips`.
    pub(crate) fn into_value(self) -> Value {
        self.0
    }

    fn wrapped(ip: &str) -> Value {
        let mut m = Map::new();
        m.insert("Ip".to_string(), Value::String(ip.to_string()));
        Value::Object(m)
    }
}

impl From<&str> for RootIp {
    fn from(s: &str) -> Self {
        RootIp(RootIp::wrapped(s))
    }
}

impl From<String> for RootIp {
    fn from(s: String) -> Self {
        RootIp(RootIp::wrapped(&s))
    }
}

impl From<&String> for RootIp {
    fn from(s: &String) -> Self {
        RootIp(RootIp::wrapped(s))
    }
}

impl From<&Value> for RootIp {
    /// A JSON string is wrapped; a `{"Ip": …}` object is taken as-is; anything
    /// else is stored verbatim so that `validate` — not this conversion — is
    /// what reports it.
    fn from(v: &Value) -> Self {
        match v {
            Value::String(s) => RootIp(RootIp::wrapped(s)),
            other => RootIp(other.clone()),
        }
    }
}

/// hl-node's `override_gossip_config.json`, as a value you can edit.
///
/// Unknown keys are preserved verbatim through parse, mutation and
/// re-serialization, so a future field added by the node does not get dropped
/// by a client that predates it.
#[derive(Debug, Clone, Default)]
pub struct GossipConfig {
    raw: Map<String, Value>,
    /// §6.5: warnings raised by the last merge. They are re-reported by
    /// `validate`/`save` so a caller who ignored the merge's return value
    /// cannot lose them.
    pending: Vec<Warning>,
}

/// Two configs are equal when their **content** is. Pending warnings are
/// bookkeeping about how a config was built, not part of the file.
impl PartialEq for GossipConfig {
    fn eq(&self, other: &Self) -> bool {
        self.raw == other.raw
    }
}

impl Eq for GossipConfig {}

impl GossipConfig {
    // ---------------------------------------------------------------- 6.1 --

    /// Build a config from scratch.
    ///
    /// `root_ips` accepts bare IPv4 strings **or** `{"Ip": …}` entries; the
    /// stored shape is always the object.
    ///
    /// ```
    /// # use hl_peers::GossipConfig;
    /// let cfg = GossipConfig::new("Mainnet", ["192.0.2.10", "192.0.2.11"]);
    /// assert_eq!(cfg.root_ips(), vec!["192.0.2.10", "192.0.2.11"]);
    /// assert_eq!(cfg.chain(), Some("Mainnet"));
    /// ```
    pub fn new<I, R>(chain: &str, root_ips: I) -> Self
    where
        I: IntoIterator<Item = R>,
        R: Into<RootIp>,
    {
        let mut cfg = GossipConfig::default();
        cfg.raw
            .insert("chain".to_string(), Value::String(chain.to_string()));
        let entries: Vec<Value> = root_ips
            .into_iter()
            .map(|r| r.into().into_value())
            .collect();
        cfg.raw
            .insert("root_node_ips".to_string(), Value::Array(entries));
        cfg
    }

    /// Decode JSON text. Unknown keys are preserved; **nothing is validated**.
    ///
    /// Invalid JSON is [`Error::Decode`]; a non-object top level is
    /// `Invalid { parameter: "$" }`.
    ///
    /// ```
    /// # use hl_peers::GossipConfig;
    /// let cfg = GossipConfig::parse(r#"{"root_node_ips": [{"Ip": "192.0.2.10"}]}"#).unwrap();
    /// assert!(GossipConfig::parse("[]").is_err());
    /// assert_eq!(cfg.root_ips(), vec!["192.0.2.10"]);
    /// ```
    pub fn parse(json_text: &str) -> Result<Self> {
        let value: Value = serde_json::from_str(json_text).map_err(|e| Error::Decode {
            cause: e.to_string(),
            body_excerpt: crate::error::body_excerpt(json_text.as_bytes(), 512),
        })?;
        match value {
            Value::Object(raw) => Ok(GossipConfig {
                raw,
                pending: Vec::new(),
            }),
            _ => Err(Error::invalid("$", "expected a JSON object")),
        }
    }

    /// Read and [`parse`](GossipConfig::parse) a file.
    ///
    /// A missing file is [`Error::NotFound`] and **never** an empty config: an
    /// empty config is the crash-loop shape. Use
    /// [`load_or_new`](GossipConfig::load_or_new) to opt into that explicitly.
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        match std::fs::read_to_string(path) {
            Ok(text) => GossipConfig::parse(&text),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(Error::NotFound {
                detail: Some(format!("{} does not exist", path.display())),
                path: Some(path.to_path_buf()),
                problem: None,
                rate_limit: Default::default(),
                api_version: None,
            }),
            Err(e) => Err(Error::Transport {
                cause: format!("reading {}: {e}", path.display()),
            }),
        }
    }

    /// [`load`](GossipConfig::load), or a fresh empty-rooted config when the
    /// file does not exist. The explicit opt-in to the shape `load` refuses.
    pub fn load_or_new(path: impl AsRef<Path>, chain: &str) -> Result<Self> {
        match GossipConfig::load(path) {
            Err(Error::NotFound { .. }) => Ok(GossipConfig::new(chain, Vec::<String>::new())),
            other => other,
        }
    }

    /// Serialize per §6.3. Does **not** validate — [`save`](GossipConfig::save)
    /// validates.
    pub fn to_json(&self) -> String {
        ser::render(&self.raw)
    }

    /// [`validate`](GossipConfig::validate), then [`to_json`](GossipConfig::to_json).
    pub fn to_json_validated(&self) -> Result<(String, Vec<Warning>)> {
        let warnings = self.validate()?;
        Ok((self.to_json(), warnings))
    }

    /// The default location of the node's file: `$HOME/override_gossip_config.json`.
    ///
    /// 🪤 A *default*, never a discovery. This crate will not inspect a running
    /// process, a systemd unit or `/proc` to guess where your node reads from.
    /// The upstream evidence cannot distinguish `$HOME`-relative from
    /// cwd-relative, because on every observed box cwd == `$HOME`;
    /// `$HOME`-relative is the labelled **hypothesis**. Rendering to stdout and
    /// placing the file yourself is the safest option.
    pub fn default_config_path() -> Option<PathBuf> {
        std::env::var_os("HOME").map(|h| PathBuf::from(h).join("override_gossip_config.json"))
    }

    /// The whole config as a raw JSON object, for callers that need to look at
    /// a key this crate does not model.
    pub fn as_map(&self) -> &Map<String, Value> {
        &self.raw
    }

    /// The warnings the last [`merge_peers`](GossipConfig::merge_peers) or
    /// [`from_peers`](GossipConfig::from_peers) raised and that
    /// [`validate`](GossipConfig::validate) will re-report (§6.5).
    ///
    /// They live for the lifetime of the value: the next merge **replaces**
    /// them, and only [`clear_warnings`](GossipConfig::clear_warnings) drops
    /// them. In particular a successful `save` does **not** clear them, so
    /// `save(); validate()` reports the same set twice rather than a shrinking
    /// one.
    pub fn pending_warnings(&self) -> &[Warning] {
        &self.pending
    }

    /// Drop the pending merge warnings. The only thing that does.
    pub fn clear_warnings(&mut self) -> &mut Self {
        self.pending.clear();
        self
    }

    pub(crate) fn set_pending_warnings(&mut self, warnings: Vec<Warning>) {
        self.pending = warnings;
    }

    // ---------------------------------------------------------------- 6.2 --

    /// The root IPs as **bare strings**, in file order. The `{"Ip": …}` wrapper
    /// is an encoding detail and does not leak into this accessor.
    pub fn root_ips(&self) -> Vec<String> {
        self.entries("root_node_ips")
            .iter()
            .filter_map(entry_ip)
            .map(str::to_string)
            .collect()
    }

    /// Is `ip` already a root?
    pub fn has_root_ip(&self, ip: &str) -> bool {
        self.root_ips().iter().any(|r| r == ip)
    }

    /// Append a root. A duplicate is a **no-op**, not an error.
    pub fn add_root_ip(&mut self, ip: &str) -> &mut Self {
        if !self.has_root_ip(ip) {
            let mut list = self.entries("root_node_ips");
            list.push(RootIp::wrapped(ip));
            self.raw
                .insert("root_node_ips".to_string(), Value::Array(list));
        }
        self
    }

    /// Remove a root. An absent IP is a no-op.
    pub fn remove_root_ip(&mut self, ip: &str) -> &mut Self {
        if self.raw.contains_key("root_node_ips") {
            let list: Vec<Value> = self
                .entries("root_node_ips")
                .into_iter()
                .filter(|e| entry_ip(e) != Some(ip))
                .collect();
            self.raw
                .insert("root_node_ips".to_string(), Value::Array(list));
        }
        self
    }

    /// Replace the root list wholesale, preserving the given order and
    /// de-duplicating **first-wins**.
    pub fn set_root_ips<I, S>(&mut self, ips: I) -> &mut Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut seen: Vec<String> = Vec::new();
        for ip in ips {
            let ip = ip.as_ref().to_string();
            if !seen.contains(&ip) {
                seen.push(ip);
            }
        }
        let list: Vec<Value> = seen.iter().map(|s| RootIp::wrapped(s)).collect();
        self.raw
            .insert("root_node_ips".to_string(), Value::Array(list));
        self
    }

    /// The reserved peers, as **bare strings**. 🪤 Two shapes, one file:
    /// `root_node_ips` entries are `{"Ip": …}` objects, `reserved_peer_ips`
    /// entries are bare strings. Round-tripping one shape through the other
    /// silently produces a file the node will not accept.
    pub fn reserved_peer_ips(&self) -> Vec<String> {
        self.entries("reserved_peer_ips")
            .iter()
            .filter_map(|v| v.as_str())
            .map(str::to_string)
            .collect()
    }

    /// Append a reserved peer. A duplicate is a no-op.
    pub fn add_reserved_peer_ip(&mut self, ip: &str) -> &mut Self {
        if !self.reserved_peer_ips().iter().any(|r| r == ip) {
            let mut list = self.entries("reserved_peer_ips");
            list.push(Value::String(ip.to_string()));
            self.raw
                .insert("reserved_peer_ips".to_string(), Value::Array(list));
        }
        self
    }

    /// Remove a reserved peer. An absent IP is a no-op.
    pub fn remove_reserved_peer_ip(&mut self, ip: &str) -> &mut Self {
        if self.raw.contains_key("reserved_peer_ips") {
            let list: Vec<Value> = self
                .entries("reserved_peer_ips")
                .into_iter()
                .filter(|v| v.as_str() != Some(ip))
                .collect();
            self.raw
                .insert("reserved_peer_ips".to_string(), Value::Array(list));
        }
        self
    }

    /// Replace the reserved-peer list wholesale, first-wins de-duplication.
    pub fn set_reserved_peer_ips<I, S>(&mut self, ips: I) -> &mut Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut seen: Vec<String> = Vec::new();
        for ip in ips {
            let ip = ip.as_ref().to_string();
            if !seen.contains(&ip) {
                seen.push(ip);
            }
        }
        let list: Vec<Value> = seen.into_iter().map(Value::String).collect();
        self.raw
            .insert("reserved_peer_ips".to_string(), Value::Array(list));
        self
    }

    /// The `chain`, if the file carries one.
    pub fn chain(&self) -> Option<&str> {
        self.raw.get("chain").and_then(|v| v.as_str())
    }

    /// Set `chain`. The spelling is exact — `Mainnet` or `Testnet`. 🪤 A chain
    /// that disagrees with the node's `--chain` stops the node from starting.
    pub fn set_chain(&mut self, chain: &str) -> &mut Self {
        self.raw
            .insert("chain".to_string(), Value::String(chain.to_string()));
        self
    }

    /// `try_new_peers`, if present.
    pub fn try_new_peers(&self) -> Option<bool> {
        self.raw.get("try_new_peers").and_then(|v| v.as_bool())
    }

    /// Set `try_new_peers`.
    ///
    /// 🪤 Whether `try_new_peers: false` restricts a **multi**-entry root list
    /// to exactly that list is explicitly *not established* (hypothesis). Only
    /// the single-root case is evidenced as pinning; `validate` warns about the
    /// other.
    pub fn set_try_new_peers(&mut self, value: bool) -> &mut Self {
        self.raw
            .insert("try_new_peers".to_string(), Value::Bool(value));
        self
    }

    /// Remove `try_new_peers` — the Rust spelling of §6.2's "set it to null".
    pub fn unset_try_new_peers(&mut self) -> &mut Self {
        self.raw.remove("try_new_peers");
        self
    }

    /// `n_gossip_peers`, if present.
    pub fn n_gossip_peers(&self) -> Option<i64> {
        self.raw.get("n_gossip_peers").and_then(|v| v.as_i64())
    }

    /// Set `n_gossip_peers`.
    ///
    /// D-C: a value outside `[8, 100]` is **refused**, at both ends, rather
    /// than silently clamped. Clamping would write a number the operator did
    /// not choose into a file they will read back later.
    pub fn set_n_gossip_peers(&mut self, value: i64) -> Result<&mut Self> {
        if !(8..=100).contains(&value) {
            return Err(Error::invalid(
                "n_gossip_peers",
                format!("{value} is outside [8, 100]; refusing rather than silently clamping"),
            ));
        }
        self.raw
            .insert("n_gossip_peers".to_string(), Value::from(value));
        Ok(self)
    }

    /// Remove `n_gossip_peers`.
    pub fn unset_n_gossip_peers(&mut self) -> &mut Self {
        self.raw.remove("n_gossip_peers");
        self
    }

    /// `split_client_blocks`, if present.
    pub fn split_client_blocks(&self) -> Option<bool> {
        self.raw
            .get("split_client_blocks")
            .and_then(|v| v.as_bool())
    }

    /// Set `split_client_blocks`.
    ///
    /// `false` **removes the key**, because the node's own serializer omits it
    /// when false. An explicit `false` already present in a parsed file is
    /// preserved by a round trip — only this setter removes it.
    pub fn set_split_client_blocks(&mut self, value: bool) -> &mut Self {
        if value {
            self.raw
                .insert("split_client_blocks".to_string(), Value::Bool(true));
        } else {
            self.raw.remove("split_client_blocks");
        }
        self
    }

    /// Remove `split_client_blocks` in either state.
    pub fn unset_split_client_blocks(&mut self) -> &mut Self {
        self.raw.remove("split_client_blocks");
        self
    }

    // ---------------------------------------------------------- internals --

    fn entries(&self, key: &str) -> Vec<Value> {
        match self.raw.get(key) {
            Some(Value::Array(a)) => a.clone(),
            _ => Vec::new(),
        }
    }

    pub(crate) fn set_entries(&mut self, key: &str, values: Vec<Value>) {
        self.raw.insert(key.to_string(), Value::Array(values));
    }

    pub(crate) fn raw_get(&self, key: &str) -> Option<&Value> {
        self.raw.get(key)
    }
}

/// The bare IP inside a root entry, for the accessor. `validate` — not this —
/// is what refuses a malformed entry.
fn entry_ip(v: &Value) -> Option<&str> {
    match v {
        Value::String(s) => Some(s.as_str()),
        Value::Object(m) => m.get("Ip").and_then(|x| x.as_str()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_client_blocks_false_removes_the_key() {
        let mut cfg = GossipConfig::parse(
            r#"{"chain":"Mainnet","root_node_ips":[{"Ip":"192.0.2.10"}],"split_client_blocks":true}"#,
        )
        .unwrap();
        cfg.set_split_client_blocks(false);
        assert!(!cfg.to_json().contains("split_client_blocks"));
    }

    #[test]
    fn an_explicit_false_survives_a_round_trip() {
        let text = "{\n  \"chain\": \"Mainnet\",\n  \"root_node_ips\": [\n    {\n      \"Ip\": \"192.0.2.10\"\n    }\n  ],\n  \"split_client_blocks\": false\n}\n";
        assert_eq!(GossipConfig::parse(text).unwrap().to_json(), text);
    }

    #[test]
    fn n_gossip_peers_refuses_both_ends() {
        let mut cfg = GossipConfig::new("Mainnet", ["192.0.2.10"]);
        assert!(cfg.set_n_gossip_peers(7).is_err());
        assert!(cfg.set_n_gossip_peers(101).is_err());
        assert!(cfg.set_n_gossip_peers(8).is_ok());
        assert!(cfg.set_n_gossip_peers(100).is_ok());
    }
}

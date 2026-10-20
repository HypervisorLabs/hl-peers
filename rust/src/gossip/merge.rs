//! `merge_peers` / `from_peers` — `CLIENT_SPEC.md` §6.5.
//!
//! Deterministic, order-preserving, no sorting. The API already ranked the
//! peers with `sort`; a client that re-ranked them would be answering a
//! different question than the one the caller asked.

use serde_json::Value;

use super::validate::EMPTY_ROOTS_REASON;
use super::{ip, GossipConfig, RootIp};
use crate::error::{Error, Result, Warning, WarningCode};

/// How an existing root list and the new candidates are combined.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
#[non_exhaustive]
pub enum MergeMode {
    /// `keep_existing ++ candidates`. The default.
    #[default]
    Replace,
    /// `keep_existing ++ existing ++ candidates`.
    Append,
    /// `keep_existing ++ candidates ++ existing`.
    Prepend,
}

impl MergeMode {
    /// Parse the CLI/fixture spelling (`replace`, `append`, `prepend`).
    pub fn parse(s: &str) -> Result<MergeMode> {
        match s {
            "replace" => Ok(MergeMode::Replace),
            "append" => Ok(MergeMode::Append),
            "prepend" => Ok(MergeMode::Prepend),
            other => Err(Error::invalid(
                "mode",
                format!("{other} is not one of replace, append, prepend"),
            )),
        }
    }
}

/// Options for [`GossipConfig::merge_peers`] and [`GossipConfig::from_peers`].
///
/// Construct with [`MergeOpts::default`] and assign the fields you want; the
/// struct is `#[non_exhaustive]` because §6.5 may grow.
#[derive(Debug, Clone)]
#[non_exhaustive]
pub struct MergeOpts {
    /// Hard cap on the resulting `root_node_ips` length. **Default 8**, not the
    /// upstream contract's ≤ 24 (D-D): a shorter root list starts the node
    /// faster, and a longer one does not buy better peers. Pass 24 for the
    /// upstream default.
    pub max_roots: usize,
    /// How to combine with the existing list.
    pub mode: MergeMode,
    /// Drop repeats, first occurrence wins. Default `true`. With `false`, a
    /// duplicate makes `validate` reject the result.
    pub dedupe: bool,
    /// IPs the operator pins: always retained and placed first.
    pub keep_existing: Vec<String>,
    /// IPs never to include. Applied last, to everything. 🪤 `exclude` beats
    /// `keep_existing`, and the conflict is surfaced as a warning naming the IP.
    pub exclude: Vec<String>,
    /// The chain for [`GossipConfig::from_peers`]. Required there; ignored by
    /// `merge_peers`, which leaves the existing chain alone.
    pub chain: Option<String>,
    /// `try_new_peers` for [`GossipConfig::from_peers`] (defaults to `true`
    /// there). `merge_peers` leaves the existing value untouched.
    pub try_new_peers: Option<bool>,
}

impl Default for MergeOpts {
    fn default() -> Self {
        MergeOpts {
            max_roots: 8,
            mode: MergeMode::Replace,
            dedupe: true,
            keep_existing: Vec::new(),
            exclude: Vec::new(),
            chain: None,
            try_new_peers: None,
        }
    }
}

impl MergeOpts {
    /// Set [`MergeOpts::max_roots`].
    pub fn with_max_roots(mut self, n: usize) -> Self {
        self.max_roots = n;
        self
    }

    /// Set [`MergeOpts::mode`].
    pub fn with_mode(mut self, mode: MergeMode) -> Self {
        self.mode = mode;
        self
    }

    /// Set [`MergeOpts::dedupe`].
    pub fn with_dedupe(mut self, dedupe: bool) -> Self {
        self.dedupe = dedupe;
        self
    }

    /// Set [`MergeOpts::keep_existing`] — the IPs the operator pins.
    pub fn with_keep_existing<I, S>(mut self, ips: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        self.keep_existing = ips.into_iter().map(|s| s.as_ref().to_string()).collect();
        self
    }

    /// Set [`MergeOpts::exclude`]. 🪤 It beats `keep_existing`.
    pub fn with_exclude<I, S>(mut self, ips: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        self.exclude = ips.into_iter().map(|s| s.as_ref().to_string()).collect();
        self
    }

    /// Set [`MergeOpts::chain`], which [`GossipConfig::from_peers`] requires.
    pub fn with_chain(mut self, chain: impl Into<String>) -> Self {
        self.chain = Some(chain.into());
        self
    }

    /// Set [`MergeOpts::try_new_peers`].
    pub fn with_try_new_peers(mut self, value: bool) -> Self {
        self.try_new_peers = Some(value);
        self
    }
}

/// One candidate root on the way in: a bare IPv4 string, a `Peer`, or a JSON
/// value carrying an `id`.
///
/// A value whose IP cannot be read becomes an *invalid* candidate, which §6.5(2)
/// makes an error rather than a silent drop.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate(String);

impl Candidate {
    /// The IP text this candidate carries.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl From<&str> for Candidate {
    fn from(s: &str) -> Self {
        Candidate(s.to_string())
    }
}

impl From<String> for Candidate {
    fn from(s: String) -> Self {
        Candidate(s)
    }
}

impl From<&String> for Candidate {
    fn from(s: &String) -> Self {
        Candidate(s.clone())
    }
}

impl From<&Value> for Candidate {
    fn from(v: &Value) -> Self {
        match v {
            Value::String(s) => Candidate(s.clone()),
            Value::Object(m) => Candidate(
                m.get("id")
                    .and_then(|x| x.as_str())
                    .unwrap_or_default()
                    .to_string(),
            ),
            other => Candidate(other.to_string()),
        }
    }
}

#[cfg(feature = "client")]
impl From<&crate::Peer> for Candidate {
    fn from(p: &crate::Peer) -> Self {
        Candidate(p.id.clone())
    }
}

impl GossipConfig {
    /// Fold a ranked peer list into this config's `root_node_ips` (§6.5).
    ///
    /// Returns the warnings this merge produced (`truncated`,
    /// `exclude_overrides_keep_existing`) **and retains them** on the config as
    /// pending warnings, so [`validate`](GossipConfig::validate) and
    /// [`save`](GossipConfig::save) re-report them (§6.5). It runs [`validate`](GossipConfig::validate)
    /// on the result and propagates its *error*, which is how `dedupe: false`
    /// with duplicate input is refused.
    ///
    /// 🔴 An empty result raises rather than writing a zero-root config. Never
    /// fall back to the previous roots silently.
    ///
    /// ```
    /// # use hl_peers::{GossipConfig, MergeOpts, MergeMode};
    /// let mut cfg = GossipConfig::new("Mainnet", ["192.0.2.10"]);
    /// let opts = MergeOpts::default().with_mode(MergeMode::Append);
    /// cfg.merge_peers(["203.0.113.1"], &opts).unwrap();
    /// assert_eq!(cfg.root_ips(), vec!["192.0.2.10", "203.0.113.1"]);
    /// ```
    pub fn merge_peers<I, C>(&mut self, peers: I, opts: &MergeOpts) -> Result<Vec<Warning>>
    where
        I: IntoIterator<Item = C>,
        C: Into<Candidate>,
    {
        let mut warnings = Vec::new();

        // (1)(2) candidates in the order given; an unreadable one is an error.
        let mut candidates: Vec<String> = Vec::new();
        for (n, c) in peers.into_iter().enumerate() {
            let c = c.into();
            if !ip::is_ipv4(c.as_str()) {
                return Err(Error::invalid(
                    format!("peers[{n}]"),
                    format!(
                        "{}; a candidate is refused, never silently dropped — dropping it would shrink the root list without telling the operator",
                        ip::bad_address_reason(c.as_str())
                    ),
                ));
            }
            candidates.push(c.0);
        }

        let existing = self.root_ips();
        // keep_existing is filtered to valid IPv4, in the order given.
        let keep: Vec<String> = opts
            .keep_existing
            .iter()
            .filter(|s| ip::is_ipv4(s))
            .cloned()
            .collect();

        // (3)(4)
        let mut merged: Vec<String> = keep.clone();
        match opts.mode {
            MergeMode::Replace => merged.extend(candidates),
            MergeMode::Append => {
                merged.extend(existing);
                merged.extend(candidates);
            }
            MergeMode::Prepend => {
                merged.extend(candidates);
                merged.extend(existing);
            }
        }

        // (5) exclude beats keep_existing.
        if !opts.exclude.is_empty() {
            let clashes: Vec<&String> = keep.iter().filter(|k| opts.exclude.contains(k)).collect();
            if !clashes.is_empty() {
                warnings.push(
                    Warning::new(
                        WarningCode::ExcludeOverridesKeepExisting,
                        "an IP was both pinned with keep_existing and excluded; exclude wins",
                    )
                    .with_detail(
                        clashes
                            .iter()
                            .map(|s| s.as_str())
                            .collect::<Vec<_>>()
                            .join(", "),
                    ),
                );
            }
            merged.retain(|m| !opts.exclude.contains(m));
        }

        // (6) dedupe, first wins.
        if opts.dedupe {
            let mut seen: Vec<String> = Vec::with_capacity(merged.len());
            for m in merged {
                if !seen.contains(&m) {
                    seen.push(m);
                }
            }
            merged = seen;
        }

        // (7) truncate, keeping the head.
        if merged.len() > opts.max_roots {
            let dropped = merged.len() - opts.max_roots;
            merged.truncate(opts.max_roots);
            warnings.push(Warning::new(
                WarningCode::Truncated,
                format!(
                    "dropped {dropped} candidate(s) at max_roots={}; the head is kept because the API already ranked it",
                    opts.max_roots
                ),
            ));
        }

        // (8) never write the crash-loop shape.
        if merged.is_empty() {
            return Err(Error::invalid(
                "root_node_ips",
                format!("{EMPTY_ROOTS_REASON}. The filter matched no peers — widen it, or pass keep_existing"),
            ));
        }

        // (9) roots only; every other field, known or unknown, is untouched.
        let entries: Vec<Value> = merged
            .iter()
            .map(|s| RootIp::from(s.as_str()).into_value())
            .collect();
        self.set_entries("root_node_ips", entries);

        // §6.5: the merge's warnings are both returned AND retained on the
        // value, replacing whatever a previous merge left. `validate` then
        // re-reports them unioned with the state-derived ones.
        self.set_pending_warnings(warnings.clone());

        // The result must be a config we would be willing to write. Its own
        // warning set is not this operation's return value.
        self.validate()?;
        Ok(warnings)
    }

    /// Build a **new** config from a ranked peer list (§6.5).
    ///
    /// `opts.chain` is required: a `chain` that disagrees with the node's
    /// `--chain` stops the node from starting, and guessing is how that happens.
    ///
    /// ```
    /// # use hl_peers::{GossipConfig, MergeOpts};
    /// let opts = MergeOpts::default().with_chain("Mainnet").with_try_new_peers(true);
    /// let (cfg, _warnings) = GossipConfig::from_peers(["192.0.2.10"], &opts).unwrap();
    /// assert_eq!(cfg.root_ips(), vec!["192.0.2.10"]);
    /// ```
    pub fn from_peers<I, C>(peers: I, opts: &MergeOpts) -> Result<(GossipConfig, Vec<Warning>)>
    where
        I: IntoIterator<Item = C>,
        C: Into<Candidate>,
    {
        let chain = opts.chain.as_deref().ok_or_else(|| {
            Error::invalid(
                "chain",
                "from_peers needs an explicit chain: never default it to Mainnet, because a chain that disagrees with the node's --chain stops the node from starting",
            )
        })?;
        let mut cfg = GossipConfig::new(chain, Vec::<String>::new());
        cfg.set_try_new_peers(opts.try_new_peers.unwrap_or(true));
        let mut replace = opts.clone();
        replace.mode = MergeMode::Replace;
        let warnings = cfg.merge_peers(peers, &replace)?;
        Ok((cfg, warnings))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exclude_beats_keep_existing_and_warns() {
        let mut cfg = GossipConfig::new("Mainnet", ["192.0.2.10"]);
        cfg.set_try_new_peers(false);
        let opts = MergeOpts::default()
            .with_keep_existing(["192.0.2.10"])
            .with_exclude(["192.0.2.10"]);
        let w = cfg.merge_peers(["203.0.113.1"], &opts).unwrap();
        assert_eq!(cfg.root_ips(), vec!["203.0.113.1"]);
        assert_eq!(w[0].code, WarningCode::ExcludeOverridesKeepExisting);
    }

    #[test]
    fn an_empty_result_refuses_rather_than_keeping_the_old_roots() {
        let mut cfg = GossipConfig::new("Mainnet", ["192.0.2.10"]);
        let e = cfg
            .merge_peers(Vec::<String>::new(), &MergeOpts::default())
            .unwrap_err();
        assert!(e.reason().unwrap().contains("empty root_node_ips"));
        // untouched
        assert_eq!(cfg.root_ips(), vec!["192.0.2.10"]);
    }
}

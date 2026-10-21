//! `validate()` — the ordered checks and the closed warning set of
//! `CLIENT_SPEC.md` §6.4.

use serde_json::Value;

use super::ip;
use super::GossipConfig;
use crate::error::{Error, Result, Warning, WarningCode};

/// The verbatim reason §6.4(1) requires. This is the single most important
/// guard in the library.
pub(crate) const EMPTY_ROOTS_REASON: &str = "refusing to emit an empty root_node_ips: hl-node asserts on a non-empty list at start and will crash-loop; there is no built-in fallback";

impl GossipConfig {
    /// Check the config, returning the **first** error in the §6.4 order and,
    /// on success, the warnings that fired.
    ///
    /// The order is fixed so that all four language clients report the same
    /// error for the same file.
    ///
    /// The result is the **union** of this config's pending merge warnings
    /// (§6.5) and the state-derived ones, de-duplicated by code, so a warning
    /// raised during a merge cannot be lost by a caller who ignored what the
    /// merge returned.
    ///
    /// ```
    /// # use hl_peers::{GossipConfig, WarningCode};
    /// let cfg = GossipConfig::parse(r#"{"root_node_ips": [{"Ip": "192.0.2.10"}]}"#).unwrap();
    /// let warnings = cfg.validate().unwrap();
    /// assert_eq!(warnings[0].code, WarningCode::ChainAbsent);
    ///
    /// let empty = GossipConfig::parse(r#"{"chain": "Mainnet", "root_node_ips": []}"#).unwrap();
    /// assert!(empty.validate().is_err()); // the crash-loop shape, refused
    /// ```
    pub fn validate(&self) -> Result<Vec<Warning>> {
        // (1) root_node_ips absent, wrong type, or empty.
        let roots: &Vec<Value> = match self.raw_get("root_node_ips") {
            None => return Err(Error::invalid("root_node_ips", EMPTY_ROOTS_REASON)),
            Some(Value::Array(a)) if a.is_empty() => {
                return Err(Error::invalid("root_node_ips", EMPTY_ROOTS_REASON))
            }
            Some(Value::Array(a)) => a,
            Some(other) => {
                return Err(Error::invalid(
                    "root_node_ips",
                    format!(
                        "expected an array of {{\"Ip\": …}} entries, found {}",
                        json_type_name(other)
                    ),
                ))
            }
        };

        // (2) every root entry is an object whose single key is `Ip`.
        for (n, entry) in roots.iter().enumerate() {
            let param = format!("root_node_ips[{n}]");
            match entry {
                Value::Object(m) => {
                    if m.len() != 1 || !m.contains_key("Ip") {
                        return Err(Error::invalid(
                            param,
                            format!(
                                "a root entry is an object with the single key `Ip` (capital I); found {{{}}}",
                                m.keys().cloned().collect::<Vec<_>>().join(", ")
                            ),
                        ));
                    }
                    if !m["Ip"].is_string() {
                        return Err(Error::invalid(param, "`Ip` must be a string"));
                    }
                }
                Value::String(_) => {
                    return Err(Error::invalid(
                        param,
                        "a bare string is not a root entry: the file needs {\"Ip\": \"…\"} (bare strings are accepted only as constructor input)",
                    ))
                }
                other => {
                    return Err(Error::invalid(
                        param,
                        format!(
                            "a root entry is an object with the single key `Ip`; found {}",
                            json_type_name(other)
                        ),
                    ))
                }
            }
        }

        // reserved_peer_ips must be an array of bare strings.
        let reserved: Vec<&Value> = match self.raw_get("reserved_peer_ips") {
            None => Vec::new(),
            Some(Value::Array(a)) => a.iter().collect(),
            Some(other) => {
                return Err(Error::invalid(
                    "reserved_peer_ips",
                    format!(
                        "expected an array of bare IPv4 strings, found {}",
                        json_type_name(other)
                    ),
                ))
            }
        };

        // (3) every root and reserved IP is a dotted-quad IPv4.
        for (n, entry) in roots.iter().enumerate() {
            let value = entry["Ip"].as_str().unwrap_or_default();
            if !ip::is_ipv4(value) {
                return Err(Error::invalid(
                    format!("root_node_ips[{n}]"),
                    ip::bad_address_reason(value),
                ));
            }
        }
        for (n, entry) in reserved.iter().enumerate() {
            let param = format!("reserved_peer_ips[{n}]");
            match entry {
                Value::String(s) => {
                    if !ip::is_ipv4(s) {
                        return Err(Error::invalid(param, ip::bad_address_reason(s)));
                    }
                }
                Value::Object(_) => {
                    return Err(Error::invalid(
                        param,
                        "reserved_peer_ips entries are BARE IPv4 strings; the {\"Ip\": …} wrapper belongs to root_node_ips only",
                    ))
                }
                other => {
                    return Err(Error::invalid(
                        param,
                        format!(
                            "reserved_peer_ips entries are bare IPv4 strings, found {}",
                            json_type_name(other)
                        ),
                    ))
                }
            }
        }

        // (4) duplicates, each list independently. An IP MAY appear in both.
        if let Some(dup) = first_duplicate(&self.root_ips()) {
            return Err(Error::invalid("root_node_ips", format!("duplicate {dup}")));
        }
        if let Some(dup) = first_duplicate(&self.reserved_peer_ips()) {
            return Err(Error::invalid(
                "reserved_peer_ips",
                format!("duplicate {dup}"),
            ));
        }

        // (5) chain spelling. Absent is allowed here and warned about below.
        if let Some(chain) = self.raw_get("chain") {
            match chain.as_str() {
                Some("Mainnet") | Some("Testnet") => {}
                _ => {
                    return Err(Error::invalid(
                        "chain",
                        format!(
                            "expected exactly `Mainnet` or `Testnet` (case matters), found {chain}"
                        ),
                    ))
                }
            }
        }

        // (6) n_gossip_peers.
        if let Some(v) = self.raw_get("n_gossip_peers") {
            match v.as_i64() {
                Some(n) if (8..=100).contains(&n) && v.is_i64() => {}
                _ => {
                    return Err(Error::invalid(
                        "n_gossip_peers",
                        format!("expected an integer in [8, 100], found {v}"),
                    ))
                }
            }
        }

        // (7) the two booleans.
        for key in ["try_new_peers", "split_client_blocks"] {
            if let Some(v) = self.raw_get(key) {
                if !v.is_boolean() {
                    return Err(Error::invalid(
                        key,
                        format!("expected a boolean, found {}", json_type_name(v)),
                    ));
                }
            }
        }

        // §6.5: the union of the pending merge warnings and the
        // state-derived ones, de-duplicated by code.
        let mut out = self.pending_warnings().to_vec();
        for w in self.warnings(roots.len()) {
            if !out.iter().any(|x| x.code == w.code) {
                out.push(w);
            }
        }
        Ok(out)
    }

    fn warnings(&self, n_roots: usize) -> Vec<Warning> {
        let mut out = Vec::new();
        if n_roots > 64 {
            out.push(Warning::new(
                WarningCode::ManyRootsGt64,
                format!("{n_roots} roots (over 64, escalated): a long root list makes the node slower to start and buys nothing; prefer a short list"),
            ));
        } else if n_roots > 24 {
            out.push(Warning::new(
                WarningCode::ManyRootsGt24,
                format!("{n_roots} roots: a long root list makes the node slower to start and buys nothing; prefer a short list"),
            ));
        }
        if self.chain().is_none() {
            out.push(Warning::new(
                WarningCode::ChainAbsent,
                "emit `chain` matching the node's `--chain`; a mismatch stops the node from starting, and this library cannot tell which chain your node runs",
            ));
        }
        // 🔴 The codes are INDEPENDENT (§6.4). Each condition is evaluated on
        // its own and every one that holds fires; a `many_roots_*` warning does
        // not suppress, absorb or outrank this one. They describe two unrelated
        // facts — this list is slow to boot, and this list may or may not pin —
        // and swallowing one because the other fired would hide the harder
        // problem behind the easier one. The only mutually exclusive pair is
        // `many_roots_gt_24` / `many_roots_gt_64`, above.
        if self.try_new_peers() == Some(false) && n_roots > 1 {
            out.push(Warning::new(
                WarningCode::TryNewPeersFalseMultiRoot,
                "whether `try_new_peers: false` restricts a multi-entry list to exactly that list is unverified; only a single root is known to pin",
            ));
        }
        out
    }
}

fn first_duplicate(items: &[String]) -> Option<&String> {
    for (i, a) in items.iter().enumerate() {
        if items[..i].contains(a) {
            return Some(a);
        }
    }
    None
}

fn json_type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "a boolean",
        Value::Number(_) => "a number",
        Value::String(_) => "a string",
        Value::Array(_) => "an array",
        Value::Object(_) => "an object",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(text: &str) -> GossipConfig {
        GossipConfig::parse(text).unwrap()
    }

    #[test]
    fn the_first_error_wins_in_spec_order() {
        // Both an empty root list and a bad chain: §6.4(1) reports first.
        let e = cfg(r#"{"chain":"mainnet","root_node_ips":[]}"#)
            .validate()
            .unwrap_err();
        assert_eq!(e.parameter(), Some("root_node_ips"));
    }

    #[test]
    fn an_ip_may_be_root_and_reserved() {
        let c = cfg(
            r#"{"chain":"Mainnet","root_node_ips":[{"Ip":"192.0.2.10"}],"reserved_peer_ips":["192.0.2.10"]}"#,
        );
        assert!(c.validate().is_ok());
    }

    #[test]
    fn gt_64_fires_instead_of_gt_24() {
        let ips: Vec<String> = (1..=70).map(|i| format!("203.0.113.{i}")).collect();
        let mut c = GossipConfig::new("Mainnet", ips.iter());
        c.set_try_new_peers(true);
        let w = c.validate().unwrap();
        let codes: Vec<_> = w.iter().map(|x| x.code).collect();
        assert_eq!(codes, vec![WarningCode::ManyRootsGt64]);
    }
}

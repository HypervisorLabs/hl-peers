//! JSON-in / JSON-out differential harness for the Rust client.
//!
//! One NDJSON request per line on stdin, one NDJSON answer per line on stdout.
//! `tools/cross_check/run.py` drives this and the three sibling harnesses side
//! by side and asserts that all four answer identically.
//!
//! The harness holds no expectations of its own: it reports what the client
//! did. It never panics the process — one bad request must not kill the stream.
//!
//! Run it with `cargo run --features blocking --bin cross-check`.

use std::io::{BufRead, Write};
use std::panic::AssertUnwindSafe;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use hl_peers::{
    AuctionStatus, Candidate, Chain, Client, Connectable, Error, FixedClock, Geo, GetPeerOpts,
    GossipConfig, IterOpts, MergeMode, MergeOpts, Network, NetworkMapOpts, NoJitter, Peer,
    PeerQuery, PeerState, RecordingSleeper, SaveOpts, Sort, TierFilter, Warning,
};
use serde_json::{json, Map, Value};

/// The canonical URL `query_url` reports: the default host, plain paths.
const BASE: &str = hl_peers::DEFAULT_BASE_URL;

/// 🪤 Not `Format::parse`, which also accepts the CLI spelling `gossip-config`.
const FORMAT: [&str; 2] = ["json", "gossip_config"];

/// A failure: either the client refused (reportable) or the harness could not
/// express the request at all (a harness bug, reported as such).
enum Fail {
    Client(Error),
    Harness(String),
}

impl From<Error> for Fail {
    fn from(e: Error) -> Self {
        Fail::Client(e)
    }
}

// ---------------------------------------------------------------------------
// errors
// ---------------------------------------------------------------------------

/// `{kind, parameter?, filter?, retry_after?, seen?, at_op?}` — keys whose
/// value is null are omitted, matching the Python harness.
///
/// The HTTP status is deliberately **not** reported: CLIENT_SPEC §5.1 puts it on
/// the `Server` variant alone, so exposing it elsewhere is a per-client accident
/// rather than contract behaviour.
fn error_payload(e: &Error, at_op: Option<i64>) -> Value {
    error_payload_at(e, at_op, false)
}

fn error_payload_at(e: &Error, at_op: Option<i64>, with_clock: bool) -> Value {
    let mut out = Map::new();
    out.insert("kind".into(), Value::String(e.kind().to_string()));
    if let Some(p) = e.parameter() {
        out.insert("parameter".into(), Value::String(p.to_string()));
    }
    if let Error::FilterUnavailable {
        filter: Some(f), ..
    } = e
    {
        out.insert("filter".into(), Value::String(f.clone()));
    }
    if let Error::RateLimited {
        retry_after,
        reset_at,
        signup_url,
        ..
    } = e
    {
        if let Some(s) = retry_after {
            out.insert("retry_after".into(), json!(s));
        }
        // Report shape: `reset_at` only when the request carried a
        // `clock_now` (otherwise it is wall-clock and cannot compare), and
        // `signup_url` always — null when absent.
        if with_clock {
            if let Some(at) = reset_at {
                out.insert("reset_at".into(), Value::String(rfc3339(*at)));
            }
        }
        out.insert("signup_url".into(), json!(signup_url));
    }
    if let Error::Truncated { seen } = e {
        out.insert("seen".into(), json!(seen));
    }
    if let Some(n) = at_op {
        out.insert("at_op".into(), json!(n));
    }
    Value::Object(out)
}

fn harness_error(message: impl Into<String>) -> Value {
    json!({"ok": false, "harness_error": message.into()})
}

// ---------------------------------------------------------------------------
// $TMPDIR substitution
// ---------------------------------------------------------------------------

fn subst(v: &Value, tmp: &str) -> Value {
    match v {
        Value::String(s) => Value::String(s.replace("$TMPDIR", tmp)),
        Value::Array(a) => Value::Array(a.iter().map(|x| subst(x, tmp)).collect()),
        Value::Object(m) => {
            Value::Object(m.iter().map(|(k, x)| (k.clone(), subst(x, tmp))).collect())
        }
        other => other.clone(),
    }
}

fn unsubst(v: &Value, tmp: &str) -> Value {
    match v {
        Value::String(s) => Value::String(s.replace(tmp, "$TMPDIR")),
        Value::Array(a) => Value::Array(a.iter().map(|x| unsubst(x, tmp)).collect()),
        Value::Object(m) => Value::Object(
            m.iter()
                .map(|(k, x)| (k.clone(), unsubst(x, tmp)))
                .collect(),
        ),
        other => other.clone(),
    }
}

// ---------------------------------------------------------------------------
// gossip
// ---------------------------------------------------------------------------

static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

fn fresh_tmpdir() -> std::io::Result<PathBuf> {
    let dir = std::env::temp_dir().join(format!(
        "hlp-xcheck-rs-{}-{}",
        std::process::id(),
        TMP_COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}

fn codes(warnings: &[Warning]) -> Vec<String> {
    let mut out: Vec<String> = warnings
        .iter()
        .map(|w| w.code.as_str().to_string())
        .collect();
    out.sort();
    out
}

fn args_of(op: &Value) -> Vec<Value> {
    op.get("args")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

fn opts_of(op: &Value) -> Map<String, Value> {
    op.get("opts")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn arg_str(a: &[Value], n: usize, op: &str) -> Result<String, Fail> {
    match a.get(n).and_then(Value::as_str) {
        Some(s) => Ok(s.to_string()),
        None => Err(Fail::Harness(format!("{op}: argument {n} is not a string"))),
    }
}

fn arg_bool(a: &[Value], n: usize, op: &str) -> Result<bool, Fail> {
    match a.get(n).and_then(Value::as_bool) {
        Some(b) => Ok(b),
        None => Err(Fail::Harness(format!("{op}: argument {n} is not a bool"))),
    }
}

fn arg_i64(a: &[Value], n: usize, op: &str) -> Result<i64, Fail> {
    match a.get(n).and_then(Value::as_i64) {
        Some(v) => Ok(v),
        None => Err(Fail::Harness(format!(
            "{op}: argument {n} is not an integer"
        ))),
    }
}

fn arg_strings(a: &[Value], n: usize, op: &str) -> Result<Vec<String>, Fail> {
    match a.get(n) {
        Some(Value::Array(items)) => Ok(items
            .iter()
            .map(|v| match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            })
            .collect()),
        _ => Err(Fail::Harness(format!("{op}: argument {n} is not a list"))),
    }
}

/// A `peers` argument element is a bare string or an object with an `id`.
fn candidates(v: Option<&Value>) -> Vec<Candidate> {
    v.and_then(Value::as_array)
        .map(|a| a.iter().map(Candidate::from).collect())
        .unwrap_or_default()
}

fn merge_opts(o: &Map<String, Value>) -> Result<MergeOpts, Fail> {
    let mut m = MergeOpts::default();
    for (key, value) in o {
        match key.as_str() {
            "max_roots" => {
                let n = value.as_u64().ok_or_else(|| {
                    Fail::Harness("max_roots is not a non-negative integer".into())
                })?;
                m = m.with_max_roots(n as usize);
            }
            "mode" => {
                let s = value
                    .as_str()
                    .ok_or_else(|| Fail::Harness("mode is not a string".into()))?;
                m = m.with_mode(MergeMode::parse(s)?);
            }
            "dedupe" => {
                let b = value
                    .as_bool()
                    .ok_or_else(|| Fail::Harness("dedupe is not a bool".into()))?;
                m = m.with_dedupe(b);
            }
            "keep_existing" => m = m.with_keep_existing(string_list(value)?),
            "exclude" => m = m.with_exclude(string_list(value)?),
            "chain" => {
                let s = value
                    .as_str()
                    .ok_or_else(|| Fail::Harness("chain is not a string".into()))?;
                m = m.with_chain(s);
            }
            "try_new_peers" => {
                let b = value
                    .as_bool()
                    .ok_or_else(|| Fail::Harness("try_new_peers is not a bool".into()))?;
                m = m.with_try_new_peers(b);
            }
            other => return Err(Fail::Harness(format!("unknown merge option {other}"))),
        }
    }
    Ok(m)
}

fn string_list(v: &Value) -> Result<Vec<String>, Fail> {
    match v {
        Value::Array(a) => Ok(a
            .iter()
            .map(|x| match x {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            })
            .collect()),
        _ => Err(Fail::Harness("expected a list of strings".into())),
    }
}

fn save_opts(o: &Map<String, Value>) -> Result<SaveOpts, Fail> {
    let mut so = SaveOpts::default();
    for (key, value) in o {
        match key.as_str() {
            "dry_run" => {
                so = so.with_dry_run(
                    value
                        .as_bool()
                        .ok_or_else(|| Fail::Harness("dry_run is not a bool".into()))?,
                )
            }
            "backup" => {
                so = so.with_backup(
                    value
                        .as_bool()
                        .ok_or_else(|| Fail::Harness("backup is not a bool".into()))?,
                )
            }
            "file_mode" => {
                so = so.with_file_mode(
                    value
                        .as_u64()
                        .ok_or_else(|| Fail::Harness("file_mode is not an integer".into()))?
                        as u32,
                )
            }
            other => return Err(Fail::Harness(format!("unknown save option {other}"))),
        }
    }
    Ok(so)
}

/// The mutable state one gossip programme threads through its operations.
#[derive(Default)]
struct GossipRun {
    subject: Option<GossipConfig>,
    value: Value,
    op_warnings: Option<Vec<String>>,
    saved_text: Option<String>,
}

impl GossipRun {
    fn config(&mut self, op: &str) -> Result<&mut GossipConfig, Fail> {
        self.subject
            .as_mut()
            .ok_or_else(|| Fail::Harness(format!("operation `{op}` needs a subject")))
    }

    fn apply(&mut self, op: &Value, tmp: &str) -> Result<(), Fail> {
        let name = match op.get("op").and_then(Value::as_str) {
            Some(n) => n.to_string(),
            None => return Err(Fail::Harness("an operation has no `op`".into())),
        };
        let a: Vec<Value> = args_of(op)
            .iter()
            .map(|v| subst(v, tmp))
            .collect::<Vec<Value>>();
        let o: Map<String, Value> = opts_of(op)
            .iter()
            .map(|(k, v)| (k.clone(), subst(v, tmp)))
            .collect();

        match name.as_str() {
            "new" => {
                let chain = arg_str(&a, 0, "new")?;
                let ips: Vec<Value> = a
                    .get(1)
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                self.subject = Some(GossipConfig::new(&chain, ips.iter()));
                self.value = Value::Null;
            }
            "from_peers" => {
                let (cfg, warnings) =
                    GossipConfig::from_peers(candidates(a.first()), &merge_opts(&o)?)?;
                self.subject = Some(cfg);
                self.op_warnings = Some(codes(&warnings));
                self.value = Value::Null;
            }
            "merge_peers" => {
                let peers = candidates(a.first());
                let opts = merge_opts(&o)?;
                let warnings = self.config("merge_peers")?.merge_peers(peers, &opts)?;
                self.op_warnings = Some(codes(&warnings));
                self.value = Value::Null;
            }
            "save" => {
                let path = arg_str(&a, 0, "save")?;
                let so = save_opts(&o)?;
                let result = self.config("save")?.save(&path, &so)?;
                self.value = json!({
                    "written": result.written,
                    "changed": result.changed,
                    "diff": result.diff,
                    "backup": result.backup_path.is_some(),
                });
                self.op_warnings = Some(codes(&result.warnings));
                self.saved_text = std::fs::read_to_string(&path).ok();
            }
            "validate" => {
                let warnings = self.config("validate")?.validate()?;
                self.value = Value::String("ok".into());
                self.op_warnings = Some(codes(&warnings));
            }
            "to_json" => {
                self.value = Value::String(self.config("to_json")?.to_json());
            }
            "root_ips" => {
                self.value = json!(self.config("root_ips")?.root_ips());
            }
            "reserved_peer_ips" => {
                self.value = json!(self.config("reserved_peer_ips")?.reserved_peer_ips());
            }
            "has_root_ip" => {
                let ip = arg_str(&a, 0, "has_root_ip")?;
                self.value = Value::Bool(self.config("has_root_ip")?.has_root_ip(&ip));
            }
            "add_root_ip" => {
                let ip = arg_str(&a, 0, &name)?;
                self.config(&name)?.add_root_ip(&ip);
                self.value = Value::Null;
            }
            "remove_root_ip" => {
                let ip = arg_str(&a, 0, &name)?;
                self.config(&name)?.remove_root_ip(&ip);
                self.value = Value::Null;
            }
            "set_root_ips" => {
                let ips = arg_strings(&a, 0, &name)?;
                self.config(&name)?.set_root_ips(ips);
                self.value = Value::Null;
            }
            "add_reserved_peer_ip" => {
                let ip = arg_str(&a, 0, &name)?;
                self.config(&name)?.add_reserved_peer_ip(&ip);
                self.value = Value::Null;
            }
            "remove_reserved_peer_ip" => {
                let ip = arg_str(&a, 0, &name)?;
                self.config(&name)?.remove_reserved_peer_ip(&ip);
                self.value = Value::Null;
            }
            "set_reserved_peer_ips" => {
                let ips = arg_strings(&a, 0, &name)?;
                self.config(&name)?.set_reserved_peer_ips(ips);
                self.value = Value::Null;
            }
            "set_chain" => {
                let chain = arg_str(&a, 0, &name)?;
                self.config(&name)?.set_chain(&chain);
                self.value = Value::Null;
            }
            "set_try_new_peers" => {
                let v = arg_bool(&a, 0, &name)?;
                self.config(&name)?.set_try_new_peers(v);
                self.value = Value::Null;
            }
            "unset_try_new_peers" => {
                self.config(&name)?.unset_try_new_peers();
                self.value = Value::Null;
            }
            "set_n_gossip_peers" => {
                let v = arg_i64(&a, 0, &name)?;
                self.config(&name)?.set_n_gossip_peers(v)?;
                self.value = Value::Null;
            }
            "unset_n_gossip_peers" => {
                self.config(&name)?.unset_n_gossip_peers();
                self.value = Value::Null;
            }
            "set_split_client_blocks" => {
                let v = arg_bool(&a, 0, &name)?;
                self.config(&name)?.set_split_client_blocks(v);
                self.value = Value::Null;
            }
            "unset_split_client_blocks" => {
                self.config(&name)?.unset_split_client_blocks();
                self.value = Value::Null;
            }
            other => return Err(Fail::Harness(format!("unknown operation {other}"))),
        }
        Ok(())
    }
}

fn run_gossip(req: &Value) -> Value {
    let dir = match fresh_tmpdir() {
        Ok(d) => d,
        Err(e) => return harness_error(format!("could not create a temp dir: {e}")),
    };
    let answer = run_gossip_in(req, &dir);
    let _ = std::fs::remove_dir_all(&dir);
    answer
}

fn run_gossip_in(req: &Value, dir: &Path) -> Value {
    let tmp = dir.display().to_string();
    let mut run = GossipRun::default();

    // (1) build the subject.
    if let Some(text) = req.get("input_json_text").and_then(Value::as_str) {
        match GossipConfig::parse(text) {
            Ok(cfg) => run.subject = Some(cfg),
            Err(e) => return json!({"ok": false, "error": error_payload(&e, Some(-1))}),
        }
    } else if let Some(cfg) = req.get("input_config").filter(|v| !v.is_null()) {
        // fixtures/README: `parse(render(input_config))`.
        match GossipConfig::parse(&cfg.to_string()) {
            Ok(cfg) => run.subject = Some(cfg),
            Err(e) => return json!({"ok": false, "error": error_payload(&e, Some(-1))}),
        }
    }

    // (2) apply the operations in order.
    let ops = req
        .get("operations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for (index, op) in ops.iter().enumerate() {
        match run.apply(op, &tmp) {
            Ok(()) => {}
            Err(Fail::Client(e)) => {
                return json!({"ok": false, "error": error_payload(&e, Some(index as i64))})
            }
            Err(Fail::Harness(m)) => return harness_error(m),
        }
    }

    // (3) report.
    let mut answer = Map::new();
    answer.insert("ok".into(), Value::Bool(true));
    answer.insert("value".into(), unsubst(&run.value, &tmp));
    answer.insert(
        "json_text".into(),
        match &run.subject {
            Some(cfg) => Value::String(cfg.to_json()),
            None => Value::Null,
        },
    );
    answer.insert(
        "op_warnings".into(),
        match &run.op_warnings {
            Some(w) => json!(w),
            None => Value::Null,
        },
    );
    answer.insert(
        "saved_text".into(),
        match &run.saved_text {
            Some(t) => Value::String(t.clone()),
            None => Value::Null,
        },
    );
    match &run.subject {
        None => {
            answer.insert("warnings".into(), Value::Null);
        }
        Some(cfg) => match cfg.validate() {
            Ok(w) => {
                answer.insert("warnings".into(), json!(codes(&w)));
            }
            Err(e) => {
                answer.insert("warnings".into(), Value::Null);
                answer.insert("validate_error".into(), error_payload(&e, None));
            }
        },
    }
    Value::Object(answer)
}

// ---------------------------------------------------------------------------
// query
// ---------------------------------------------------------------------------

/// Python's `_flatten`: `["DE","NL"]`, `"DE,NL"` and `["DE,NL"]` are one thing.
fn csv_items(v: &Value, name: &str) -> Result<Vec<String>, Fail> {
    fn push(out: &mut Vec<String>, s: &str) {
        out.extend(s.split(',').filter(|p| !p.is_empty()).map(str::to_string));
    }
    let mut out = Vec::new();
    match v {
        Value::Null => {}
        Value::String(s) => push(&mut out, s),
        Value::Array(items) => {
            for item in items {
                match item {
                    Value::Null => {}
                    Value::String(s) => push(&mut out, s),
                    other => out.push(other.to_string()),
                }
            }
        }
        other => {
            return Err(Fail::Harness(format!(
                "{name}: {other} is not a string or list"
            )))
        }
    }
    Ok(out)
}

fn coerce_i64(v: &Value, name: &str) -> Result<i64, Fail> {
    match v {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .ok_or_else(|| Fail::Harness(format!("{name}: {n} is not an integer"))),
        Value::String(s) => s
            .trim()
            .parse::<i64>()
            .map_err(|e| Fail::Harness(format!("{name}: {s:?} is not an integer ({e})"))),
        other => Err(Fail::Harness(format!("{name}: {other} is not an integer"))),
    }
}

fn coerce_f64(v: &Value, name: &str) -> Result<f64, Fail> {
    match v {
        Value::Number(n) => n
            .as_f64()
            .ok_or_else(|| Fail::Harness(format!("{name}: {n} is not a number"))),
        Value::String(s) => s
            .trim()
            .parse::<f64>()
            .map_err(|e| Fail::Harness(format!("{name}: {s:?} is not a number ({e})"))),
        other => Err(Fail::Harness(format!("{name}: {other} is not a number"))),
    }
}

fn coerce_bool(v: &Value, name: &str) -> Result<bool, Fail> {
    match v {
        Value::Bool(b) => Ok(*b),
        Value::String(s) => Ok(s == "true"),
        other => Err(Fail::Harness(format!("{name}: {other} is not a bool"))),
    }
}

fn coerce_str(v: &Value, name: &str) -> Result<String, Fail> {
    match v {
        Value::String(s) => Ok(s.clone()),
        other => Err(Fail::Harness(format!("{name}: {other} is not a string"))),
    }
}

fn tier_items(v: &Value) -> Result<Vec<TierFilter>, Fail> {
    fn one(out: &mut Vec<TierFilter>, s: &str) -> Result<(), Fail> {
        if s == "null" {
            out.push(TierFilter::Unplaced);
            return Ok(());
        }
        s.trim()
            .parse::<i64>()
            .map(|n| out.push(TierFilter::Exact(n)))
            .map_err(|e| Fail::Harness(format!("tier: {s:?} is not an integer or null ({e})")))
    }
    let mut out = Vec::new();
    match v {
        Value::Null => out.push(TierFilter::Unplaced),
        Value::String(s) => {
            for part in s.split(',') {
                one(&mut out, part)?;
            }
        }
        Value::Array(items) => {
            for item in items {
                match item {
                    Value::Null => out.push(TierFilter::Unplaced),
                    Value::Number(n) => {
                        let n = n
                            .as_i64()
                            .ok_or_else(|| Fail::Harness(format!("tier: {n} is not an integer")))?;
                        out.push(TierFilter::Exact(n));
                    }
                    Value::String(s) => one(&mut out, s)?,
                    other => {
                        return Err(Fail::Harness(format!(
                            "tier: {other} is not an integer or null"
                        )))
                    }
                }
            }
        }
        other => {
            return Err(Fail::Harness(format!(
                "tier: {other} is not an integer, null, or a list"
            )))
        }
    }
    Ok(out)
}

fn non_negative(v: i64, name: &str) -> Result<i64, Fail> {
    if v < 0 {
        return Err(Fail::Client(Error::invalid(
            name,
            format!("must be >= 0, got {v}"),
        )));
    }
    Ok(v)
}

/// `^[0-9]+[mhd]$`, byte-safe.
fn seen_within_ok(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() >= 2
        && matches!(b[b.len() - 1], b'm' | b'h' | b'd')
        && b[..b.len() - 1].iter().all(u8::is_ascii_digit)
}

/// Build the query the way the Python harness does: wire-parameter names in
/// sorted order, each setter's own refusal raised where that setter is.
fn build_query(spec: &Map<String, Value>) -> Result<PeerQuery, Fail> {
    let mut q = PeerQuery::new();
    let mut names: Vec<&String> = spec.keys().collect();
    names.sort();
    for name in names {
        let v = &spec[name];
        match name.as_str() {
            "chain" => q = q.chain(Chain::parse(&coerce_str(v, "chain")?)?),
            "network" => q = q.network(Network::parse(&coerce_str(v, "network")?)?),
            "cc" => q = q.cc(csv_items(v, "cc")?),
            "region" => q = q.region(csv_items(v, "region")?),
            "asn" => q = q.asn(csv_items(v, "asn")?),
            "role" => q = q.role(csv_items(v, "role")?),
            "label" => q = q.label(csv_items(v, "label")?),
            "version" => q = q.version(csv_items(v, "version")?),
            "fields" => q = q.fields(csv_items(v, "fields")?),
            "tier" => q = q.tier(tier_items(v)?),
            "max_tier" => q = q.max_tier(non_negative(coerce_i64(v, "max_tier")?, "max_tier")?),
            "max_blocks_behind" => {
                q = q.max_blocks_behind(non_negative(
                    coerce_i64(v, "max_blocks_behind")?,
                    "max_blocks_behind",
                )?)
            }
            "max_latency_ms" => {
                q = q.max_latency_ms(non_negative(
                    coerce_i64(v, "max_latency_ms")?,
                    "max_latency_ms",
                )?)
            }
            "limit" => {
                let n = coerce_i64(v, "limit")?;
                if n < 1 {
                    return Err(Fail::Client(Error::invalid(
                        "limit",
                        format!("must be >= 1, got {n}"),
                    )));
                }
                q = q.limit(n);
            }
            "min_uptime" => {
                let u = coerce_f64(v, "min_uptime")?;
                if !(0.0..=1.0).contains(&u) {
                    return Err(Fail::Client(Error::invalid(
                        "min_uptime",
                        format!("must be within [0, 1], got {u}"),
                    )));
                }
                q = q.min_uptime(u);
            }
            "accepting" => q = q.accepting(coerce_bool(v, "accepting")?),
            // Closed enums: the lenient `from_wire` decodes an unknown member
            // as `Other`, and `PeerQuery::validate` refuses it — the library
            // under test does the membership check, not the harness.
            "connectable" => {
                q = q.connectable(
                    csv_items(v, "connectable")?
                        .iter()
                        .map(|s| Connectable::from_wire(s)),
                )
            }
            "state" => {
                q = q.state(
                    csv_items(v, "state")?
                        .iter()
                        .map(|s| PeerState::from_wire(s)),
                )
            }
            "country" => q = q.country(csv_items(v, "country")?),
            "serves_mempool" => q = q.serves_mempool(serves_mempool_members(v)?),
            "auction" => {
                q = q.auction(
                    nullable_items(v, "auction")?
                        .into_iter()
                        .map(|m| m.map(|s| AuctionStatus::from_wire(&s))),
                )
            }
            "try_new_peers" => q = q.try_new_peers(coerce_bool(v, "try_new_peers")?),
            "vantage" => q = q.vantage(coerce_str(v, "vantage")?),
            "cursor" => q = q.cursor(coerce_str(v, "cursor")?),
            "seen_within" => {
                let s = coerce_str(v, "seen_within")?;
                if !seen_within_ok(&s) {
                    return Err(Fail::Client(Error::invalid(
                        "seen_within",
                        format!("{s:?} does not match ^[0-9]+[mhd]$"),
                    )));
                }
                q = q.seen_within(s);
            }
            "sort" => q = q.sort(Sort::parse(&coerce_str(v, "sort")?)?),
            "format" => {
                let s = coerce_str(v, "format")?;
                if !FORMAT.contains(&s.as_str()) {
                    return Err(Fail::Client(Error::invalid(
                        "format",
                        format!("{s} is not one of {}", FORMAT.join(", ")),
                    )));
                }
                q = q.format(hl_peers::Format::parse(&s)?);
            }
            // A parameter the builder does not model (e.g. the retired
            // `mempool=`, which answers `400 filter-removed`): sent verbatim.
            other => q = q.extra_param(other, coerce_str(v, other)?),
        }
    }
    Ok(q)
}

/// Like [`csv_items`], but `null` (JSON null, or the string member `null`) is
/// kept as [`None`] and a JSON bool becomes `"true"`/`"false"`.
/// `serves_mempool` members as the typed builders take them. A member outside
/// `true | false | null` is unrepresentable there, so it is the client-side
/// `Invalid` here (§2.2(5)).
fn serves_mempool_members(v: &Value) -> Result<Vec<Option<bool>>, Fail> {
    let mut members = Vec::new();
    for m in nullable_items(v, "serves_mempool")? {
        members.push(match m.as_deref() {
            None => None,
            Some("true") => Some(true),
            Some("false") => Some(false),
            Some(other) => {
                return Err(Fail::Client(Error::invalid(
                    "serves_mempool",
                    format!("{other} is not one of true, false, null"),
                )))
            }
        });
    }
    Ok(members)
}

fn nullable_items(v: &Value, name: &str) -> Result<Vec<Option<String>>, Fail> {
    fn push(out: &mut Vec<Option<String>>, s: &str) {
        out.extend(
            s.split(',')
                .filter(|p| !p.is_empty())
                .map(|p| (p != "null").then(|| p.to_string())),
        );
    }
    let mut out = Vec::new();
    let one = |out: &mut Vec<Option<String>>, item: &Value| -> Result<(), Fail> {
        match item {
            Value::Null => out.push(None),
            Value::Bool(b) => out.push(Some(b.to_string())),
            Value::String(s) => push(out, s),
            other => {
                return Err(Fail::Harness(format!(
                    "{name}: {other} is not a string, bool or null"
                )))
            }
        }
        Ok(())
    };
    match v {
        Value::Array(items) => {
            for item in items {
                one(&mut out, item)?;
            }
        }
        other => one(&mut out, other)?,
    }
    Ok(out)
}

/// The §4.8 options, from the same wire-named spec a query uses.
fn build_network_map_opts(spec: &Map<String, Value>) -> Result<NetworkMapOpts, Fail> {
    let mut o = NetworkMapOpts::new();
    for (name, v) in spec {
        match name.as_str() {
            "max_tier" => o = o.max_tier(coerce_i64(v, "max_tier")?),
            "show_unreached" => o = o.show_unreached(coerce_bool(v, "show_unreached")?),
            "hide_unreachable" => o = o.hide_unreachable(coerce_bool(v, "hide_unreachable")?),
            "connectable" => {
                o = o.connectable(
                    csv_items(v, "connectable")?
                        .iter()
                        .map(|s| Connectable::from_wire(s)),
                )
            }
            "cc" => o = o.cc(coerce_str(v, "cc")?),
            "operator" => o = o.operator(coerce_str(v, "operator")?),
            "label" => o = o.label(coerce_str(v, "label")?),
            "network" => o = o.network(Network::parse(&coerce_str(v, "network")?)?),
            "serves_mempool" => o = o.serves_mempool(serves_mempool_members(v)?),
            "auction" => {
                o = o.auction(
                    nullable_items(v, "auction")?
                        .into_iter()
                        .map(|m| m.map(|s| AuctionStatus::from_wire(&s))),
                )
            }
            other => return Err(Fail::Harness(format!("unknown network-map option {other}"))),
        }
    }
    Ok(o)
}

fn preset(name: &str) -> Result<PeerQuery, Fail> {
    match name {
        "tier1" => Ok(PeerQuery::tier1()),
        "sentries" => Ok(PeerQuery::sentries()),
        "not_full_cc" => Ok(PeerQuery::not_full(Geo::cc(["SG"]))),
        "mempool_servers" => Ok(PeerQuery::mempool_servers()),
        other => Err(Fail::Harness(format!("unknown preset {other}"))),
    }
}

fn query_spec(req: &Value) -> Map<String, Value> {
    req.get("query")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn run_query_url(req: &Value) -> Value {
    let built = match req.get("preset").and_then(Value::as_str) {
        Some(name) => preset(name),
        None => build_query(&query_spec(req)),
    };
    let query = match built {
        Ok(q) => q,
        Err(Fail::Client(e)) => return json!({"ok": false, "error": error_payload(&e, None)}),
        Err(Fail::Harness(m)) => return harness_error(m),
    };
    if let Err(e) = query.validate() {
        return json!({"ok": false, "error": error_payload(&e, None)});
    }
    match query.to_query_string() {
        Ok(qs) => {
            let url = if qs.is_empty() {
                format!("{BASE}/peers")
            } else {
                format!("{BASE}/peers?{qs}")
            };
            json!({"ok": true, "url": url})
        }
        Err(e) => json!({"ok": false, "error": error_payload(&e, None)}),
    }
}

// ---------------------------------------------------------------------------
// http
// ---------------------------------------------------------------------------

fn rate_limit_payload(rl: Option<&hl_peers::RateLimit>) -> Value {
    match rl {
        None => json!({
            "limit": Value::Null,
            "remaining": Value::Null,
            "reset": Value::Null,
            "policy": Value::Null,
            "retry_after": Value::Null,
        }),
        Some(rl) => json!({
            "limit": rl.limit,
            "remaining": rl.remaining,
            "reset": rl.reset,
            "policy": rl.policy,
            "retry_after": rl.retry_after,
        }),
    }
}

fn envelope_payload(env: &hl_peers::Envelope) -> Value {
    json!({
        "version": env.version,
        "available": env.available,
        "stale": env.stale,
        "chain": env.chain,
        "network": env.network,
        "generated_at": env.generated_at,
        "next_sweep_at": env.next_sweep_at,
        "attribution": env.attribution,
    })
}

/// `[[id, state, tier, city, version], …]`.
fn rows(peers: &[Peer]) -> Value {
    Value::Array(
        peers
            .iter()
            .map(|p| {
                json!([
                    p.id,
                    p.state.as_ref().map(|s| s.as_str()),
                    p.tier,
                    p.city,
                    p.version
                ])
            })
            .collect(),
    )
}

/// `[[id, name, cc, lat, lon, peers], …]`, or null when absent.
fn cities(c: &Option<Vec<hl_peers::CityCluster>>) -> Value {
    match c {
        None => Value::Null,
        Some(list) => Value::Array(
            list.iter()
                .map(|c| json!([c.id, c.name, c.cc, c.lat, c.lon, c.peers]))
                .collect(),
        ),
    }
}

/// `[[id, mempool.serves, mempool.source, auction.status, auction.slot], …]`.
fn signals(peers: &[Peer]) -> Value {
    Value::Array(
        peers
            .iter()
            .map(|p| {
                json!([
                    p.id,
                    p.mempool.serves,
                    p.mempool.source,
                    p.auction.status.as_ref().map(|s| s.as_str()),
                    p.auction.slot,
                ])
            })
            .collect(),
    )
}

/// `YYYY-MM-DDTHH:MM:SSZ`.
fn rfc3339(t: std::time::SystemTime) -> String {
    let secs = match t.duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => d.as_secs() as i64,
        Err(e) => -(e.duration().as_secs() as i64),
    };
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

/// Days since 1970-01-01 for a civil date (Howard Hinnant).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The `…Z` / `…+00:00` RFC 3339 form the fixtures use.
fn parse_rfc3339(s: &str) -> Option<i64> {
    let s = s.trim();
    let s = s.strip_suffix('Z').unwrap_or(s);
    let s = s.strip_suffix("+00:00").unwrap_or(s);
    let (date, time) = s.split_once('T')?;
    let mut d = date.split('-');
    let y: i64 = d.next()?.parse().ok()?;
    let m: i64 = d.next()?.parse().ok()?;
    let day: i64 = d.next()?.parse().ok()?;
    let mut t = time.split(':');
    let h: i64 = t.next()?.parse().ok()?;
    let mi: i64 = t.next()?.parse().ok()?;
    let sec: f64 = t.next().unwrap_or("0").parse().ok()?;
    Some(days_from_civil(y, m, day) * 86_400 + h * 3600 + mi * 60 + sec as i64)
}

fn build_client(req: &Value) -> Result<Client, Fail> {
    let base_url = req
        .get("base_url")
        .and_then(Value::as_str)
        .ok_or_else(|| Fail::Harness("http request has no base_url".into()))?;
    let mut builder = Client::builder()
        .base_url(base_url)
        // A sleeper that returns immediately, and a fixed jitter factor: the
        // retry *decisions* are what the differential asserts on, never delays.
        .sleeper(Arc::new(RecordingSleeper::default()))
        .jitter(Arc::new(NoJitter));
    if let Some(key) = req.get("api_key").and_then(Value::as_str) {
        builder = builder.api_key(key);
    }
    if let Some(now) = req.get("clock_now").and_then(Value::as_str) {
        let unix = parse_rfc3339(now)
            .ok_or_else(|| Fail::Harness(format!("clock_now {now:?} is not RFC 3339")))?;
        builder = builder.clock(Arc::new(FixedClock(unix)));
    }
    // Absent ⇒ the client default (0: retries are off).
    if let Some(n) = req.get("max_retries").and_then(Value::as_u64) {
        builder = builder.max_retries(n as u32);
    }
    Ok(builder.build()?)
}

fn run_http(runtime: &tokio::runtime::Runtime, req: &Value) -> Value {
    let client = match build_client(req) {
        Ok(c) => c,
        Err(Fail::Client(e)) => {
            return json!({
                "ok": false,
                "error": error_payload(&e, None),
                "rate_limit": rate_limit_payload(e.rate_limit()),
            })
        }
        Err(Fail::Harness(m)) => return harness_error(m),
    };
    let op = match req.get("op").and_then(Value::as_str) {
        Some(o) => o.to_string(),
        None => return harness_error("http request has no op"),
    };
    let spec = query_spec(req);
    let with_clock = req.get("clock_now").is_some();
    let fail = |e: &Error| {
        json!({
            "ok": false,
            "error": error_payload_at(e, None, with_clock),
            "rate_limit": rate_limit_payload(e.rate_limit()),
        })
    };

    // The network map takes its own options, not a peer query.
    if op == "network_map_graph" || op == "network_map_summary" {
        let opts = match build_network_map_opts(&spec) {
            Ok(o) => o,
            Err(Fail::Client(e)) => return fail(&e),
            Err(Fail::Harness(m)) => return harness_error(m),
        };
        let outcome = if op == "network_map_graph" {
            runtime.block_on(client.network_map_graph(&opts)).map(|r| {
                let g = &r.body;
                json!({
                    "ok": true,
                    "status": r.status,
                    "peer_ids": g.nodes.iter().map(|n| n.id.clone()).collect::<Vec<String>>(),
                    "edges": g.edges.iter().map(|e| json!([e.from, e.to, e.span])).collect::<Vec<Value>>(),
                    "tiers": g.tiers.iter().map(|t| json!([t.tier, t.count])).collect::<Vec<Value>>(),
                    "node_count": g.node_count,
                    "edge_count": g.edge_count,
                    "unreached": g.unreached,
                    "truncated": g.truncated,
                    "node_cities": g.nodes.iter().map(|n| n.city.clone()).collect::<Vec<Option<String>>>(),
                    "node_versions": g.nodes.iter().map(|n| n.version.clone()).collect::<Vec<Option<String>>>(),
                    "cities": cities(&g.cities),
                    "attribution": g.attribution,
                    "operator_nodes_count": g.operator_nodes.count,
                    "envelope": json!({
                        "version": g.version,
                        "available": g.available,
                        "stale": g.stale,
                        "network": g.network,
                        "generated_at": g.generated_at,
                    }),
                    "api_version": r.api_version,
                    "rate_limit": rate_limit_payload(Some(&r.rate_limit)),
                    "etag": r.etag,
                    "from_cache": r.from_cache,
                })
            })
        } else {
            runtime.block_on(client.network_map_summary(&opts)).map(|r| {
                let g = &r.body;
                json!({
                    "ok": true,
                    "status": r.status,
                    "tiers": g.tiers.iter().map(|t| json!([t.tier, t.count])).collect::<Vec<Value>>(),
                    "node_count": g.node_count,
                    "edge_count": g.edge_count,
                    "unreached": g.unreached,
                    "operator_nodes_count": g.operator_nodes.count,
                    "connectable": g.connectable,
                    "serves_mempool": g.serves_mempool,
                    "auction": g.auction,
                    "cities": cities(&g.cities),
                    "attribution": g.attribution,
                    "envelope": json!({
                        "version": g.version,
                        "available": g.available,
                        "stale": g.stale,
                        "network": g.network,
                        "generated_at": g.generated_at,
                    }),
                    "api_version": r.api_version,
                    "rate_limit": rate_limit_payload(Some(&r.rate_limit)),
                    "etag": r.etag,
                    "from_cache": r.from_cache,
                })
            })
        };
        return match outcome {
            Ok(v) => v,
            Err(e) => fail(&e),
        };
    }

    let query = match build_query(&spec) {
        Ok(q) => q,
        Err(Fail::Client(e)) => return fail(&e),
        Err(Fail::Harness(m)) => return harness_error(m),
    };

    let outcome: Result<Value, Error> = match op.as_str() {
        "get_peer" => {
            let ip = match req.get("ip").and_then(Value::as_str) {
                Some(ip) => ip.to_string(),
                None => return harness_error("get_peer has no ip"),
            };
            let opts = match query.network {
                Some(n) => GetPeerOpts::new().network(n),
                None => GetPeerOpts::new(),
            };
            runtime.block_on(client.get_peer_with(&ip, &opts)).map(|r| {
                json!({
                    "ok": true,
                    "status": r.status,
                    "peer_ids": [r.body.peer.id.clone()],
                    "signals": signals(std::slice::from_ref(&r.body.peer)),
                    "rows": rows(std::slice::from_ref(&r.body.peer)),
                    "envelope": envelope_payload(&r.body.envelope),
                    "api_version": r.api_version,
                    "rate_limit": rate_limit_payload(Some(&r.rate_limit)),
                    "etag": r.etag,
                    "from_cache": r.from_cache,
                })
            })
        }
        "facets" => {
            let arg = if spec.is_empty() { None } else { Some(&query) };
            runtime.block_on(client.facets(arg)).map(|r| {
                let mut facets = Map::new();
                // BTreeMap: already sorted by facet name.
                for (name, facet) in &r.body.facets {
                    facets.insert(
                        name.clone(),
                        json!({
                            "available": facet.available,
                            "values": facet.values.iter()
                                .map(|v| json!([v.value.clone(), v.count]))
                                .collect::<Vec<Value>>(),
                        }),
                    );
                }
                json!({
                    "ok": true,
                    "status": r.status,
                    "envelope": envelope_payload(&r.body.envelope),
                    "total": r.body.total,
                    "facets": Value::Object(facets),
                    "api_version": r.api_version,
                    "rate_limit": rate_limit_payload(Some(&r.rate_limit)),
                    "etag": r.etag,
                    "from_cache": r.from_cache,
                })
            })
        }
        "list_peers_as_gossip_config" => runtime
            .block_on(client.list_peers_as_gossip_config(&query))
            .map(|r| {
                json!({
                    "ok": true,
                    "status": r.status,
                    "json_text": r.body.to_json(),
                    "api_version": r.api_version,
                    "rate_limit": rate_limit_payload(Some(&r.rate_limit)),
                    "etag": r.etag,
                    "from_cache": r.from_cache,
                })
            }),
        "list_peers" | "list_peers_twice" => runtime
            .block_on(async {
                let first = client.list_peers(&query).await;
                if op == "list_peers_twice" {
                    // The same request twice: the second exercises the ETag cache.
                    first?;
                    client.list_peers(&query).await
                } else {
                    first
                }
            })
            .map(|r| {
                json!({
                    "ok": true,
                    "status": r.status,
                    "peer_ids": r.body.peers.iter().map(|p| p.id.clone()).collect::<Vec<String>>(),
                    "signals": signals(&r.body.peers),
                    "rows": rows(&r.body.peers),
                    "count": r.body.count,
                    "total": r.body.total,
                    "next_cursor": r.body.next_cursor,
                    "envelope": envelope_payload(&r.body.envelope),
                    "api_version": r.api_version,
                    "rate_limit": rate_limit_payload(Some(&r.rate_limit)),
                    "etag": r.etag,
                    "from_cache": r.from_cache,
                })
            }),
        "iter_peers" => {
            let max_items = req
                .get("max_items")
                .and_then(Value::as_u64)
                .unwrap_or(10_000) as usize;
            let opts = IterOpts::default().with_max_items(max_items);
            runtime.block_on(async {
                let mut stream = client.iter_peers(&query, opts);
                let mut ids: Vec<String> = Vec::new();
                while let Some(item) = stream.next().await {
                    ids.push(item?.id);
                }
                Ok(json!({"ok": true, "peer_ids": ids, "truncated": stream.truncated()}))
            })
        }
        other => return harness_error(format!("unknown op {other}")),
    };

    match outcome {
        Ok(v) => v,
        Err(e) => fail(&e),
    }
}

// ---------------------------------------------------------------------------
// the stream
// ---------------------------------------------------------------------------

fn handle(runtime: &tokio::runtime::Runtime, req: &Value) -> Value {
    match req.get("kind").and_then(Value::as_str) {
        Some("gossip") => run_gossip(req),
        Some("query_url") => run_query_url(req),
        Some("http") => run_http(runtime, req),
        Some(other) => harness_error(format!("unknown kind {other}")),
        None => harness_error("request has no kind"),
    }
}

fn main() {
    // 🔴 The harness must never read the operator's real environment — the
    // Python and TypeScript harnesses pass an empty env source, and Rust
    // resolves `HL_PEER_API_*` inside `ClientBuilder::build`. Clear them before
    // anything can build a client.
    std::env::remove_var("HL_PEER_API_BASE_URL");
    std::env::remove_var("HL_PEER_API_KEY");

    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("cross-check: could not build a runtime: {e}");
            std::process::exit(1);
        }
    };

    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(e) => {
                eprintln!("cross-check: reading stdin: {e}");
                break;
            }
        };
        if line.trim().is_empty() {
            continue;
        }
        let req: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(e) => {
                let answer = json!({
                    "id": Value::Null,
                    "ok": false,
                    "harness_error": format!("undecodable request line: {e}"),
                });
                let _ = writeln!(out, "{answer}");
                let _ = out.flush();
                continue;
            }
        };
        // One bad request must never kill the stream.
        let mut answer = match std::panic::catch_unwind(AssertUnwindSafe(|| handle(&runtime, &req)))
        {
            Ok(v) => v,
            Err(payload) => {
                let what = payload
                    .downcast_ref::<&str>()
                    .map(|s| (*s).to_string())
                    .or_else(|| payload.downcast_ref::<String>().cloned())
                    .unwrap_or_else(|| "a panic with no message".to_string());
                harness_error(format!("panic: {what}"))
            }
        };
        if !answer.is_object() {
            answer = harness_error("the handler did not produce an object");
        }
        if let Some(map) = answer.as_object_mut() {
            map.insert("id".into(), req.get("id").cloned().unwrap_or(Value::Null));
        }
        let _ = writeln!(out, "{answer}");
        let _ = out.flush();
    }
}

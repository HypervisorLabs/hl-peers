//! ONE generic runner over `../fixtures/gossip/cases/*.json` (§9(1)).
//!
//! Not one test per case: one interpreter, driven by the case format in
//! `fixtures/README.md`. Every case must pass byte-for-byte.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use hl_peers::{Candidate, Error, GossipConfig, MergeMode, MergeOpts, SaveOpts};
use serde_json::{json, Map, Value};

fn cases_dir() -> Option<PathBuf> {
    // ../fixtures lives outside the package root, so a packaged .crate has no
    // copy of it. Say so rather than passing vacuously.
    let d = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()?
        .join("fixtures/gossip/cases");
    d.is_dir().then_some(d)
}

#[derive(Debug, Default)]
struct Outcome {
    value: Option<Value>,
    diff: Option<String>,
}

fn args(op: &Value) -> Vec<Value> {
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

fn as_str(v: &Value) -> String {
    v.as_str().unwrap_or_default().to_string()
}

fn subst(s: &str, tmp: &Path) -> String {
    s.replace("$TMPDIR", &tmp.display().to_string())
}

fn merge_opts(o: &Map<String, Value>) -> Result<MergeOpts, Error> {
    let mut m = MergeOpts::default();
    if let Some(v) = o.get("max_roots").and_then(Value::as_u64) {
        m = m.with_max_roots(v as usize);
    }
    if let Some(v) = o.get("mode").and_then(Value::as_str) {
        m = m.with_mode(MergeMode::parse(v)?);
    }
    if let Some(v) = o.get("dedupe").and_then(Value::as_bool) {
        m = m.with_dedupe(v);
    }
    if let Some(v) = o.get("keep_existing").and_then(Value::as_array) {
        m = m.with_keep_existing(v.iter().map(as_str));
    }
    if let Some(v) = o.get("exclude").and_then(Value::as_array) {
        m = m.with_exclude(v.iter().map(as_str));
    }
    if let Some(v) = o.get("chain").and_then(Value::as_str) {
        m = m.with_chain(v);
    }
    if let Some(v) = o.get("try_new_peers").and_then(Value::as_bool) {
        m = m.with_try_new_peers(v);
    }
    Ok(m)
}

fn candidates(v: &Value) -> Vec<Candidate> {
    v.as_array()
        .map(|a| a.iter().map(Candidate::from).collect())
        .unwrap_or_default()
}

/// Apply one operation to the subject.
fn apply(subject: &mut Option<GossipConfig>, op: &Value, tmp: &Path) -> Result<Outcome, Error> {
    let name = op["op"].as_str().expect("every operation has an `op`");
    let a = args(op);
    let o = opts_of(op);
    let mut out = Outcome::default();

    // The two constructors may run without a subject.
    match name {
        "new" => {
            let chain = as_str(&a[0]);
            let ips: Vec<Value> = a
                .get(1)
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            *subject = Some(GossipConfig::new(&chain, ips.iter()));
            return Ok(out);
        }
        "from_peers" => {
            let (cfg, _w) = GossipConfig::from_peers(candidates(&a[0]), &merge_opts(&o)?)?;
            *subject = Some(cfg);
            return Ok(out);
        }
        _ => {}
    }

    let cfg = subject
        .as_mut()
        .unwrap_or_else(|| panic!("operation `{name}` needs a subject"));

    match name {
        "to_json" => out.value = Some(Value::String(cfg.to_json())),
        "validate" => {
            cfg.validate()?;
            out.value = Some(Value::String("ok".into()));
        }
        "root_ips" => out.value = Some(json!(cfg.root_ips())),
        "reserved_peer_ips" => out.value = Some(json!(cfg.reserved_peer_ips())),
        "has_root_ip" => out.value = Some(Value::Bool(cfg.has_root_ip(&as_str(&a[0])))),
        "add_root_ip" => {
            cfg.add_root_ip(&as_str(&a[0]));
        }
        "remove_root_ip" => {
            cfg.remove_root_ip(&as_str(&a[0]));
        }
        "set_root_ips" => {
            cfg.set_root_ips(a[0].as_array().unwrap().iter().map(as_str));
        }
        "add_reserved_peer_ip" => {
            cfg.add_reserved_peer_ip(&as_str(&a[0]));
        }
        "remove_reserved_peer_ip" => {
            cfg.remove_reserved_peer_ip(&as_str(&a[0]));
        }
        "set_reserved_peer_ips" => {
            cfg.set_reserved_peer_ips(a[0].as_array().unwrap().iter().map(as_str));
        }
        "set_chain" => {
            cfg.set_chain(&as_str(&a[0]));
        }
        "set_try_new_peers" => {
            cfg.set_try_new_peers(a[0].as_bool().unwrap());
        }
        "set_n_gossip_peers" => {
            cfg.set_n_gossip_peers(a[0].as_i64().unwrap())?;
        }
        "set_split_client_blocks" => {
            cfg.set_split_client_blocks(a[0].as_bool().unwrap());
        }
        "unset_split_client_blocks" => {
            cfg.unset_split_client_blocks();
        }
        "merge_peers" => {
            cfg.merge_peers(candidates(&a[0]), &merge_opts(&o)?)?;
        }
        "save" => {
            let path = subst(&as_str(&a[0]), tmp);
            let mut so = SaveOpts::default();
            if o.get("dry_run").and_then(Value::as_bool) == Some(true) {
                so = so.with_dry_run(true);
            }
            if o.get("backup").and_then(Value::as_bool) == Some(true) {
                so = so.with_backup(true);
            }
            if let Some(m) = o.get("file_mode").and_then(Value::as_u64) {
                so = so.with_file_mode(m as u32);
            }
            let r = cfg.save(&path, &so)?;
            out.value = Some(json!({
                "written": r.written,
                "changed": r.changed,
                "path": r.path.display().to_string(),
            }));
            out.diff = Some(r.diff);
        }
        other => panic!("the runner does not implement operation `{other}`"),
    }
    Ok(out)
}

/// Subset match: only the keys present in `want` are compared.
fn value_matches(want: &Value, got: &Value) -> bool {
    match (want, got) {
        (Value::Object(w), Value::Object(g)) => w
            .iter()
            .all(|(k, v)| g.get(k).map(|gv| value_matches(v, gv)).unwrap_or(false)),
        _ => want == got,
    }
}

struct Failure {
    case: String,
    why: String,
}

fn run_case(path: &Path, tmp: &Path) -> Result<(), String> {
    let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    let case: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    let expect = &case["expect"];

    // 1. Build the subject.
    let mut subject: Option<GossipConfig> = None;
    let mut error: Option<(isize, Error)> = None;

    if let Some(t) = case.get("input_json_text").and_then(Value::as_str) {
        match GossipConfig::parse(t) {
            Ok(c) => subject = Some(c),
            Err(e) => error = Some((-1, e)),
        }
    } else if let Some(c) = case.get("input_config") {
        if !c.is_null() {
            // `parse(render(input_config))` and "construct directly" are
            // equivalent per fixtures/README; go through parse.
            subject = Some(
                GossipConfig::parse(&c.to_string())
                    .map_err(|e| format!("input_config did not parse: {e}"))?,
            );
        }
    }

    // 2. Apply the operations in order.
    let ops = case["operations"].as_array().cloned().unwrap_or_default();
    let mut last = Outcome::default();
    if error.is_none() {
        for (i, op) in ops.iter().enumerate() {
            match apply(&mut subject, op, tmp) {
                Ok(o) => last = o,
                Err(e) => {
                    error = Some((i as isize, e));
                    break;
                }
            }
        }
    }

    // 3. Compare.
    if let Some(want) = expect.get("error") {
        let Some((at, e)) = &error else {
            return Err(format!(
                "expected {} but nothing raised",
                want["kind"].as_str().unwrap_or("an error")
            ));
        };
        let kind = want["kind"].as_str().unwrap_or_default();
        if e.kind() != kind {
            return Err(format!("expected kind {kind}, got {} ({e})", e.kind()));
        }
        if let Some(p) = want.get("parameter").and_then(Value::as_str) {
            if e.parameter() != Some(p) {
                return Err(format!("expected parameter {p}, got {:?}", e.parameter()));
            }
        }
        if let Some(n) = want.get("at_op").and_then(Value::as_i64) {
            if *at != n as isize {
                return Err(format!("expected the error at op {n}, got it at {at}"));
            }
        }
        if let Some(sub) = want.get("reason_contains").and_then(Value::as_str) {
            let reason = e.reason().unwrap_or_default().to_lowercase();
            if !reason.contains(&sub.to_lowercase()) {
                return Err(format!("reason {reason:?} does not contain {sub:?}"));
            }
        }
    } else if let Some((at, e)) = &error {
        return Err(format!("unexpected error at op {at}: {e} ({})", e.kind()));
    }

    if let Some(want) = expect.get("json_text").and_then(Value::as_str) {
        let got = subject
            .as_ref()
            .ok_or("expected json_text but there is no subject")?
            .to_json();
        if got != want {
            return Err(format!(
                "json_text mismatch\n want: {want:?}\n  got: {got:?}"
            ));
        }
    }

    if let Some(want) = expect.get("value") {
        let got = last
            .value
            .clone()
            .ok_or("expected a value but the last operation returned none")?;
        if !value_matches(want, &got) {
            return Err(format!("value mismatch: want {want}, got {got}"));
        }
    }

    // fixtures/README: after the last operation the runner calls validate() and
    // compares the resulting warning SET — §6.4's state-derived warnings unioned
    // with any pending merge warnings (§6.5), de-duplicated by code.
    // It is required on every non-error case.
    if expect.get("error").is_none() {
        let Some(want) = expect.get("warnings").and_then(Value::as_array) else {
            return Err("a non-error case must assert `warnings` (use [] for none)".to_string());
        };
        let want: BTreeSet<String> = want.iter().map(as_str).collect();
        let cfg = subject
            .as_ref()
            .ok_or("expected warnings but there is no subject")?;
        let got: BTreeSet<String> = cfg
            .validate()
            .map_err(|e| format!("validate() failed while collecting warnings: {e}"))?
            .iter()
            .map(|w| w.code.as_str().to_string())
            .collect();
        if want != got {
            return Err(format!("warnings mismatch: want {want:?}, got {got:?}"));
        }
    } else if expect.get("warnings").is_some() {
        return Err("an error case must not assert warnings".to_string());
    }

    if let Some(want) = expect.get("diff_contains").and_then(Value::as_array) {
        let diff = last.diff.clone().unwrap_or_default();
        for needle in want {
            let needle = as_str(needle);
            if !diff.contains(&needle) {
                return Err(format!("diff does not contain {needle:?}:\n{diff}"));
            }
        }
    }

    if let Some(want) = expect.get("file_after") {
        let p = subst(&as_str(&want["path"]), tmp);
        let got = std::fs::read_to_string(&p).map_err(|e| format!("{p}: {e}"))?;
        let expected = as_str(&want["json_text"]);
        if got != expected {
            return Err(format!(
                "file_after mismatch at {p}\n want: {expected:?}\n  got: {got:?}"
            ));
        }
    }

    if let Some(want) = expect.get("file_absent").and_then(Value::as_str) {
        let p = subst(want, tmp);
        if Path::new(&p).exists() {
            return Err(format!("{p} exists but file_absent was asserted"));
        }
    }

    Ok(())
}

#[test]
fn every_gossip_conformance_case_passes() {
    let Some(dir) = cases_dir() else {
        eprintln!(
            "SKIP: ../fixtures/gossip/cases is not present. The conformance suite lives outside \
             the package root, so a packaged .crate cannot run it; run the tests from the \
             monorepo checkout."
        );
        return;
    };

    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .expect("reading the case directory")
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no cases found in {}", dir.display());

    let root = std::env::temp_dir().join(format!("hl-peers-cases-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);

    let mut failures: Vec<Failure> = Vec::new();
    let mut passed = 0usize;

    for file in &files {
        let name = file.file_stem().unwrap().to_string_lossy().to_string();
        let tmp = root.join(&name);
        std::fs::create_dir_all(&tmp).expect("creating the case temp dir");

        match run_case(file, &tmp) {
            Ok(()) => passed += 1,
            Err(why) => failures.push(Failure { case: name, why }),
        }
    }

    let _ = std::fs::remove_dir_all(&root);

    eprintln!(
        "gossip conformance: {passed}/{} passed, {} failure(s)",
        files.len(),
        failures.len()
    );
    if !failures.is_empty() {
        let mut msg = String::new();
        for f in &failures {
            msg.push_str(&format!("\n--- {}\n{}\n", f.case, f.why));
        }
        panic!("{} conformance case(s) failed:{msg}", failures.len());
    }
}

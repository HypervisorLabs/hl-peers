//! Filesystem behaviour of `save()` — `CLIENT_SPEC.md` §6.6.

use std::path::PathBuf;
use std::sync::Arc;

use hl_peers::{FixedClock, GossipConfig, SaveOpts, WarningCode};

/// 2026-09-21T02:35:00Z, the instant the HTTP fixtures use.
const FIXED_NOW: i64 = 1_789_958_100;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let p = std::env::temp_dir().join(format!(
            "hl-peers-save-{}-{tag}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        TempDir(p)
    }
    fn join(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    fn entries(&self) -> Vec<String> {
        let mut v: Vec<String> = std::fs::read_dir(&self.0)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        v.sort();
        v
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn minimal() -> GossipConfig {
    let mut c = GossipConfig::new("Mainnet", ["192.0.2.10"]);
    c.set_try_new_peers(false);
    c
}

#[test]
fn a_new_file_is_written_atomically_and_leaves_no_temp_behind() {
    let dir = TempDir::new("atomic");
    let path = dir.join("override_gossip_config.json");
    let r = minimal().save(&path, &SaveOpts::default()).unwrap();
    assert!(r.written && r.changed);
    assert_eq!(dir.entries(), vec!["override_gossip_config.json"]);
    assert_eq!(std::fs::read_to_string(&path).unwrap(), minimal().to_json());
}

#[cfg(unix)]
#[test]
fn file_mode_names_a_new_file_s_permissions() {
    use std::os::unix::fs::PermissionsExt;
    let dir = TempDir::new("filemode");
    let path = dir.join("override_gossip_config.json");
    // 🔴 `file_mode`, not `mode`: `mode` is the merge strategy of §6.5.
    minimal()
        .save(&path, &SaveOpts::default().with_file_mode(0o640))
        .unwrap();
    let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o640);
}

#[test]
fn a_merge_warning_survives_to_save_and_is_only_cleared_explicitly() {
    let dir = TempDir::new("pending");
    let path = dir.join("override_gossip_config.json");

    let mut cfg = GossipConfig::new("Mainnet", ["192.0.2.10"]);
    cfg.set_try_new_peers(true);
    let candidates: Vec<String> = (1..=12).map(|i| format!("203.0.113.{i}")).collect();
    let returned = cfg
        .merge_peers(candidates.iter(), &hl_peers::MergeOpts::default())
        .unwrap();
    assert_eq!(returned[0].code, WarningCode::Truncated);

    // §6.5: validate() and save() re-report it, so a caller who ignored the
    // merge's return value cannot lose it.
    let res = cfg.save(&path, &SaveOpts::default()).unwrap();
    assert!(res
        .warnings
        .iter()
        .any(|w| w.code == WarningCode::Truncated));
    // …and a successful save does NOT clear it.
    assert!(cfg
        .validate()
        .unwrap()
        .iter()
        .any(|w| w.code == WarningCode::Truncated));
    cfg.clear_warnings();
    assert!(cfg
        .validate()
        .unwrap()
        .iter()
        .all(|w| w.code != WarningCode::Truncated));
}

#[cfg(unix)]
#[test]
fn a_new_file_is_private_and_an_existing_mode_is_preserved() {
    use std::os::unix::fs::PermissionsExt;
    let dir = TempDir::new("mode");
    let path = dir.join("override_gossip_config.json");

    minimal().save(&path, &SaveOpts::default()).unwrap();
    let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o600, "default-private: this file names peers");

    // Widen it, change the content, and check the mode survives the rename.
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
    let mut cfg = minimal();
    cfg.add_root_ip("192.0.2.11");
    cfg.save(&path, &SaveOpts::default()).unwrap();
    let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o644, "an existing mode is preserved, never widened");
}

#[test]
fn saving_identical_bytes_is_a_complete_no_op() {
    let dir = TempDir::new("noop");
    let path = dir.join("override_gossip_config.json");
    minimal().save(&path, &SaveOpts::default()).unwrap();
    let before = std::fs::metadata(&path).unwrap().modified().unwrap();

    let r = minimal()
        .save(&path, &SaveOpts::default().with_backup(true))
        .unwrap();
    assert!(!r.written && !r.changed);
    assert_eq!(r.diff, "");
    assert!(r.backup_path.is_none(), "a no-op creates no .bak");
    assert_eq!(dir.entries(), vec!["override_gossip_config.json"]);
    assert_eq!(
        std::fs::metadata(&path).unwrap().modified().unwrap(),
        before
    );
}

#[test]
fn a_backup_uses_the_injected_clock() {
    let dir = TempDir::new("backup");
    let path = dir.join("override_gossip_config.json");
    minimal().save(&path, &SaveOpts::default()).unwrap();

    let mut cfg = minimal();
    cfg.add_root_ip("192.0.2.11");
    let r = cfg
        .save(
            &path,
            &SaveOpts::default()
                .with_backup(true)
                .with_clock(Arc::new(FixedClock(FIXED_NOW))),
        )
        .unwrap();
    let bak = r.backup_path.unwrap();
    assert!(bak.to_string_lossy().ends_with(".20260921T023500Z.bak"));
    assert_eq!(std::fs::read_to_string(&bak).unwrap(), minimal().to_json());
    assert_eq!(std::fs::read_to_string(&path).unwrap(), cfg.to_json());
}

#[test]
fn a_dry_run_writes_nothing_and_diffs_against_an_empty_file_when_absent() {
    let dir = TempDir::new("dry");
    let path = dir.join("override_gossip_config.json");
    let r = minimal()
        .save(&path, &SaveOpts::default().with_dry_run(true))
        .unwrap();
    assert!(!r.written && r.changed);
    assert!(r.diff.contains("192.0.2.10"));
    assert!(r.diff.starts_with("--- "));
    assert!(!path.exists());
    assert!(dir.entries().is_empty());
}

#[test]
fn an_invalid_config_never_reaches_the_filesystem() {
    let dir = TempDir::new("invalid");
    let path = dir.join("override_gossip_config.json");
    let mut cfg = minimal();
    cfg.set_root_ips(Vec::<String>::new());
    let e = cfg.save(&path, &SaveOpts::default()).unwrap_err();
    assert_eq!(e.parameter(), Some("root_node_ips"));
    assert!(!path.exists());
    assert!(dir.entries().is_empty());
}

#[test]
fn load_refuses_to_invent_an_empty_config_for_a_missing_file() {
    let dir = TempDir::new("load");
    let path = dir.join("nope.json");
    let e = GossipConfig::load(&path).unwrap_err();
    assert_eq!(e.kind(), "NotFound");
    // …and load_or_new is the explicit opt-in to that shape.
    let cfg = GossipConfig::load_or_new(&path, "Mainnet").unwrap();
    assert!(cfg.root_ips().is_empty());
    assert!(cfg.validate().is_err(), "still the crash-loop shape");
}

#[test]
fn the_live_minimal_form_round_trips_byte_equivalently() {
    let dir = TempDir::new("roundtrip");
    let path = dir.join("override_gossip_config.json");
    let text = "{\n  \"chain\": \"Mainnet\",\n  \"root_node_ips\": [\n    {\n      \"Ip\": \"192.0.2.10\"\n    }\n  ],\n  \"try_new_peers\": false\n}\n";
    std::fs::write(&path, text).unwrap();
    let r = GossipConfig::load(&path)
        .unwrap()
        .save(&path, &SaveOpts::default())
        .unwrap();
    assert!(!r.written, "identical bytes: no write at all");
    assert_eq!(std::fs::read_to_string(&path).unwrap(), text);
}

// ---------------------------------------------------------------------------
// 🔴 symlinks — CLIENT_SPEC §6.6
// ---------------------------------------------------------------------------

#[test]
#[cfg(unix)]
fn save_follows_a_symlink_and_replaces_its_target() {
    // Renaming over the link would swap the operator's arrangement for a plain
    // file, after which the node and their tooling read two different files.
    let d = TempDir::new("symlink");
    std::fs::create_dir_all(d.join("managed")).unwrap();
    let real = d.join("managed").join("gossip.json");
    std::fs::write(&real, "{\"chain\": \"Mainnet\", \"root_node_ips\": []}\n").unwrap();
    let link = d.join("override_gossip_config.json");
    std::os::unix::fs::symlink(&real, &link).unwrap();

    let cfg = minimal();
    let res = cfg.save(&link, &SaveOpts::default()).unwrap();

    assert!(
        std::fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink(),
        "the link itself must survive the write"
    );
    assert_eq!(std::fs::read_to_string(&real).unwrap(), cfg.to_json());
    assert_eq!(res.path, link);
    assert_eq!(res.resolved_path, real);
}

#[test]
#[cfg(unix)]
fn save_preserves_the_target_mode_through_a_symlink() {
    use std::os::unix::fs::PermissionsExt;
    let d = TempDir::new("symlink-mode");
    let real = d.join("gossip.json");
    std::fs::write(&real, "{}\n").unwrap();
    std::fs::set_permissions(&real, std::fs::Permissions::from_mode(0o640)).unwrap();
    let link = d.join("override_gossip_config.json");
    std::os::unix::fs::symlink(&real, &link).unwrap();

    minimal().save(&link, &SaveOpts::default()).unwrap();

    let mode = std::fs::metadata(&real).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o640);
}

#[test]
#[cfg(unix)]
fn save_writes_through_a_dangling_symlink() {
    let d = TempDir::new("symlink-dangling");
    let real = d.join("not-yet-there.json");
    let link = d.join("override_gossip_config.json");
    std::os::unix::fs::symlink(&real, &link).unwrap();

    let cfg = minimal();
    cfg.save(&link, &SaveOpts::default()).unwrap();

    assert_eq!(std::fs::read_to_string(&real).unwrap(), cfg.to_json());
    assert!(std::fs::symlink_metadata(&link)
        .unwrap()
        .file_type()
        .is_symlink());
}

#[test]
#[cfg(unix)]
fn save_refuses_a_symlink_loop() {
    let d = TempDir::new("symlink-loop");
    let a = d.join("a.json");
    let b = d.join("b.json");
    std::os::unix::fs::symlink(&b, &a).unwrap();
    std::os::unix::fs::symlink(&a, &b).unwrap();

    let err = minimal().save(&a, &SaveOpts::default()).unwrap_err();
    assert_eq!(err.kind(), "Invalid");
    assert_eq!(err.parameter(), Some("path"));
}

#[test]
#[cfg(unix)]
fn save_refuses_a_symlink_into_a_missing_directory() {
    let d = TempDir::new("symlink-nodir");
    let link = d.join("override_gossip_config.json");
    std::os::unix::fs::symlink(d.join("nowhere").join("gossip.json"), &link).unwrap();

    let err = minimal().save(&link, &SaveOpts::default()).unwrap_err();
    assert_eq!(err.kind(), "Invalid");
    assert_eq!(err.parameter(), Some("path"));
}

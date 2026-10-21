//! `save(path, opts)` — `CLIENT_SPEC.md` §6.6.
//!
//! 🔴 `save` never restarts, signals, or reloads anything. hl-node reads
//! `override_gossip_config.json` **when the node starts**: writing it
//! does not steer a running node, and the change takes effect at the node's next start, which is the operator's to schedule. Restarting a node *to apply a
//! config* while it is behind can make a lagging box worse.
//!

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use super::diff;
use super::GossipConfig;
use crate::error::{Error, Result, Warning};
use crate::time::format_utc;

/// The injectable clock.
///
/// It is read in exactly two places: the `.bak` filename of §6.6, and §5.2's
/// resolution of a `Retry-After` given as an HTTP-date. Both are injectable so
/// that tests are deterministic and never depend on wall-clock time.
pub trait Clock: Send + Sync + std::fmt::Debug {
    /// Seconds since the Unix epoch.
    fn now_unix(&self) -> i64;

    /// `%Y%m%dT%H%M%SZ`, UTC. Derived from [`Clock::now_unix`].
    fn utc_timestamp(&self) -> String {
        format_utc(self.now_unix())
    }
}

/// The real clock.
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_unix(&self) -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0)
    }
}

/// A clock frozen at one instant, for tests.
#[derive(Debug, Clone, Copy)]
pub struct FixedClock(pub i64);

impl Clock for FixedClock {
    fn now_unix(&self) -> i64 {
        self.0
    }
}

/// Options for [`GossipConfig::save`].
///
/// `#[non_exhaustive]`; build with [`SaveOpts::default`] and the `with_*`
/// methods.
#[derive(Clone)]
#[non_exhaustive]
pub struct SaveOpts {
    /// Render the unified diff and write nothing.
    pub dry_run: bool,
    /// Copy the existing target to `<path>.<UTC %Y%m%dT%H%M%SZ>.bak` first.
    /// Default `false`.
    pub backup: bool,
    /// Permission bits for a **new** file. Default `0o600` — this file names
    /// infrastructure you peer with, so default-private is the right posture.
    /// An existing file's mode is preserved and never widened.
    ///
    /// 🔴 It is `file_mode`, not `mode`: `mode` is the **merge strategy** of
    /// §6.5 and nothing else. The two would collide the moment §7 takes one
    /// options bag, and a caller who passed `0o600` expecting permissions would
    /// silently get an invalid merge strategy.
    pub file_mode: Option<u32>,
    /// The clock used for the `.bak` name.
    pub clock: std::sync::Arc<dyn Clock>,
}

impl std::fmt::Debug for SaveOpts {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SaveOpts")
            .field("dry_run", &self.dry_run)
            .field("backup", &self.backup)
            .field("file_mode", &self.file_mode)
            .finish_non_exhaustive()
    }
}

impl Default for SaveOpts {
    fn default() -> Self {
        SaveOpts {
            dry_run: false,
            backup: false,
            file_mode: None,
            clock: std::sync::Arc::new(SystemClock),
        }
    }
}

impl SaveOpts {
    /// Set [`SaveOpts::dry_run`].
    pub fn with_dry_run(mut self, v: bool) -> Self {
        self.dry_run = v;
        self
    }
    /// Set [`SaveOpts::backup`].
    pub fn with_backup(mut self, v: bool) -> Self {
        self.backup = v;
        self
    }
    /// Set [`SaveOpts::file_mode`] for a newly created file.
    pub fn with_file_mode(mut self, file_mode: u32) -> Self {
        self.file_mode = Some(file_mode);
        self
    }
    /// Inject a [`Clock`], so a `.bak` name is deterministic in a test.
    pub fn with_clock(mut self, clock: std::sync::Arc<dyn Clock>) -> Self {
        self.clock = clock;
        self
    }
}

/// What [`GossipConfig::save`] did.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct SaveResult {
    /// Were bytes written to the target?
    pub written: bool,
    /// Do the rendered bytes differ from what was on disk?
    pub changed: bool,
    /// The target, exactly as the caller named it — symlink and all.
    pub path: PathBuf,
    /// Where the bytes actually landed. It differs from [`SaveResult::path`]
    /// only when that path was a symlink, which `save` **follows** rather than
    /// replacing (§6.6).
    pub resolved_path: PathBuf,
    /// The unified diff. Empty unless there was a change.
    pub diff: String,
    /// Where the previous content was copied, if `backup` was set.
    pub backup_path: Option<PathBuf>,
    /// The warnings [`GossipConfig::validate`] produced. Never swallow these.
    pub warnings: Vec<Warning>,
}

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// How many links `save` will follow before it refuses (§6.6).
pub const SYMLINK_DEPTH: usize = 32;

/// Follow a symlinked config path to the file it names.
///
/// 🔴 `save` replaces the **target**, never the link: renaming over a symlink
/// swaps an operator's deliberate arrangement for a regular file, and from that
/// moment the node and their tooling read two different files with nothing to
/// announce it.
fn resolve_symlink(path: &Path) -> Result<PathBuf> {
    // The end of the walk: `current` is not a link (or is not there at all). A
    // dangling link is written through — but only into a directory that exists,
    // because creating one would be guessing at the operator's layout.
    let settle = |current: PathBuf| -> Result<PathBuf> {
        if current != path {
            let dir = current.parent().unwrap_or_else(|| Path::new("."));
            if !dir.is_dir() {
                return Err(Error::Invalid {
                    parameter: "path".into(),
                    reason: format!(
                        "{} is a symbolic link to {}, whose directory does not exist; refusing \
                         to create one rather than guess at the operator's layout",
                        path.display(),
                        current.display()
                    ),
                });
            }
        }
        Ok(current)
    };

    let mut current = path.to_path_buf();
    for _ in 0..SYMLINK_DEPTH {
        // 🪤 `symlink_metadata`, not `metadata`: a symlink loop's members all
        // exist as links, so the walk must run out of fuel below rather than be
        // mistaken for a dangling link here.
        let meta = match fs::symlink_metadata(&current) {
            Ok(m) => m,
            Err(_) => return settle(current),
        };
        if !meta.file_type().is_symlink() {
            return settle(current);
        }
        let target = fs::read_link(&current).map_err(|e| Error::Invalid {
            parameter: "path".into(),
            reason: format!("reading the symbolic link {}: {e}", current.display()),
        })?;
        current = if target.is_absolute() {
            target
        } else {
            current
                .parent()
                .unwrap_or_else(|| Path::new("."))
                .join(target)
        };
    }
    Err(Error::Invalid {
        parameter: "path".into(),
        reason: format!(
            "{} resolves through more than {SYMLINK_DEPTH} symbolic links; refusing to follow \
             further",
            path.display()
        ),
    })
}

impl GossipConfig {
    /// Validate, render, and atomically replace `path` (§6.6).
    ///
    /// In order: `validate()` (on error **nothing is written**); render; a
    /// byte-identical target is a **no-op** (no write, no `.bak`, no mtime
    /// change); `dry_run` returns a unified diff; optional `.bak`; write to
    /// `.<basename>.<n>.tmp` **in the same directory**, `fsync`, `rename`,
    /// `fsync` the directory. A temp file in `/tmp` would be wrong: a rename
    /// across filesystems is not atomic.
    ///
    /// 🔴 This never restarts, signals or reloads your node. hl-node reads the
    /// file when the node starts.
    ///
    /// ```no_run
    /// # use hl_peers::{GossipConfig, SaveOpts};
    /// let cfg = GossipConfig::new("Mainnet", ["192.0.2.10"]);
    /// let res = cfg.save("/tmp/override_gossip_config.json", &SaveOpts::default()).unwrap();
    /// println!("written={} changed={}", res.written, res.changed);
    /// ```
    pub fn save(&self, path: impl AsRef<Path>, opts: &SaveOpts) -> Result<SaveResult> {
        let path = path.as_ref();

        // (1) validate first: nothing reaches the filesystem otherwise.
        let warnings = self.validate()?;

        // (0) 🔴 follow a symlinked path to its target, so the atomic rename
        // replaces the file the operator pointed at rather than the link.
        let target = resolve_symlink(path)?;
        let target = target.as_path();

        // (2)
        let rendered = self.to_json();
        let existing: Option<String> = match fs::read(target) {
            Ok(bytes) => Some(String::from_utf8_lossy(&bytes).into_owned()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            Err(e) => {
                return Err(Error::Transport {
                    cause: format!("reading {}: {e}", target.display()),
                })
            }
        };

        // (3) identical: no write, no .bak, no mtime change.
        if existing.as_deref() == Some(rendered.as_str()) {
            return Ok(SaveResult {
                written: false,
                changed: false,
                path: path.to_path_buf(),
                resolved_path: target.to_path_buf(),
                diff: String::new(),
                backup_path: None,
                warnings,
            });
        }

        let label = path.display().to_string();
        let diff_text = diff::unified(existing.as_deref().unwrap_or(""), &rendered, &label, &label);

        // (4) dry run.
        if opts.dry_run {
            return Ok(SaveResult {
                written: false,
                changed: true,
                path: path.to_path_buf(),
                resolved_path: target.to_path_buf(),
                diff: diff_text,
                backup_path: None,
                warnings,
            });
        }

        // (5) backup.
        let mut backup_path = None;
        if opts.backup && existing.is_some() {
            let mut name = target.as_os_str().to_os_string();
            name.push(format!(".{}.bak", opts.clock.utc_timestamp()));
            let bak = PathBuf::from(name);
            fs::copy(target, &bak).map_err(|e| Error::Transport {
                cause: format!("writing {}: {e}", bak.display()),
            })?;
            #[cfg(unix)]
            if let Ok(meta) = fs::metadata(target) {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(
                    &bak,
                    fs::Permissions::from_mode(meta.permissions().mode() & 0o7777),
                );
            }
            backup_path = Some(bak);
        }

        // (6) temp file in the SAME directory, fsync, rename.
        let dir = target.parent().unwrap_or_else(|| Path::new("."));
        let base = target
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| "override_gossip_config.json".to_string());
        let unique = format!(
            "{}.{}",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
        );
        let tmp = dir.join(format!(".{base}.{unique}.tmp"));

        let write = || -> std::io::Result<()> {
            let mut f = fs::File::create(&tmp)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                // (7) preserve the target's mode; 0600 for a new file.
                let mode = match fs::metadata(target) {
                    Ok(meta) => meta.permissions().mode() & 0o7777,
                    Err(_) => opts.file_mode.unwrap_or(0o600),
                };
                f.set_permissions(fs::Permissions::from_mode(mode))?;
            }
            f.write_all(rendered.as_bytes())?;
            f.flush()?;
            f.sync_all()?;
            drop(f);
            fs::rename(&tmp, target)?;
            // fsync the directory so the rename itself is durable.
            #[cfg(unix)]
            if let Ok(d) = fs::File::open(dir) {
                let _ = d.sync_all();
            }
            Ok(())
        };

        // (8) on any failure the temp file goes, and the original is untouched
        // by construction.
        if let Err(e) = write() {
            let _ = fs::remove_file(&tmp);
            return Err(Error::Transport {
                cause: format!("writing {}: {e}", target.display()),
            });
        }

        Ok(SaveResult {
            written: true,
            changed: true,
            path: path.to_path_buf(),
            resolved_path: target.to_path_buf(),
            diff: diff_text,
            backup_path,
            warnings,
        })
    }
}

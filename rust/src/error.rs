//! The error taxonomy of `CLIENT_SPEC.md` §5.1, and the rate-limit block that
//! rides on every response and every error.

use std::fmt;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

/// An RFC 9457 `application/problem+json` body, as far as this API defines it.
///
/// Unknown members are kept in [`Problem::extra`]; nothing the server adds is
/// dropped. Discrimination is on [`Problem::type_`] only — never on `title` or
/// `detail`, because prose is not an API.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[non_exhaustive]
pub struct Problem {
    /// The stable problem identifier URI.
    #[serde(rename = "type", default)]
    pub type_: Option<String>,
    /// Short human title.
    #[serde(default)]
    pub title: Option<String>,
    /// The HTTP status restated in the body.
    #[serde(default)]
    pub status: Option<u16>,
    /// Human-readable detail. 🔴 Never parse numbers out of this (D-A).
    #[serde(default)]
    pub detail: Option<String>,
    /// The occurrence URI.
    #[serde(default)]
    pub instance: Option<String>,
    /// The query parameter at fault, when exactly one is.
    #[serde(default)]
    pub parameter: Option<String>,
    /// The permitted values, when the fault is a closed-enum violation.
    #[serde(default)]
    pub allowed: Option<Vec<String>>,
    /// Where to raise the limit. Present on `429`.
    #[serde(default)]
    pub signup_url: Option<String>,
    /// Every other member, preserved verbatim.
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

impl Problem {
    /// The last path segment of `type`, which is what §5.1 discriminates on.
    ///
    /// ```
    /// # use hl_peers::Problem;
    /// let p: Problem = serde_json::from_str(
    ///     r#"{"type":"https://example.invalid/problems/cursor-expired","title":"x","status":400}"#,
    /// )
    /// .unwrap();
    /// assert_eq!(p.type_suffix(), Some("cursor-expired"));
    /// ```
    pub fn type_suffix(&self) -> Option<&str> {
        let t = self.type_.as_deref()?;
        let t = t.split(['?', '#']).next().unwrap_or(t);
        t.rsplit('/').next().filter(|s| !s.is_empty())
    }
}

/// The parsed `RateLimit-*` / `Retry-After` block (§3.3).
///
/// Every member is nullable. A missing or unparseable header is [`None`],
/// **never** `0`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct RateLimit {
    /// `RateLimit-Limit`: quota units in the expiring window.
    pub limit: Option<u64>,
    /// `RateLimit-Remaining`: quota units left in the current window.
    pub remaining: Option<u64>,
    /// `RateLimit-Reset`: **seconds from now**, not an epoch.
    pub reset: Option<u64>,
    /// `RateLimit-Policy`: the raw string, unparsed.
    pub policy: Option<String>,
    /// `Retry-After`, **resolved to a delay in seconds** (§5.2): an all-digits
    /// value as-is, an IMF-fixdate as `max(0, date - now)` against the client's
    /// injectable clock, and [`None`] for anything that parses as neither.
    pub retry_after: Option<u64>,
    /// The raw `Retry-After` header, for diagnostics. Kept even when it could
    /// not be resolved, so a caller can see what the server actually sent.
    pub retry_after_raw: Option<String>,
}

impl RateLimit {
    /// Parse the block out of header name/value pairs, resolving an HTTP-date
    /// `Retry-After` against wall-clock time.
    ///
    /// Header names are matched case-insensitively. Prefer
    /// [`RateLimit::from_headers_at`] wherever determinism matters; the client
    /// always uses its injectable clock.
    ///
    /// ```
    /// # use hl_peers::RateLimit;
    /// let rl = RateLimit::from_headers([
    ///     ("RateLimit-Limit".to_string(), "20".to_string()),
    ///     ("Retry-After".to_string(), "soon-ish".to_string()),
    /// ]);
    /// assert_eq!(rl.limit, Some(20));
    /// // 🔴 unparseable is ABSENT — never 0, and never a failed request.
    /// assert_eq!(rl.retry_after, None);
    /// assert_eq!(rl.retry_after_raw.as_deref(), Some("soon-ish"));
    /// ```
    pub fn from_headers<I>(headers: I) -> Self
    where
        I: IntoIterator<Item = (String, String)>,
    {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        RateLimit::from_headers_at(headers, now)
    }

    /// [`RateLimit::from_headers`] with the instant to resolve an HTTP-date
    /// `Retry-After` against (§5.2).
    ///
    /// 1. an all-digits value is a delay in seconds;
    /// 2. otherwise it is parsed as an IMF-fixdate and converted with
    ///    `max(0, date - now)`;
    /// 3. a date in the past yields `0` — retry immediately, not "absent";
    /// 4. a value that parses as neither is treated as **absent**: do not
    ///    guess, and do not fail the request over a bad header.
    ///
    /// ```
    /// # use hl_peers::RateLimit;
    /// // 2026-09-21T02:35:00Z
    /// let now = 1_789_958_100;
    /// let rl = RateLimit::from_headers_at(
    ///     [("Retry-After".to_string(), "Mon, 21 Sep 2026 02:36:00 GMT".to_string())],
    ///     now,
    /// );
    /// assert_eq!(rl.retry_after, Some(60));
    /// ```
    pub fn from_headers_at<I>(headers: I, now_unix: i64) -> Self
    where
        I: IntoIterator<Item = (String, String)>,
    {
        let mut rl = RateLimit::default();
        for (name, value) in headers {
            let lower = name.to_ascii_lowercase();
            match lower.as_str() {
                "ratelimit-limit" => rl.limit = value.trim().parse().ok(),
                "ratelimit-remaining" => rl.remaining = value.trim().parse().ok(),
                "ratelimit-reset" => rl.reset = value.trim().parse().ok(),
                "ratelimit-policy" => rl.policy = Some(value),
                "retry-after" => {
                    let raw = value.trim().to_string();
                    rl.retry_after = if !raw.is_empty() && raw.chars().all(|c| c.is_ascii_digit()) {
                        raw.parse().ok()
                    } else {
                        crate::time::parse_imf_fixdate(&raw).map(|at| (at - now_unix).max(0) as u64)
                    };
                    rl.retry_after_raw = Some(raw);
                }
                _ => {}
            }
        }
        rl
    }
}

/// A non-fatal finding from [`crate::GossipConfig::validate`], [`crate::GossipConfig::save`]
/// or a merge (§6.4).
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Warning {
    /// The stable identifier the conformance suite asserts on.
    pub code: WarningCode,
    /// Prose. Never asserted on, and free to change.
    pub message: String,
    /// Optional extra context.
    pub detail: Option<String>,
}

impl Warning {
    pub(crate) fn new(code: WarningCode, message: impl Into<String>) -> Self {
        Warning {
            code,
            message: message.into(),
            detail: None,
        }
    }

    pub(crate) fn with_detail(mut self, detail: impl Into<String>) -> Self {
        self.detail = Some(detail.into());
        self
    }
}

impl fmt::Display for Warning {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)?;
        if let Some(d) = &self.detail {
            write!(f, " ({d})")?;
        }
        Ok(())
    }
}

/// The closed set of warning codes (§6.4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub enum WarningCode {
    /// `root_node_ips` length > 24.
    ManyRootsGt24,
    /// `root_node_ips` length > 64. Fires *instead of* [`WarningCode::ManyRootsGt24`].
    ManyRootsGt64,
    /// The `chain` key is missing.
    ChainAbsent,
    /// `try_new_peers == false` with more than one root.
    TryNewPeersFalseMultiRoot,
    /// A merge dropped candidates at `max_roots`.
    Truncated,
    /// An IP appeared in both `exclude` and `keep_existing`; `exclude` won.
    ExcludeOverridesKeepExisting,
}

impl WarningCode {
    /// The wire spelling used by the conformance fixtures.
    pub fn as_str(&self) -> &'static str {
        match self {
            WarningCode::ManyRootsGt24 => "many_roots_gt_24",
            WarningCode::ManyRootsGt64 => "many_roots_gt_64",
            WarningCode::ChainAbsent => "chain_absent",
            WarningCode::TryNewPeersFalseMultiRoot => "try_new_peers_false_multi_root",
            WarningCode::Truncated => "truncated",
            WarningCode::ExcludeOverridesKeepExisting => "exclude_overrides_keep_existing",
        }
    }
}

impl fmt::Display for WarningCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The result type of every fallible operation in this crate.
pub type Result<T> = std::result::Result<T, Error>;

/// Every way this crate can fail — the closed taxonomy of `CLIENT_SPEC.md` §5.1.
///
/// [`Error::FilterUnavailable`], [`Error::ChainNotServed`] and
/// [`Error::CursorExpired`] are members of the **400 family**: a caller that
/// handles [`Error::BadRequest`] generically should use [`Error::is_bad_request`]
/// so it catches all four. Rust has no subtyping, so that predicate is the
/// discriminator §5.1 asks for.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum Error {
    /// `429`. See D-I: a `Retry-After` above the cap raises instead of sleeping.
    #[error("rate limited{}", rate_limited_suffix(*retry_after, *reset_at))]
    RateLimited {
        /// `Retry-After`, seconds.
        retry_after: Option<u64>,
        /// When to try again: an absolute UTC instant from the client's
        /// injectable clock at response time — `now + retry_after` when
        /// `Retry-After` is known, else `now + RateLimit-Reset`, else [`None`].
        /// Sleep until this; nothing needs to parse a header.
        reset_at: Option<std::time::SystemTime>,
        /// `RateLimit-Limit`.
        limit: Option<u64>,
        /// `RateLimit-Remaining`.
        remaining: Option<u64>,
        /// `RateLimit-Reset`, seconds from now.
        reset: Option<u64>,
        /// `RateLimit-Policy`, raw.
        policy: Option<String>,
        /// Where to raise the limit, from the problem body ([`None`] if absent).
        signup_url: Option<String>,
        /// The problem body, when the response carried one.
        problem: Option<Box<Problem>>,
        /// The full parsed rate-limit block.
        rate_limit: RateLimit,
        /// The `API-Version` response header (informational; never validated).
        api_version: Option<String>,
    },

    /// `400` whose problem `type` ends `/filter-unavailable`: the attribute is
    /// **not collected**. Never swallow this into an empty result list.
    #[error("filter unavailable: {}", filter.as_deref().unwrap_or("<unnamed>"))]
    FilterUnavailable {
        /// The parameter at fault (`problem.parameter`).
        filter: Option<String>,
        /// The server's human detail.
        detail: Option<String>,
        /// The problem body.
        problem: Option<Box<Problem>>,
        /// The parsed rate-limit block.
        rate_limit: RateLimit,
        /// The `API-Version` response header (informational; never validated).
        api_version: Option<String>,
    },

    /// `400` … `/chain-not-served`.
    #[error("chain not served (allowed: {})", allowed.join(", "))]
    ChainNotServed {
        /// The chains this deployment does serve.
        allowed: Vec<String>,
        /// The problem body.
        problem: Option<Box<Problem>>,
        /// The parsed rate-limit block.
        rate_limit: RateLimit,
        /// The `API-Version` response header (informational; never validated).
        api_version: Option<String>,
    },

    /// `400` … `/cursor-expired`. D-H: surface it, never auto-restart the walk.
    #[error("cursor expired: the cursor was issued in an earlier graph generation")]
    CursorExpired {
        /// The problem body.
        problem: Option<Box<Problem>>,
        /// The parsed rate-limit block.
        rate_limit: RateLimit,
        /// The `API-Version` response header (informational; never validated).
        api_version: Option<String>,
    },

    /// Any other `400`.
    #[error("bad request{}", parameter.as_deref().map(|p| format!(" ({p})")).unwrap_or_default())]
    BadRequest {
        /// The parameter at fault, when the server named one.
        parameter: Option<String>,
        /// The permitted values, when the fault is a closed-enum violation.
        allowed: Option<Vec<String>>,
        /// The server's human detail.
        detail: Option<String>,
        /// The problem body.
        problem: Option<Box<Problem>>,
        /// The parsed rate-limit block.
        rate_limit: RateLimit,
        /// The `API-Version` response header (informational; never validated).
        api_version: Option<String>,
    },

    /// `401`. Omitting a key is *not* a 401 — anonymous is a supported tier.
    #[error("unauthorized: {}", detail.as_deref().unwrap_or("the supplied key was not accepted"))]
    Unauthorized {
        /// The server's human detail.
        detail: Option<String>,
        /// The problem body.
        problem: Option<Box<Problem>>,
        /// The parsed rate-limit block.
        rate_limit: RateLimit,
        /// The `API-Version` response header (informational; never validated).
        api_version: Option<String>,
    },

    /// `404` from the API, or a missing file from [`crate::GossipConfig::load`].
    #[error("not found{}", path.as_ref().map(|p| format!(": {}", p.display())).unwrap_or_default())]
    NotFound {
        /// The server's human detail, for an API 404.
        detail: Option<String>,
        /// The path that did not exist, for a `load` failure.
        path: Option<PathBuf>,
        /// The problem body.
        problem: Option<Box<Problem>>,
        /// The parsed rate-limit block.
        rate_limit: RateLimit,
        /// The `API-Version` response header (informational; never validated).
        api_version: Option<String>,
    },

    /// `503`, **and** an `available: false` envelope where a config would
    /// otherwise have been built (D-G).
    #[error("unavailable: {}", detail.as_deref().unwrap_or("the peer graph producer could not answer"))]
    Unavailable {
        /// The server's human detail.
        detail: Option<String>,
        /// The problem body.
        problem: Option<Box<Problem>>,
        /// The parsed rate-limit block.
        rate_limit: RateLimit,
        /// The `API-Version` response header (informational; never validated).
        api_version: Option<String>,
    },

    /// Any other `5xx`.
    #[error("server error {status}: {body_excerpt}")]
    Server {
        /// The HTTP status.
        status: u16,
        /// At most 512 bytes of body, truncated on a UTF-8 boundary.
        body_excerpt: String,
        /// The parsed rate-limit block.
        rate_limit: RateLimit,
        /// The `API-Version` response header (informational; never validated).
        api_version: Option<String>,
    },

    /// Connect/read timeout, DNS, TLS, connection reset.
    #[error("transport error: {cause}")]
    Transport {
        /// What the transport reported.
        cause: String,
    },

    /// The body is not JSON, or does not match the schema.
    #[error("decode error: {cause}")]
    Decode {
        /// What the decoder reported.
        cause: String,
        /// At most 512 bytes of body, truncated on a UTF-8 boundary.
        body_excerpt: String,
    },

    /// Client-side validation (§2.2, §4.3, §4.4, §6.4). Nothing was sent, and
    /// for a `save`, nothing was written.
    #[error("invalid {parameter}: {reason}")]
    Invalid {
        /// The parameter, field or JSON pointer at fault.
        parameter: String,
        /// Why it was refused.
        reason: String,
    },

    /// `iter_peers` hit `max_items` while `on_truncate` was
    /// [`crate::OnTruncate::Raise`].
    #[error("truncated after {seen} peers (max_items reached)")]
    Truncated {
        /// How many peers were yielded before the guard fired.
        seen: usize,
    },
}

impl Error {
    /// Build an [`Error::Invalid`]. Exposed because a caller building queries
    /// on top of this crate wants the same variant.
    pub fn invalid(parameter: impl Into<String>, reason: impl Into<String>) -> Error {
        Error::Invalid {
            parameter: parameter.into(),
            reason: reason.into(),
        }
    }

    /// True for every member of the 400 family: [`Error::BadRequest`],
    /// [`Error::FilterUnavailable`], [`Error::ChainNotServed`] and
    /// [`Error::CursorExpired`].
    ///
    /// ```
    /// # use hl_peers::Error;
    /// let e = Error::CursorExpired {
    ///     problem: None,
    ///     rate_limit: Default::default(),
    ///     api_version: None,
    /// };
    /// assert!(e.is_bad_request());
    /// ```
    pub fn is_bad_request(&self) -> bool {
        matches!(
            self,
            Error::BadRequest { .. }
                | Error::FilterUnavailable { .. }
                | Error::ChainNotServed { .. }
                | Error::CursorExpired { .. }
        )
    }

    /// The variant name, matching the `error.kind` of the conformance fixtures.
    pub fn kind(&self) -> &'static str {
        match self {
            Error::RateLimited { .. } => "RateLimited",
            Error::FilterUnavailable { .. } => "FilterUnavailable",
            Error::ChainNotServed { .. } => "ChainNotServed",
            Error::CursorExpired { .. } => "CursorExpired",
            Error::BadRequest { .. } => "BadRequest",
            Error::Unauthorized { .. } => "Unauthorized",
            Error::NotFound { .. } => "NotFound",
            Error::Unavailable { .. } => "Unavailable",
            Error::Server { .. } => "Server",
            Error::Transport { .. } => "Transport",
            Error::Decode { .. } => "Decode",
            Error::Invalid { .. } => "Invalid",
            Error::Truncated { .. } => "Truncated",
        }
    }

    /// The parameter this error names, if any. `Invalid` always has one.
    pub fn parameter(&self) -> Option<&str> {
        match self {
            Error::Invalid { parameter, .. } => Some(parameter),
            Error::BadRequest { parameter, .. } => parameter.as_deref(),
            Error::FilterUnavailable { filter, .. } => filter.as_deref(),
            // 🪤 `ChainNotServed` and `CursorExpired` are constructors over the
            // 400 family (CLIENT_SPEC §5.1), so a caller that catches
            // `BadRequest` generically must still learn which parameter the
            // server named. Neither variant stores it separately; it comes from
            // the problem document, which is where the other three read it too.
            Error::ChainNotServed { problem, .. } | Error::CursorExpired { problem, .. } => {
                problem.as_ref().and_then(|p| p.parameter.as_deref())
            }
            _ => None,
        }
    }

    /// The human reason/detail, for a caller that wants to print one line.
    pub fn reason(&self) -> Option<&str> {
        match self {
            Error::Invalid { reason, .. } => Some(reason),
            Error::BadRequest { detail, .. }
            | Error::FilterUnavailable { detail, .. }
            | Error::Unauthorized { detail, .. }
            | Error::NotFound { detail, .. }
            | Error::Unavailable { detail, .. } => detail.as_deref(),
            Error::Decode { cause, .. } | Error::Transport { cause } => Some(cause),
            _ => None,
        }
    }

    /// The rate-limit block, when this error came from a response.
    pub fn rate_limit(&self) -> Option<&RateLimit> {
        match self {
            Error::RateLimited { rate_limit, .. }
            | Error::FilterUnavailable { rate_limit, .. }
            | Error::ChainNotServed { rate_limit, .. }
            | Error::CursorExpired { rate_limit, .. }
            | Error::BadRequest { rate_limit, .. }
            | Error::Unauthorized { rate_limit, .. }
            | Error::NotFound { rate_limit, .. }
            | Error::Unavailable { rate_limit, .. }
            | Error::Server { rate_limit, .. } => Some(rate_limit),
            _ => None,
        }
    }

    /// The `API-Version` header of the response this error came from.
    ///
    /// Informational: the server sends `1` today and a breaking change ships
    /// under `/v2` paths, so a different value is never an error.
    pub fn api_version(&self) -> Option<&str> {
        match self {
            Error::RateLimited { api_version, .. }
            | Error::FilterUnavailable { api_version, .. }
            | Error::ChainNotServed { api_version, .. }
            | Error::CursorExpired { api_version, .. }
            | Error::BadRequest { api_version, .. }
            | Error::Unauthorized { api_version, .. }
            | Error::NotFound { api_version, .. }
            | Error::Unavailable { api_version, .. }
            | Error::Server { api_version, .. } => api_version.as_deref(),
            _ => None,
        }
    }

    /// The problem body, when the response carried `application/problem+json`.
    pub fn problem(&self) -> Option<&Problem> {
        match self {
            Error::RateLimited { problem, .. }
            | Error::FilterUnavailable { problem, .. }
            | Error::ChainNotServed { problem, .. }
            | Error::CursorExpired { problem, .. }
            | Error::BadRequest { problem, .. }
            | Error::Unauthorized { problem, .. }
            | Error::NotFound { problem, .. }
            | Error::Unavailable { problem, .. } => problem.as_deref(),
            _ => None,
        }
    }

    /// The process exit code §8 assigns to this error.
    pub fn exit_code(&self) -> i32 {
        match self {
            Error::Invalid { .. }
            | Error::BadRequest { .. }
            | Error::FilterUnavailable { .. }
            | Error::ChainNotServed { .. }
            | Error::CursorExpired { .. }
            | Error::Decode { .. }
            | Error::Truncated { .. } => 1,
            Error::RateLimited { .. } => 3,
            Error::Unauthorized { .. } => 4,
            Error::NotFound { .. } => 5,
            Error::Unavailable { .. } | Error::Server { .. } | Error::Transport { .. } => 6,
        }
    }
}

/// ` (retry after 42s, at 2026-09-21T02:36:00Z)` — the parts that are known.
fn rate_limited_suffix(
    retry_after: Option<u64>,
    reset_at: Option<std::time::SystemTime>,
) -> String {
    let at = reset_at
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| crate::time::format_rfc3339(d.as_secs() as i64));
    match (retry_after, at) {
        (Some(s), Some(at)) => format!(" (retry after {s}s, at {at})"),
        (Some(s), None) => format!(" (retry after {s}s)"),
        (None, Some(at)) => format!(" (window resets at {at})"),
        (None, None) => String::new(),
    }
}

/// Truncate a body to at most `max` bytes, on a UTF-8 boundary (§5.1).
pub(crate) fn body_excerpt(bytes: &[u8], max: usize) -> String {
    let text = String::from_utf8_lossy(bytes);
    if text.len() <= max {
        return text.into_owned();
    }
    let mut end = max;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn excerpt_stops_on_a_char_boundary() {
        let s = "ä".repeat(400); // 800 bytes
        let cut = body_excerpt(s.as_bytes(), 512);
        assert!(cut.len() <= 512);
        assert!(std::str::from_utf8(cut.as_bytes()).is_ok());
    }

    #[test]
    fn type_suffix_ignores_query_and_fragment() {
        let p = Problem {
            type_: Some("https://x.invalid/problems/rate-limited?v=1".into()),
            ..Default::default()
        };
        assert_eq!(p.type_suffix(), Some("rate-limited"));
    }
}

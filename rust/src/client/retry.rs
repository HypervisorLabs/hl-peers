//! Retry and backoff — `CLIENT_SPEC.md` §5.2.
//!
//! Retried: `429` (per `Retry-After`, capped), `5xx` including `503`, and
//! transport faults. 🔴 **Never** a `4xx` other than `429`: a `400`, `401` or
//! `404` is deterministic, and retrying it burns quota to receive the same
//! answer.
//!
//! The sleeper and the jitter source are injectable, so a test can assert on
//! the *decisions* — which statuses retry, how many attempts, that
//! `Retry-After` was read — and never on wall-clock delays.

use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use super::transport::BoxFuture;

/// Where a delay actually happens.
pub trait Sleeper: Send + Sync + fmt::Debug {
    /// Sleep for `duration`.
    fn sleep(&self, duration: Duration) -> BoxFuture<'static, ()>;
}

/// The real sleeper.
#[derive(Debug, Clone, Copy, Default)]
pub struct TokioSleeper;

impl Sleeper for TokioSleeper {
    fn sleep(&self, duration: Duration) -> BoxFuture<'static, ()> {
        Box::pin(tokio::time::sleep(duration))
    }
}

/// A sleeper that returns immediately and records what it was asked for.
///
/// Use it in tests: the backoff *decisions* are assertable, the delays are not.
#[derive(Debug, Default)]
pub struct RecordingSleeper {
    slept: std::sync::Mutex<Vec<Duration>>,
}

impl RecordingSleeper {
    /// Every duration this sleeper was asked for, in order.
    pub fn recorded(&self) -> Vec<Duration> {
        self.slept.lock().map(|g| g.clone()).unwrap_or_default()
    }
}

impl Sleeper for RecordingSleeper {
    fn sleep(&self, duration: Duration) -> BoxFuture<'static, ()> {
        if let Ok(mut g) = self.slept.lock() {
            g.push(duration);
        }
        Box::pin(std::future::ready(()))
    }
}

/// The multiplicative jitter factor, in `[0.5, 1.0]`.
pub trait Jitter: Send + Sync + fmt::Debug {
    /// A factor in `[0.5, 1.0]`.
    fn factor(&self) -> f64;
}

/// Full-width jitter in the lower half, so two clients started together
/// diverge. A tiny xorshift, seeded per instance — the randomness source is
/// not required to match across languages and conformance tests must not
/// assert on delays.
#[derive(Debug)]
pub struct DefaultJitter {
    state: AtomicU64,
}

impl Default for DefaultJitter {
    fn default() -> Self {
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0x2545_F491_4F6C_DD1D)
            ^ (std::process::id() as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
        DefaultJitter {
            state: AtomicU64::new(seed | 1),
        }
    }
}

impl Jitter for DefaultJitter {
    fn factor(&self) -> f64 {
        let mut x = self.state.load(Ordering::Relaxed);
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.state.store(x, Ordering::Relaxed);
        0.5 + (x >> 11) as f64 / (1u64 << 53) as f64 / 2.0
    }
}

/// A jitter source that always returns 1.0, for deterministic tests.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoJitter;

impl Jitter for NoJitter {
    fn factor(&self) -> f64 {
        1.0
    }
}

/// `delay(n) = min(base * 2^n, max_delay) * jitter`, `n` zero-based.
pub(crate) fn backoff(
    n: u32,
    base: Duration,
    max_delay: Duration,
    jitter: &dyn Jitter,
) -> Duration {
    let raw = base.saturating_mul(2u32.saturating_pow(n.min(16)));
    let capped = raw.min(max_delay);
    capped.mul_f64(jitter.factor())
}

/// What the client decided to do about one response.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Decision {
    /// Retry after this many milliseconds.
    RetryAfter(u64),
    /// Give up and raise.
    Raise,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_doubles_and_is_capped() {
        let b = Duration::from_millis(500);
        let max = Duration::from_secs(30);
        assert_eq!(backoff(0, b, max, &NoJitter), Duration::from_millis(500));
        assert_eq!(backoff(1, b, max, &NoJitter), Duration::from_millis(1000));
        assert_eq!(backoff(9, b, max, &NoJitter), max);
    }

    #[test]
    fn jitter_stays_in_the_lower_half() {
        let j = DefaultJitter::default();
        for _ in 0..1000 {
            let f = j.factor();
            assert!((0.5..=1.0).contains(&f), "{f}");
        }
    }
}

//! The ETag cache — `CLIENT_SPEC.md` §4.5.
//!
//! Per-client-instance, in memory, never global and never on disk. It exists
//! to make polling free — a `304` costs no quota unit — not to serve stale
//! bytes, so `Cache-Control: max-age` is deliberately **not** honoured as a
//! hard TTL and the client always revalidates.

use std::collections::HashMap;
use std::sync::Mutex;

/// One cached body plus its validator.
#[derive(Debug, Clone)]
pub(crate) struct CacheEntry {
    pub etag: String,
    pub body: Vec<u8>,
    #[allow(dead_code)]
    pub stored_at: std::time::Instant,
}

/// A small LRU keyed per §4.5.
#[derive(Debug)]
pub(crate) struct EtagCache {
    capacity: usize,
    inner: Mutex<Inner>,
}

#[derive(Debug, Default)]
struct Inner {
    map: HashMap<String, CacheEntry>,
    /// Least-recently-used first.
    order: Vec<String>,
}

impl EtagCache {
    pub fn new(capacity: usize) -> Self {
        EtagCache {
            capacity,
            inner: Mutex::new(Inner::default()),
        }
    }

    /// `method \0 path \0 canonical_query \0 (k|a)`.
    ///
    /// The key includes whether a key was sent, because the tier changes the
    /// page ceiling and therefore the body.
    pub fn key(method: &str, path: &str, query: &str, api_key_present: bool) -> String {
        format!(
            "{method}\0{path}\0{query}\0{}",
            if api_key_present { "k" } else { "a" }
        )
    }

    pub fn get(&self, key: &str) -> Option<CacheEntry> {
        let mut g = self.inner.lock().ok()?;
        let entry = g.map.get(key).cloned()?;
        g.order.retain(|k| k != key);
        g.order.push(key.to_string());
        Some(entry)
    }

    pub fn put(&self, key: &str, etag: String, body: Vec<u8>) {
        let Ok(mut g) = self.inner.lock() else { return };
        g.order.retain(|k| k != key);
        g.map.insert(
            key.to_string(),
            CacheEntry {
                etag,
                body,
                stored_at: std::time::Instant::now(),
            },
        );
        g.order.push(key.to_string());
        while g.order.len() > self.capacity {
            let evicted = g.order.remove(0);
            g.map.remove(&evicted);
        }
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.inner.lock().map(|g| g.map.len()).unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn evicts_the_least_recently_used() {
        let c = EtagCache::new(2);
        c.put("a", "1".into(), b"a".to_vec());
        c.put("b", "2".into(), b"b".to_vec());
        let _ = c.get("a"); // a becomes most recent
        c.put("c", "3".into(), b"c".to_vec());
        assert_eq!(c.len(), 2);
        assert!(c.get("b").is_none());
        assert!(c.get("a").is_some());
    }

    #[test]
    fn the_key_separates_anonymous_from_keyed() {
        assert_ne!(
            EtagCache::key("GET", "/p", "tier=1", true),
            EtagCache::key("GET", "/p", "tier=1", false)
        );
    }
}

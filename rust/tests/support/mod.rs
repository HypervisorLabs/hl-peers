//! Test transports. No network in the default test run (§9(5)).
#![allow(dead_code)]
#![cfg(feature = "client")]

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use hl_peers::{BoxFuture, HttpRequest, HttpResponse, Result, Transport};
use serde_json::Value;

pub fn fixtures_dir() -> Option<PathBuf> {
    let d = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()?
        .join("fixtures/http");
    d.is_dir().then_some(d)
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(if b[i] == b'+' { b' ' } else { b[i] });
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// path plus a *set* of decoded query pairs, which is what the mock server and
/// the fixture format both match on.
fn split_url(url: &str) -> (String, Vec<(String, String)>) {
    let without_scheme = url.splitn(4, '/').nth(3).unwrap_or("");
    let full = format!("/{without_scheme}");
    let (path, q) = match full.split_once('?') {
        Some((p, q)) => (p.to_string(), q),
        None => (full, ""),
    };
    let mut pairs: Vec<(String, String)> = q
        .split('&')
        .filter(|s| !s.is_empty())
        .map(|kv| match kv.split_once('=') {
            Some((k, v)) => (percent_decode(k), percent_decode(v)),
            None => (percent_decode(kv), String::new()),
        })
        .collect();
    pairs.sort();
    (path, pairs)
}

fn fixture_pairs(fx: &Value) -> Vec<(String, String)> {
    let mut v: Vec<(String, String)> = fx["request"]["query"]
        .as_object()
        .map(|m| {
            m.iter()
                .map(|(k, val)| {
                    (
                        k.clone(),
                        val.as_str()
                            .map(str::to_string)
                            .unwrap_or_else(|| val.to_string()),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    v.sort();
    v
}

/// Replays `fixtures/http/*.json` through the [`Transport`] seam, matching the
/// same way `tools/mock_server.py` does.
#[derive(Debug)]
pub struct FixtureTransport {
    fixtures: Vec<Value>,
    scenario: Option<String>,
    pub requests: Mutex<Vec<HttpRequest>>,
}

impl FixtureTransport {
    pub fn new() -> Self {
        FixtureTransport::with_scenario(None)
    }

    pub fn with_scenario(scenario: Option<&str>) -> Self {
        let dir = fixtures_dir().expect("../fixtures/http must be present to run these tests");
        let mut fixtures = Vec::new();
        let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
            .unwrap()
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.extension().is_some_and(|x| x == "json"))
            .collect();
        files.sort();
        for f in files {
            fixtures.push(serde_json::from_str(&std::fs::read_to_string(f).unwrap()).unwrap());
        }
        FixtureTransport {
            fixtures,
            scenario: scenario.map(str::to_string),
            requests: Mutex::new(Vec::new()),
        }
    }

    pub fn request_count(&self) -> usize {
        self.requests.lock().unwrap().len()
    }

    pub fn last_url(&self) -> Option<String> {
        self.requests.lock().unwrap().last().map(|r| r.url.clone())
    }

    fn header_of(req: &HttpRequest, name: &str) -> Option<String> {
        req.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.clone())
    }

    fn resolve(&self, req: &HttpRequest) -> HttpResponse {
        let (path, pairs) = split_url(&req.url);
        let mut exact = Vec::new();
        let mut loose = Vec::new();
        for fx in &self.fixtures {
            if fx["request"]["method"].as_str() != Some(req.method) {
                continue;
            }
            if fx["request"]["path"].as_str() != Some(path.as_str()) {
                continue;
            }
            if fixture_pairs(fx) != pairs {
                continue;
            }
            let want = fx["request"]["headers"]
                .as_object()
                .cloned()
                .unwrap_or_default();
            if want.is_empty() {
                loose.push(fx);
                continue;
            }
            let all = want.iter().all(|(k, v)| {
                let want_value = v.as_str().unwrap_or_default();
                if k.eq_ignore_ascii_case("X-Mock-Scenario") {
                    self.scenario.as_deref() == Some(want_value)
                } else {
                    FixtureTransport::header_of(req, k).as_deref() == Some(want_value)
                }
            });
            if all {
                exact.push(fx);
            }
        }
        let Some(fx) = exact.into_iter().chain(loose).next() else {
            return HttpResponse::new(
                404,
                vec![("Content-Type".into(), "application/problem+json".into())],
                format!(
                    r#"{{"type":"https://example.invalid/problems/no-fixture","title":"No fixture matches this request","status":404,"detail":"{} {}"}}"#,
                    req.method, req.url
                )
                .into_bytes(),
            );
        };

        let resp = &fx["response"];
        let mut headers: Vec<(String, String)> = resp["headers"]
            .as_object()
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap_or_default().to_string()))
                    .collect()
            })
            .unwrap_or_default();

        // The mock server synthesises a 304 for any fixture with an ETag when
        // a matching If-None-Match arrives. Do the same here.
        let inm = FixtureTransport::header_of(req, "If-None-Match");
        let etag = headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case("ETag"))
            .map(|(_, v)| v.clone());
        if resp["status"].as_u64() == Some(200) && inm.is_some() && inm == etag {
            // Like tools/mock_server.py, plus `API-Version`, which the real
            // server sends on every response, 304s included (CLIENT_SPEC §1).
            headers.retain(|(k, _)| {
                k.eq_ignore_ascii_case("ETag")
                    || k.eq_ignore_ascii_case("Cache-Control")
                    || k.eq_ignore_ascii_case("API-Version")
                    || k.to_ascii_lowercase().starts_with("ratelimit-")
            });
            return HttpResponse::new(304, headers, Vec::new());
        }

        let body = if let Some(text) = resp.get("body_text").and_then(Value::as_str) {
            text.as_bytes().to_vec()
        } else if resp["body"].is_null() {
            Vec::new()
        } else {
            resp["body"].to_string().into_bytes()
        };

        HttpResponse::new(resp["status"].as_u64().unwrap_or(200) as u16, headers, body)
    }
}

impl Transport for FixtureTransport {
    fn execute<'a>(&'a self, request: HttpRequest) -> BoxFuture<'a, Result<HttpResponse>> {
        let resp = self.resolve(&request);
        self.requests.lock().unwrap().push(request);
        Box::pin(async move { Ok(resp) })
    }
}

/// A transport that hands out a scripted sequence, so a retry test can assert
/// on the number of attempts rather than on any wall-clock delay.
#[derive(Debug)]
pub struct ScriptedTransport {
    script: Vec<std::result::Result<HttpResponse, String>>,
    calls: AtomicUsize,
}

impl ScriptedTransport {
    pub fn new(script: Vec<std::result::Result<HttpResponse, String>>) -> Self {
        ScriptedTransport {
            script,
            calls: AtomicUsize::new(0),
        }
    }

    pub fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl Transport for ScriptedTransport {
    fn execute<'a>(&'a self, _request: HttpRequest) -> BoxFuture<'a, Result<HttpResponse>> {
        let n = self.calls.fetch_add(1, Ordering::SeqCst);
        let item = self
            .script
            .get(n)
            .cloned()
            .unwrap_or_else(|| self.script.last().cloned().expect("a non-empty script"));
        Box::pin(async move { item.map_err(|cause| hl_peers::Error::Transport { cause }) })
    }
}

/// Every `fixtures/http` case, as raw JSON, for tests that assert on the
/// fixture's own `expect` block.
pub fn all_fixtures() -> Vec<Value> {
    let dir = fixtures_dir().expect("../fixtures/http must be present");
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap()
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    files
        .into_iter()
        .map(|f| serde_json::from_str(&std::fs::read_to_string(f).unwrap()).unwrap())
        .collect()
}

/// `2026-09-21T02:35:00Z` -> Unix seconds. Days-from-civil, no dependency.
pub fn parse_rfc3339_z(s: &str) -> i64 {
    let (date, time) = s.trim_end_matches('Z').split_once('T').unwrap();
    let d: Vec<i64> = date.split('-').map(|x| x.parse().unwrap()).collect();
    let t: Vec<i64> = time.split(':').map(|x| x.parse().unwrap()).collect();
    let (y, m, day) = (d[0], d[1], d[2]);
    let y2 = if m <= 2 { y - 1 } else { y };
    let era = if y2 >= 0 { y2 } else { y2 - 399 } / 400;
    let yoe = y2 - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    days * 86_400 + t[0] * 3600 + t[1] * 60 + t[2]
}

pub fn json_response(status: u16, body: &str, headers: &[(&str, &str)]) -> HttpResponse {
    HttpResponse::new(
        status,
        headers
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect(),
        body.as_bytes().to_vec(),
    )
}

/// One `fixtures/http` case by name.
pub fn fixture(name: &str) -> Value {
    let path = fixtures_dir()
        .expect("../fixtures/http must be present")
        .join(format!("{name}.json"));
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// A transport that answers every request with one fixture's response,
/// whatever was asked — for a response the typed client cannot request.
pub fn replay(name: &str) -> ScriptedTransport {
    let fx = fixture(name);
    let resp = &fx["response"];
    let headers: Vec<(String, String)> = resp["headers"]
        .as_object()
        .map(|m| {
            m.iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap_or_default().to_string()))
                .collect()
        })
        .unwrap_or_default();
    let body = if let Some(text) = resp.get("body_text").and_then(Value::as_str) {
        text.as_bytes().to_vec()
    } else if resp["body"].is_null() {
        Vec::new()
    } else {
        resp["body"].to_string().into_bytes()
    };
    ScriptedTransport::new(vec![Ok(HttpResponse::new(
        resp["status"].as_u64().unwrap_or(200) as u16,
        headers,
        body,
    ))])
}

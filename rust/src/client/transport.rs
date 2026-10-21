//! The transport seam.
//!
//! Every HTTP byte the client sends or receives goes through [`Transport`], so
//! a test can inject canned fixture responses and the default test run touches
//! no network at all (§9(5)).

use std::fmt;
use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use crate::error::{Error, Result};

/// A request, already fully serialized.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct HttpRequest {
    /// Always `GET` today. If a non-GET method is ever added, §5.2 says the
    /// retry policy must be re-evaluated, not inherited.
    pub method: &'static str,
    /// The absolute URL, query string included and canonically ordered.
    pub url: String,
    /// Request headers, in the order the client set them.
    pub headers: Vec<(String, String)>,
    /// The per-attempt timeout.
    pub timeout: Duration,
}

/// A response, as bytes plus headers.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct HttpResponse {
    /// The HTTP status.
    pub status: u16,
    /// Response headers. Names are compared case-insensitively by the client.
    pub headers: Vec<(String, String)>,
    /// The raw body. Empty on a `304`.
    pub body: Vec<u8>,
}

impl HttpResponse {
    /// Build one — for a [`Transport`] implementation outside this crate.
    pub fn new(status: u16, headers: Vec<(String, String)>, body: Vec<u8>) -> Self {
        HttpResponse {
            status,
            headers,
            body,
        }
    }

    /// Look a header up, case-insensitively.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

/// A boxed future, so [`Transport`] stays object-safe without `async_trait`.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Where requests go.
///
/// Implement it to replay fixtures, to route through a proxy, or to count
/// attempts in a retry test.
pub trait Transport: Send + Sync + fmt::Debug {
    /// Perform one attempt. A connect/read timeout, DNS, TLS or reset failure
    /// must be reported as [`Error::Transport`] so the retry policy can see it.
    fn execute<'a>(&'a self, request: HttpRequest) -> BoxFuture<'a, Result<HttpResponse>>;
}

/// The default transport: `reqwest` with rustls.
#[derive(Debug, Clone)]
pub struct ReqwestTransport {
    inner: reqwest::Client,
}

impl ReqwestTransport {
    /// Build one with the crate's defaults.
    pub fn new() -> Result<Self> {
        let inner = reqwest::Client::builder()
            .build()
            .map_err(|e| Error::Transport {
                cause: e.to_string(),
            })?;
        Ok(ReqwestTransport { inner })
    }

    /// Wrap a `reqwest::Client` the caller already configured.
    pub fn from_client(inner: reqwest::Client) -> Self {
        ReqwestTransport { inner }
    }
}

impl Transport for ReqwestTransport {
    fn execute<'a>(&'a self, request: HttpRequest) -> BoxFuture<'a, Result<HttpResponse>> {
        Box::pin(async move {
            let mut rb = self.inner.get(&request.url).timeout(request.timeout);
            for (k, v) in &request.headers {
                rb = rb.header(k, v);
            }
            let resp = rb.send().await.map_err(|e| Error::Transport {
                cause: e.to_string(),
            })?;
            let status = resp.status().as_u16();
            let headers = resp
                .headers()
                .iter()
                .map(|(k, v)| {
                    (
                        k.as_str().to_string(),
                        v.to_str().unwrap_or_default().to_string(),
                    )
                })
                .collect();
            let body = resp
                .bytes()
                .await
                .map_err(|e| Error::Transport {
                    cause: e.to_string(),
                })?
                .to_vec();
            Ok(HttpResponse {
                status,
                headers,
                body,
            })
        })
    }
}

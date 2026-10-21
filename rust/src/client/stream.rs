//! Cursor pagination as a [`futures_core::Stream`] — `CLIENT_SPEC.md` §4.2.

use std::collections::VecDeque;
use std::pin::Pin;
use std::task::{Context, Poll};

use futures_core::Stream;

use super::transport::BoxFuture;
use super::{Client, Conditional, Envelope, Peer, PeerList, PeerQuery, Response};
use crate::error::{Error, Result};

/// What to do when `max_items` is reached.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
#[non_exhaustive]
pub enum OnTruncate {
    /// End the stream silently and set [`PeerStream::truncated`]. The default.
    #[default]
    Stop,
    /// Yield [`Error::Truncated`] as the stream's last item.
    Raise,
}

/// Options for [`Client::iter_peers`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub struct IterOpts {
    /// Hard guard. **Default 10 000.** A client must not default to unbounded.
    pub max_items: usize,
    /// What reaching the guard does.
    pub on_truncate: OnTruncate,
}

impl Default for IterOpts {
    fn default() -> Self {
        IterOpts {
            max_items: 10_000,
            on_truncate: OnTruncate::Stop,
        }
    }
}

impl IterOpts {
    /// Set [`IterOpts::max_items`].
    pub fn with_max_items(mut self, n: usize) -> Self {
        self.max_items = n;
        self
    }

    /// Set [`IterOpts::on_truncate`].
    pub fn with_on_truncate(mut self, t: OnTruncate) -> Self {
        self.on_truncate = t;
        self
    }
}

type PageFuture = BoxFuture<'static, Result<Response<PeerList>>>;

/// A lazy walk over every page of a query.
///
/// The client never deduplicates across pages and never reorders results: the
/// API already ranked them.
pub struct PeerStream {
    client: Client,
    query: PeerQuery,
    opts: IterOpts,
    buffer: VecDeque<Peer>,
    seen: usize,
    cursor: Option<String>,
    first_request_done: bool,
    finished: bool,
    truncated: bool,
    raised_truncation: bool,
    pending: Option<PageFuture>,
    last_envelope: Option<Envelope>,
}

impl std::fmt::Debug for PeerStream {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PeerStream")
            .field("seen", &self.seen)
            .field("truncated", &self.truncated)
            .field("finished", &self.finished)
            .finish_non_exhaustive()
    }
}

impl PeerStream {
    pub(crate) fn new(client: Client, query: PeerQuery, opts: IterOpts) -> Self {
        let cursor = query.cursor.clone();
        PeerStream {
            client,
            query,
            opts,
            buffer: VecDeque::new(),
            seen: 0,
            cursor,
            first_request_done: false,
            finished: false,
            truncated: false,
            raised_truncation: false,
            pending: None,
            last_envelope: None,
        }
    }

    /// Did the walk stop because it hit `max_items`?
    pub fn truncated(&self) -> bool {
        self.truncated
    }

    /// How many peers have been yielded.
    pub fn seen(&self) -> usize {
        self.seen
    }

    /// The envelope of the most recent page.
    ///
    /// This is how a caller tells "the walk ended because the producer could
    /// not build a graph" (`available == false`) from "the walk ended because
    /// there were no more peers".
    pub fn envelope(&self) -> Option<&Envelope> {
        self.last_envelope.as_ref()
    }

    /// The next peer, or `None` at the end of the walk.
    pub async fn next(&mut self) -> Option<Result<Peer>> {
        std::future::poll_fn(|cx| Pin::new(&mut *self).poll_next(cx)).await
    }

    /// Drain the whole walk into a `Vec`, subject to the same `max_items`
    /// guard.
    pub async fn collect_all(mut self) -> Result<Vec<Peer>> {
        let mut out = Vec::new();
        while let Some(item) = self.next().await {
            out.push(item?);
        }
        Ok(out)
    }

    fn start_page(&mut self) {
        let client = self.client.clone();
        let query = self.query.with_cursor(self.cursor.clone());
        self.pending = Some(Box::pin(async move {
            let qs = query.to_query_string()?;
            client
                .get_json(super::PEERS_PATH, &qs, None)
                .await
                .map(|c| match c {
                    Conditional::Modified(r) => r,
                    Conditional::NotModified { .. } => {
                        unreachable!("no caller validator was sent")
                    }
                })
        }));
    }
}

impl Stream for PeerStream {
    type Item = Result<Peer>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let me = self.get_mut();
        loop {
            if me.seen >= me.opts.max_items {
                // Only a guard hit with more to come counts as truncation.
                let more = !me.buffer.is_empty() || me.cursor.is_some() || !me.first_request_done;
                if more {
                    me.truncated = true;
                    if me.opts.on_truncate == OnTruncate::Raise && !me.raised_truncation {
                        me.raised_truncation = true;
                        return Poll::Ready(Some(Err(Error::Truncated { seen: me.seen })));
                    }
                }
                return Poll::Ready(None);
            }

            if let Some(peer) = me.buffer.pop_front() {
                me.seen += 1;
                return Poll::Ready(Some(Ok(peer)));
            }

            if me.finished {
                return Poll::Ready(None);
            }

            if me.pending.is_none() {
                if me.first_request_done && me.cursor.is_none() {
                    me.finished = true;
                    return Poll::Ready(None);
                }
                me.start_page();
            }

            let fut = me.pending.as_mut().expect("just set");
            match fut.as_mut().poll(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Err(e)) => {
                    me.pending = None;
                    me.finished = true;
                    return Poll::Ready(Some(Err(e)));
                }
                Poll::Ready(Ok(page)) => {
                    me.pending = None;
                    me.first_request_done = true;
                    me.last_envelope = Some(page.body.envelope.clone());

                    // 🔴 available:false stops the walk immediately, without
                    // raising. The caller tells the difference from the flag.
                    if !page.body.envelope.available || page.body.peers.is_empty() {
                        me.finished = true;
                        me.cursor = None;
                        return Poll::Ready(None);
                    }
                    me.cursor = page.body.next_cursor.clone();
                    me.buffer.extend(page.body.peers);
                }
            }
        }
    }
}

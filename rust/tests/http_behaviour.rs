//! HTTP behaviour against `../fixtures/http`, through the transport seam
//! (§9(2), §9(3)). No network.
#![cfg(feature = "client")]

mod support;

use std::sync::Arc;
use std::time::Duration;

use hl_peers::{
    normalize_base_path, AuctionStatus, Client, Connectable, Error, FixedClock, Geo, IterOpts,
    NetworkMapOpts, OnTruncate, PeerQuery, RecordingSleeper, Sort, TierFilter, DEFAULT_BASE_PATH,
    DEFAULT_BASE_URL,
};
use support::{json_response, FixtureTransport, ScriptedTransport};

fn client_with(transport: Arc<dyn hl_peers::Transport>) -> Client {
    Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(transport)
        .build()
        .unwrap()
}

fn fixture_client() -> Client {
    client_with(Arc::new(FixtureTransport::new()))
}

fn scenario_client(name: &str) -> Client {
    client_with(Arc::new(FixtureTransport::with_scenario(Some(name))))
}

// --------------------------------------------------------------- endpoints --

#[tokio::test]
async fn the_unfiltered_first_page() {
    let page = fixture_client()
        .list_peers(&PeerQuery::new())
        .await
        .unwrap();
    assert_eq!(page.status, 200);
    assert!(page.envelope().available);
    assert!(!page.envelope().stale);
    assert_eq!(page.peers.len(), 5);
    assert_eq!(page.total, Some(5));
    // Every response carries the rate-limit block.
    assert_eq!(page.rate_limit.limit, Some(20));
    assert_eq!(page.rate_limit.remaining, Some(17));
    assert_eq!(page.rate_limit.reset, Some(42));
    assert!(page.rate_limit.policy.is_some());
    assert!(page.etag.is_some());
    assert!(!page.from_cache);
}

#[tokio::test]
async fn a_null_tier_is_not_tier_zero() {
    let page = fixture_client()
        .list_peers(&PeerQuery::new())
        .await
        .unwrap();
    let sentry = page.peers.iter().find(|p| p.id == "192.0.2.200").unwrap();
    assert_eq!(sentry.tier, Some(0));
    // and the three-state enums are not folded
    let sg = page.peers.iter().find(|p| p.id == "198.51.100.7").unwrap();
    assert_eq!(sg.connectable, Some(Connectable::Yes));
    // mempool is typed, so it never lands in `extra`.
    assert!(!sg.extra.contains_key("mempool"));
    assert_eq!(sg.mempool.serves, Some(true));
    let jp = page.peers.iter().find(|p| p.id == "198.51.100.20").unwrap();
    assert_eq!(jp.connectable, Some(Connectable::No));
}

#[tokio::test]
async fn the_four_owner_queries() {
    let c = fixture_client();

    let q1 = PeerQuery::new().cc(["DE"]);
    assert_eq!(q1.to_query_string().unwrap(), "cc=DE");
    assert_eq!(c.list_peers(&q1).await.unwrap().peers.len(), 2);

    let q2 = PeerQuery::not_full(Geo::cc(["SG"]));
    assert_eq!(q2.to_query_string().unwrap(), "accepting=true&cc=SG");
    assert_eq!(c.list_peers(&q2).await.unwrap().peers.len(), 1);

    // tier=1 is EXACT, and no `sort` is sent unless the caller asks (D-J).
    let q3 = PeerQuery::tier1();
    assert_eq!(q3.to_query_string().unwrap(), "tier=1");
    let p3 = c.list_peers(&q3).await.unwrap();
    assert_eq!(p3.total, Some(1));
    assert!(
        p3.next_cursor.is_none(),
        "the owner queries are single-page"
    );

    // label=sentry, not tier=0.
    let q4 = PeerQuery::sentries();
    assert_eq!(q4.to_query_string().unwrap(), "label=sentry");
    let p4 = c.list_peers(&q4).await.unwrap();
    assert_eq!(p4.total, Some(1));
    assert!(p4.next_cursor.is_none());
}

#[tokio::test]
async fn format_gossip_config_returns_a_config_we_can_edit() {
    let c = fixture_client();
    let q = PeerQuery::new().cc(["DE"]);
    let resp = c.list_peers_as_gossip_config(&q).await.unwrap();
    assert_eq!(
        resp.body.root_ips(),
        vec!["192.0.2.10", "192.0.2.11", "198.51.100.7"]
    );
    assert_eq!(resp.body.chain(), Some("Mainnet"));
    assert!(resp.body.validate().is_ok());
    // the response metadata rides along
    assert_eq!(resp.status, 200);
    assert_eq!(resp.api_version.as_deref(), Some("1"));
    assert_eq!(resp.rate_limit.limit, Some(20));
    assert!(resp.etag.is_some());
    assert!(!resp.from_cache);
}

/// §4.7 golden: `to_json()` of the returned config AND the bytes `save()`
/// writes equal `expect.gossip_config_json_text`, byte for byte.
#[tokio::test]
async fn the_gossip_config_golden() {
    let fx = support::fixture("peers_format_gossip_config");
    let golden = fx["expect"]["gossip_config_json_text"].as_str().unwrap();

    let resp = fixture_client()
        .list_peers_as_gossip_config(&PeerQuery::new().cc(["DE"]))
        .await
        .unwrap();
    assert_eq!(resp.body.to_json(), golden);

    let dir = std::env::temp_dir().join(format!("hl-peers-golden-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("override_gossip_config.json");
    let saved = resp
        .body
        .save(&path, &hl_peers::SaveOpts::default())
        .unwrap();
    assert!(saved.written);
    let bytes = std::fs::read(&path).unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(bytes, golden.as_bytes());
}

#[tokio::test]
async fn the_caller_format_is_overridden_and_try_new_peers_is_sent() {
    let t = Arc::new(FixtureTransport::new());
    let c = client_with(t.clone());
    let _ = c
        .list_peers_as_gossip_config(
            &PeerQuery::new()
                .cc(["DE"])
                .format(hl_peers::Format::Json)
                .try_new_peers(false),
        )
        .await;
    assert_eq!(
        t.last_url().unwrap(),
        "https://fixtures.invalid/peers?cc=DE&format=gossip_config&try_new_peers=false"
    );
    // …while list_peers still refuses the format outright (D-R).
    let e = c
        .list_peers(&PeerQuery::new().format(hl_peers::Format::GossipConfig))
        .await
        .unwrap_err();
    assert_eq!(e.parameter(), Some("format"));
    assert!(e.reason().unwrap().contains("list_peers_as_gossip_config"));
}

#[tokio::test]
async fn one_peers_dossier() {
    let r = fixture_client().get_peer("192.0.2.10").await.unwrap();
    assert_eq!(r.body.peer.id, "192.0.2.10");
    assert_eq!(
        r.body.peer.feeders.as_deref(),
        Some(&["192.0.2.200".to_string()][..])
    );
    let p = &r.body.peer;
    assert_eq!(p.state, Some(hl_peers::PeerState::Responsive));
    assert_eq!(p.cc_confidence.as_deref(), Some("high"));
    assert_eq!(p.cc_source.as_deref(), Some("db+rtt-confirmed"));
    assert_eq!(p.org.as_deref(), Some("Example Hosting"));
    assert_eq!(p.uptime, Some(0.97));
    assert_eq!(p.mempool.serves, Some(true));
    assert_eq!(p.mempool.first_frame_ms, Some(412));
    assert_eq!(p.mempool.source.as_deref(), Some("probe"));
    assert_eq!(p.auction.status, Some(AuctionStatus::HoldsSlot));
    assert_eq!(p.auction.slot, Some(3));
    assert!(
        p.extra.is_empty(),
        "every published key is modelled: {:?}",
        p.extra
    );
    assert!(r
        .body
        .envelope
        .attribution
        .as_deref()
        .unwrap()
        .contains("IPinfo"));
    assert_eq!(r.api_version.as_deref(), Some("1"));
}

#[tokio::test]
async fn facets_keep_not_collected_distinct_from_nothing_matched() {
    let f = fixture_client().facets(None).await.unwrap();
    assert_eq!(f.available("asn"), Some(false)); // NOT COLLECTED
    assert_eq!(f.available("region"), Some(false));
    assert_eq!(f.available("tier"), Some(true));
    assert_eq!(f.facets["tier"].values.len(), 4);
    // the selectable absent-value member is the literal string "null"
    assert!(f.facets["tier"].values.iter().any(|v| v.value == "null"));
    // The mempool/auction facets and geo_coverage
    assert_eq!(f.available("serves_mempool"), Some(true));
    assert_eq!(f.available("auction"), Some(true));
    let sm: Vec<&str> = f.facets["serves_mempool"]
        .values
        .iter()
        .map(|v| v.value.as_str())
        .collect();
    assert_eq!(sm, ["true", "false", "null"]);
    let geo = f.geo_coverage.as_ref().unwrap();
    assert_eq!(geo["source"], "dbip-lite");
    assert!(f.envelope.attribution.is_some());
}

#[tokio::test]
async fn facets_take_every_filter_but_refuse_page_controls() {
    let t = Arc::new(FixtureTransport::new());
    let c = client_with(t.clone());
    // every filter is sent (this one has no fixture, so it 404s; only the URL matters)
    let _ = c
        .facets(Some(
            &PeerQuery::mempool_servers()
                .label(["sentry"])
                .auction([None]),
        ))
        .await;
    assert_eq!(
        t.last_url().unwrap(),
        "https://fixtures.invalid/peers/facets?auction=null&label=sentry&serves_mempool=true"
    );
    for (q, p) in [
        (PeerQuery::new().sort(Sort::Tier), "sort"),
        (PeerQuery::new().limit(10), "limit"),
        (PeerQuery::new().try_new_peers(true), "try_new_peers"),
    ] {
        let e = c.facets(Some(&q)).await.unwrap_err();
        assert_eq!(e.kind(), "Invalid");
        assert_eq!(e.parameter(), Some(p));
    }
    assert_eq!(t.request_count(), 1, "a refusal spends no request");
}

// -------------------------------------------------------------- pagination --

#[tokio::test]
async fn the_three_page_cursor_walk() {
    let t = Arc::new(FixtureTransport::new());
    let c = client_with(t.clone());
    let q = PeerQuery::new().limit(2).sort(Sort::Tier);
    let peers = c.list_all_peers(&q, IterOpts::default()).await.unwrap();
    assert_eq!(peers.len(), 5);
    assert_eq!(peers[0].id, "203.0.113.1");
    assert_eq!(peers[4].id, "203.0.113.5");
    assert_eq!(t.request_count(), 3);
}

#[tokio::test]
async fn max_items_is_a_hard_guard_and_can_raise() {
    let c = fixture_client();
    let q = PeerQuery::new().limit(2).sort(Sort::Tier);

    let mut s = c.iter_peers(&q, IterOpts::default().with_max_items(3));
    let mut n = 0;
    while let Some(item) = s.next().await {
        item.unwrap();
        n += 1;
    }
    assert_eq!(n, 3);
    assert!(s.truncated());

    let mut s = c.iter_peers(
        &q,
        IterOpts::default()
            .with_max_items(3)
            .with_on_truncate(OnTruncate::Raise),
    );
    let mut seen = 0;
    let mut raised = None;
    while let Some(item) = s.next().await {
        match item {
            Ok(_) => seen += 1,
            Err(e) => {
                raised = Some(e);
                break;
            }
        }
    }
    assert_eq!(seen, 3);
    assert!(matches!(raised, Some(Error::Truncated { seen: 3 })));
}

#[tokio::test]
async fn a_cursor_expired_is_surfaced_and_never_auto_restarted() {
    let t = Arc::new(FixtureTransport::new());
    let c = client_with(t.clone());
    let q = PeerQuery::new().cursor("cursor-from-a-previous-sweep");
    let e = c.list_peers(&q).await.unwrap_err();
    assert_eq!(e.kind(), "CursorExpired");
    assert!(e.is_bad_request(), "it is a member of the 400 family");

    // The walk stops at the error; it does not start over.
    let mut s = c.iter_peers(&q, IterOpts::default());
    let first = s.next().await.unwrap();
    assert_eq!(first.unwrap_err().kind(), "CursorExpired");
    assert!(s.next().await.is_none());
    assert_eq!(t.request_count(), 2, "one per call, never a silent retry");
}

// ------------------------------------------------------------ available:false --

#[tokio::test]
async fn available_false_is_not_an_error_and_not_an_empty_result() {
    let c = scenario_client("unavailable");
    let page = c.list_peers(&PeerQuery::new()).await.unwrap();
    assert!(!page.envelope().available);
    assert!(page.peers.is_empty());

    // The stream stops without raising, and the flag is how you tell.
    let mut s = c.iter_peers(&PeerQuery::new(), IterOpts::default());
    assert!(s.next().await.is_none());
    assert_eq!(s.envelope().map(|e| e.available), Some(false));
}

#[tokio::test]
async fn available_false_refuses_to_build_a_config() {
    let c = scenario_client("unavailable");
    let e = hl_peers::update_gossip_config(
        &c,
        &PeerQuery::new(),
        None,
        &hl_peers::UpdateOpts::default(),
    )
    .await
    .unwrap_err();
    assert_eq!(e.kind(), "Unavailable");
}

#[tokio::test]
async fn stale_is_reported_and_available_is_checked_first() {
    let page = scenario_client("stale")
        .list_peers(&PeerQuery::new())
        .await
        .unwrap();
    assert!(page.envelope().available);
    assert!(page.envelope().stale);
}

// ------------------------------------------------------------- ETag / 304 ---

#[tokio::test]
async fn a_304_replays_the_cached_body_with_the_fresh_rate_limit() {
    let t = Arc::new(FixtureTransport::new());
    let c = client_with(t.clone());

    let first = c.list_peers(&PeerQuery::new()).await.unwrap();
    assert_eq!(first.status, 200);
    assert!(!first.from_cache);

    let second = c.list_peers(&PeerQuery::new()).await.unwrap();
    assert_eq!(second.status, 304);
    assert!(second.from_cache);
    assert_eq!(second.peers.len(), first.peers.len());
    assert_eq!(second.rate_limit.limit, Some(20));
    assert_eq!(t.request_count(), 2);

    // The second request carried the stored validator.
    let sent = t.requests.lock().unwrap();
    assert!(sent[1]
        .headers
        .iter()
        .any(|(k, _)| k.eq_ignore_ascii_case("If-None-Match")));
}

#[tokio::test]
async fn a_caller_supplied_validator_gets_not_modified_not_a_body() {
    let c = fixture_client();
    let first = c.list_peers(&PeerQuery::new()).await.unwrap();
    let etag = first.etag.clone().unwrap();
    let out = c
        .list_peers_conditional(&PeerQuery::new(), &etag)
        .await
        .unwrap();
    match out {
        hl_peers::Conditional::NotModified { rate_limit, .. } => {
            assert_eq!(rate_limit.limit, Some(20));
        }
        other => panic!("expected NotModified, got {other:?}"),
    }
}

// ------------------------------------------------------------------ errors --

#[tokio::test]
async fn a_400_filter_unavailable_is_never_an_empty_list() {
    let e = fixture_client()
        .list_peers(&PeerQuery::new().asn(["AS24940"]))
        .await
        .unwrap_err();
    match &e {
        Error::FilterUnavailable { filter, detail, .. } => {
            assert_eq!(filter.as_deref(), Some("asn"));
            assert!(detail.as_ref().unwrap().contains("not collected"));
        }
        other => panic!("expected FilterUnavailable, got {other:?}"),
    }
    assert!(e.is_bad_request());
    assert_eq!(e.rate_limit().unwrap().limit, Some(20));
}

#[tokio::test]
async fn region_is_the_other_uncollected_attribute() {
    let e = fixture_client()
        .list_peers(&PeerQuery::new().region(["aws:eu-central-1"]))
        .await
        .unwrap_err();
    assert_eq!(e.parameter(), Some("region"));
    assert_eq!(e.kind(), "FilterUnavailable");
}

#[tokio::test]
async fn chain_not_served_carries_what_is_allowed() {
    let e = fixture_client()
        .list_peers(&PeerQuery::new().chain(hl_peers::Chain::Testnet))
        .await
        .unwrap_err();
    match e {
        Error::ChainNotServed { allowed, .. } => assert_eq!(allowed, vec!["Mainnet"]),
        other => panic!("expected ChainNotServed, got {other:?}"),
    }
}

#[tokio::test]
async fn an_invalid_enum_value_is_refused_locally_and_the_server_400_is_a_bad_request() {
    // §2.2(5): a closed-enum violation never reaches the server…
    let t = Arc::new(FixtureTransport::new());
    let q = PeerQuery::new().connectable([Connectable::Other("yess".into())]);
    let e = client_with(t.clone()).list_peers(&q).await.unwrap_err();
    assert_eq!(e.kind(), "Invalid");
    assert_eq!(e.parameter(), Some("connectable"));
    assert_eq!(t.request_count(), 0);

    // …and if a server 400 names one anyway, it maps to plain BadRequest.
    let e = client_with(Arc::new(support::replay("error_400_invalid_enum")))
        .list_peers(&PeerQuery::new())
        .await
        .unwrap_err();
    match e {
        Error::BadRequest {
            parameter, allowed, ..
        } => {
            assert_eq!(parameter.as_deref(), Some("connectable"));
            assert_eq!(allowed.unwrap().len(), 3);
        }
        other => panic!("expected BadRequest, got {other:?}"),
    }
}

#[tokio::test]
async fn a_401_is_a_bad_key_not_a_missing_one() {
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .api_key("hlp_live_invalidkey0000000000")
        .transport(Arc::new(FixtureTransport::new()))
        .build()
        .unwrap();
    let e = c.list_peers(&PeerQuery::new()).await.unwrap_err();
    assert_eq!(e.kind(), "Unauthorized");
    // 🔴 the key never appears in the error
    assert!(!format!("{e:?}").contains("invalidkey"));
}

#[tokio::test]
async fn a_404_and_a_400_on_the_peer_path_do_not_collapse() {
    let c = fixture_client();

    // well-formed IPv4, in no source
    let e = c.get_peer("198.51.100.254").await.unwrap_err();
    assert_eq!(e.kind(), "NotFound");

    // malformed IPv4: refused locally so the typo spends no quota
    let e = c.get_peer("not-an-ip").await.unwrap_err();
    assert_eq!(e.kind(), "Invalid");
    assert_eq!(e.parameter(), Some("ip"));

    // IPv6 is refused locally, and says why
    let e = c.get_peer("2001:db8::1").await.unwrap_err();
    assert_eq!(e.kind(), "Invalid");
    assert!(e.reason().unwrap().contains("IPv4-only"));

    // and a server 400 on that path still maps to BadRequest
    let t = Arc::new(ScriptedTransport::new(vec![Ok(json_response(
        400,
        r#"{"type":"https://example.invalid/problems/invalid-filter-value","title":"Invalid IP","status":400,"detail":"'x' is not an IPv4 address","parameter":"ip"}"#,
        &[("Content-Type", "application/problem+json")],
    ))]));
    let e = client_with(t).get_peer("192.0.2.99").await.unwrap_err();
    assert_eq!(e.kind(), "BadRequest");
}

#[tokio::test]
async fn a_500_with_a_non_problem_body_falls_back_to_the_status() {
    let c2 = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(Arc::new(FixtureTransport::with_scenario(Some(
            "server-error",
        ))))
        .max_retries(0)
        .build()
        .unwrap();
    let e = c2.list_peers(&PeerQuery::new()).await.unwrap_err();
    match e {
        Error::Server {
            status,
            body_excerpt,
            ..
        } => {
            assert_eq!(status, 500);
            assert_eq!(body_excerpt, "Internal Server Error");
        }
        other => panic!("expected Server, got {other:?}"),
    }
}

// ------------------------------------------------------- 429 and the retries --

#[tokio::test]
async fn a_429_parses_every_header_and_is_retried_within_the_cap() {
    let sleeper = Arc::new(RecordingSleeper::default());
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(Arc::new(FixtureTransport::with_scenario(Some(
            "rate-limited",
        ))))
        .sleeper(sleeper.clone())
        .max_retries(0)
        .build()
        .unwrap();
    let e = c.list_peers(&PeerQuery::new()).await.unwrap_err();
    match e {
        Error::RateLimited {
            retry_after,
            remaining,
            reset,
            signup_url,
            problem,
            ..
        } => {
            assert_eq!(retry_after, Some(42));
            assert_eq!(remaining, Some(0));
            assert_eq!(reset, Some(42));
            assert_eq!(
                signup_url.as_deref(),
                Some("https://hypervisor.gg/docs/peer-api/authentication")
            );
            // The prose figures in `detail` are surfaced verbatim and never parsed (D-A).
            assert!(problem.unwrap().detail.unwrap().contains("120/minute"));
        }
        other => panic!("expected RateLimited, got {other:?}"),
    }
    assert!(
        sleeper.recorded().is_empty(),
        "max_retries=0 must not sleep"
    );

    // With a retry budget, Retry-After is honoured and the attempt is repeated.
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(ScriptedTransport::new(vec![
        Ok(json_response(
            429,
            r#"{"type":"https://example.invalid/problems/rate-limited","title":"x","status":429}"#,
            &[
                ("Content-Type", "application/problem+json"),
                ("Retry-After", "42"),
            ],
        )),
        Ok(json_response(
            200,
            r#"{"version":"v1","available":true,"stale":false,"chain":"Mainnet","count":0,"total":0,"next_cursor":null,"peers":[]}"#,
            &[("Content-Type", "application/json")],
        )),
    ]));
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .max_retries(3)
        .build()
        .unwrap();
    c.list_peers(&PeerQuery::new()).await.unwrap();
    assert_eq!(t.calls(), 2);
    assert_eq!(sleeper.recorded(), vec![Duration::from_secs(42)]);
}

/// Every `429` fixture states the answer in `expect.retry_after_seconds`, and
/// an HTTP-date one also carries `clock.now` — the instant to inject so the
/// assertion never depends on wall-clock time (§5.2).
#[tokio::test]
async fn every_429_fixture_resolves_retry_after_as_it_says() {
    let mut checked = 0;
    for fx in support::all_fixtures() {
        let Some(expect) = fx.get("expect").and_then(|e| e.get("retry_after_seconds")) else {
            continue;
        };
        let scenario = fx["request"]["headers"]["X-Mock-Scenario"]
            .as_str()
            .expect("a 429 fixture is selected by scenario")
            .to_string();
        let now = fx
            .get("clock")
            .and_then(|c| c.get("now"))
            .and_then(|n| n.as_str())
            .map(support::parse_rfc3339_z)
            .unwrap_or(0);

        let sleeper = Arc::new(RecordingSleeper::default());
        let client = Client::builder()
            .base_url("https://fixtures.invalid")
            .transport(Arc::new(FixtureTransport::with_scenario(Some(&scenario))))
            .sleeper(sleeper.clone())
            .clock(Arc::new(FixedClock(now)))
            .max_retries(0)
            .build()
            .unwrap();

        let e = client.list_peers(&PeerQuery::new()).await.unwrap_err();
        let want = expect.as_u64();
        match &e {
            Error::RateLimited {
                retry_after,
                rate_limit,
                ..
            } => {
                assert_eq!(
                    *retry_after,
                    want,
                    "{}: retry_after",
                    fx["name"].as_str().unwrap()
                );
                assert_eq!(rate_limit.retry_after, want);
                // the raw header is always kept, even when it resolved to None
                assert!(rate_limit.retry_after_raw.is_some());
            }
            other => panic!("expected RateLimited, got {other:?}"),
        }
        checked += 1;
    }
    assert_eq!(checked, 5, "all five 429 fixtures carry an expectation");
}

#[tokio::test]
async fn an_unparseable_retry_after_falls_back_to_backoff() {
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(FixtureTransport::with_scenario(Some(
        "rate-limited-bad-retry-after",
    )));
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .jitter(Arc::new(hl_peers::NoJitter))
        .max_retries(2)
        .build()
        .unwrap();
    let e = c.list_peers(&PeerQuery::new()).await.unwrap_err();
    assert_eq!(e.kind(), "RateLimited");
    // Treated as ABSENT: the request is not failed over a bad header, it backs off.
    assert_eq!(t.request_count(), 3);
    assert_eq!(
        sleeper.recorded(),
        vec![Duration::from_millis(500), Duration::from_millis(1000)]
    );
}

#[tokio::test]
async fn an_http_date_retry_after_is_capped_like_a_numeric_one() {
    // 2026-09-21T02:35:00Z minus an hour: the date is then 61 minutes out.
    let now = support::parse_rfc3339_z("2026-09-21T01:35:00Z");
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(FixtureTransport::with_scenario(Some(
        "rate-limited-http-date",
    )));
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .clock(Arc::new(FixedClock(now)))
        .max_retries(3)
        .max_retry_after(Duration::from_secs(60))
        .build()
        .unwrap();
    let e = c.list_peers(&PeerQuery::new()).await.unwrap_err();
    match e {
        Error::RateLimited { retry_after, .. } => assert_eq!(retry_after, Some(3660)),
        other => panic!("expected RateLimited, got {other:?}"),
    }
    assert_eq!(t.request_count(), 1);
    assert!(
        sleeper.recorded().is_empty(),
        "above the cap: raise, do not sleep"
    );
}

#[tokio::test]
async fn a_retry_after_above_the_cap_raises_instead_of_sleeping() {
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(FixtureTransport::with_scenario(Some("rate-limited-long")));
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .max_retries(3)
        .max_retry_after(Duration::from_secs(60))
        .build()
        .unwrap();
    let e = c.list_peers(&PeerQuery::new()).await.unwrap_err();
    match e {
        Error::RateLimited { retry_after, .. } => assert_eq!(retry_after, Some(3600)),
        other => panic!("expected RateLimited, got {other:?}"),
    }
    // D-I: one attempt, no sleep. A library must not block a caller for an hour.
    assert_eq!(t.request_count(), 1);
    assert!(sleeper.recorded().is_empty());
}

#[tokio::test]
async fn a_503_is_retried_and_a_4xx_is_not() {
    // 503 -> backoff, up to max_retries additional attempts.
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(FixtureTransport::with_scenario(Some(
        "producer-unavailable",
    )));
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .jitter(Arc::new(hl_peers::NoJitter))
        .max_retries(2)
        .build()
        .unwrap();
    let e = c.list_peers(&PeerQuery::new()).await.unwrap_err();
    assert_eq!(e.kind(), "Unavailable");
    assert_eq!(t.request_count(), 3, "the first attempt plus max_retries");
    assert_eq!(sleeper.recorded().len(), 2);

    // 🔴 a 400/401/404 is deterministic and is never retried.
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(FixtureTransport::new());
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .max_retries(3)
        .build()
        .unwrap();
    let _ = c
        .list_peers(&PeerQuery::new().asn(["AS24940"]))
        .await
        .unwrap_err();
    assert_eq!(t.request_count(), 1);
    assert!(sleeper.recorded().is_empty());
}

#[tokio::test]
async fn a_transport_fault_is_retried_then_raised() {
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(ScriptedTransport::new(vec![Err("connection reset".into())]));
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .jitter(Arc::new(hl_peers::NoJitter))
        .max_retries(2)
        .build()
        .unwrap();
    let e = c.list_peers(&PeerQuery::new()).await.unwrap_err();
    assert_eq!(e.kind(), "Transport");
    assert_eq!(t.calls(), 3);
    // Decisions, not delays: only that it backed off twice.
    assert_eq!(sleeper.recorded().len(), 2);
}

// ------------------------------------------------------------------- misc ---

#[tokio::test]
async fn the_polling_helper_refuses_a_too_fast_interval() {
    let c = fixture_client();
    let e = c
        .poll_peers(&PeerQuery::new(), Duration::from_secs(5), |_| false)
        .await
        .unwrap_err();
    assert_eq!(e.kind(), "Invalid");
    assert_eq!(e.parameter(), Some("interval"));
}

#[tokio::test]
async fn the_request_url_is_canonical() {
    let t = Arc::new(FixtureTransport::new());
    let c = client_with(t.clone());
    let q = PeerQuery::new()
        .cc(["de", "nl"])
        .tier([TierFilter::Exact(1), TierFilter::Unplaced])
        .limit(50);
    let _ = c.list_peers(&q).await;
    assert_eq!(
        t.last_url().unwrap(),
        "https://fixtures.invalid/peers?cc=DE%2CNL&limit=50&tier=1%2Cnull"
    );
}

#[test]
fn base_path_default_and_normalization() {
    assert_eq!(DEFAULT_BASE_PATH, "");
    assert_eq!(fixture_client().base_path(), "");
    assert_eq!(normalize_base_path("peer-api/v1/"), "/peer-api/v1");
    assert_eq!(normalize_base_path("//peer-api/v1//"), "/peer-api/v1");
    assert_eq!(normalize_base_path("/"), "");
    assert_eq!(normalize_base_path(""), "");
}

#[tokio::test]
async fn a_custom_base_path_reaches_every_route() {
    let t = Arc::new(FixtureTransport::new());
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .base_path("/peer-api/v1/")
        .transport(t.clone())
        .max_retries(0)
        .build()
        .unwrap();
    assert_eq!(c.base_path(), "/peer-api/v1");
    // The fixtures live at the host root, so these answer 404; only the URL matters.
    let _ = c.list_peers(&PeerQuery::new().cc(["DE"])).await;
    assert_eq!(
        t.last_url().unwrap(),
        "https://fixtures.invalid/peer-api/v1/peers?cc=DE"
    );
    let _ = c.get_peer("192.0.2.10").await;
    assert_eq!(
        t.last_url().unwrap(),
        "https://fixtures.invalid/peer-api/v1/peers/192.0.2.10"
    );
    let _ = c.facets(None).await;
    assert_eq!(
        t.last_url().unwrap(),
        "https://fixtures.invalid/peer-api/v1/peers/facets"
    );
}

// -{66} defaults ---

#[test]
fn the_1_0_defaults() {
    assert_eq!(DEFAULT_BASE_URL, "https://api.hypervisor.gg");
    let c = Client::builder()
        .transport(Arc::new(FixtureTransport::new()))
        .build()
        .unwrap();
    assert_eq!(c.max_retries(), 0, "auto-retry is OFF by default (D-Q)");
    assert_eq!(c.base_path(), "");
    assert_eq!(
        c.user_agent(),
        format!("hl-peers-rust/{}", env!("CARGO_PKG_VERSION"))
    );
    assert_eq!(env!("CARGO_PKG_VERSION"), "1.1.0");
    // An explicit argument beats everything (the env var is not consulted
    // here, so this test never depends on the runner's environment).
    let c = Client::builder()
        .base_url("https://proxy.example.invalid/")
        .transport(Arc::new(FixtureTransport::new()))
        .build()
        .unwrap();
    assert_eq!(c.base_url(), "https://proxy.example.invalid");
}

#[tokio::test]
async fn owner_query_1_whole_mempool_servers_in_de() {
    let q = PeerQuery::mempool_servers().cc(["DE"]);
    assert_eq!(q.to_query_string().unwrap(), "cc=DE&serves_mempool=true");
    let page = fixture_client().list_peers(&q).await.unwrap();
    assert_eq!(page.peers.len(), 1);
    let p = &page.peers[0];
    assert_eq!(p.id, "192.0.2.10");
    assert_eq!(p.mempool.serves, Some(true));
    assert_eq!(p.mempool.verdict.as_deref(), Some("serves"));
    assert_eq!(
        p.mempool.last_checked.as_deref(),
        Some("2026-09-21T02:30:00Z")
    );
    assert_eq!(p.auction.status, Some(AuctionStatus::HoldsSlot));
    assert_eq!(p.auction.since.as_deref(), Some("2026-09-20T11:00:00Z"));
    assert_eq!(
        p.auction.last_bid_at.as_deref(),
        Some("2026-09-20T10:59:12Z")
    );
}

#[tokio::test]
async fn serves_mempool_null_is_unknown_never_false() {
    let q = PeerQuery::new().serves_mempool([None]);
    assert_eq!(q.to_query_string().unwrap(), "serves_mempool=null");
    let page = fixture_client().list_peers(&q).await.unwrap();
    assert_eq!(page.peers.len(), 2);
    for p in &page.peers {
        assert_eq!(p.mempool, hl_peers::PeerMempool::default());
        assert_ne!(p.mempool.serves, Some(false));
        assert_eq!(p.auction, hl_peers::PeerAuction::default());
    }
}

#[tokio::test]
async fn auction_holds_slot_or_bidding() {
    let q =
        PeerQuery::new().auction([Some(AuctionStatus::HoldsSlot), Some(AuctionStatus::Bidding)]);
    let page = fixture_client().list_peers(&q).await.unwrap();
    type Signal<'a> = (&'a str, Option<&'a str>, Option<i64>, Option<&'a str>);
    let signals: Vec<Signal> = page
        .peers
        .iter()
        .map(|p| {
            (
                p.id.as_str(),
                p.auction.status.as_ref().map(|s| s.as_str()),
                p.auction.slot,
                p.mempool.source.as_deref(),
            )
        })
        .collect();
    assert_eq!(
        signals,
        vec![
            ("192.0.2.10", Some("holds_slot"), Some(3), Some("probe")),
            ("198.51.100.7", Some("bidding"), None, Some("relay")),
        ]
    );
}

#[tokio::test]
async fn the_removed_mempool_filter_is_a_bad_request() {
    let e = fixture_client()
        .list_peers(&PeerQuery::new().extra_param("mempool", "serving"))
        .await
        .unwrap_err();
    match &e {
        Error::BadRequest { parameter, .. } => assert_eq!(parameter.as_deref(), Some("mempool")),
        other => panic!("expected BadRequest, got {other:?}"),
    }
    assert_eq!(e.problem().unwrap().type_suffix(), Some("filter-removed"));
    assert_eq!(e.api_version(), Some("1"));
}

#[tokio::test]
async fn serves_mempool_can_be_filter_unavailable() {
    let e = scenario_client("mempool-unavailable")
        .list_peers(&PeerQuery::mempool_servers())
        .await
        .unwrap_err();
    match &e {
        Error::FilterUnavailable { filter, .. } => {
            assert_eq!(filter.as_deref(), Some("serves_mempool"))
        }
        other => panic!("expected FilterUnavailable, got {other:?}"),
    }
}

#[tokio::test]
async fn the_network_map_graph() {
    let t = Arc::new(FixtureTransport::new());
    let r = client_with(t.clone())
        .network_map_graph(&NetworkMapOpts::new().max_tier(2))
        .await
        .unwrap();
    assert_eq!(
        t.last_url().unwrap(),
        "https://fixtures.invalid/network-map/graph?max_tier=2"
    );
    let g = &r.body;
    assert!(g.available);
    assert!(!g.stale);
    assert_eq!(g.operator_nodes.count, None);
    assert_eq!(g.node_count, Some(4));
    assert_eq!(g.edge_count, Some(3));
    assert_eq!(g.tier_count, Some(3));
    assert_eq!(g.unreached, Some(0));
    assert_eq!(g.truncated, Some(false));
    assert_eq!(g.tiers.len(), 3);
    assert_eq!(g.nodes.len(), 4);
    // nodes decode with the Peer decoder
    assert_eq!(g.nodes[0].id, "192.0.2.200");
    assert_eq!(g.nodes[0].tier, Some(0));
    assert_eq!(g.nodes[0].mempool.serves, None);
    assert_eq!(g.edges[0].from, "192.0.2.200");
    assert_eq!(g.edges[0].span, Some(1));
    assert_eq!(r.api_version.as_deref(), Some("1"));
    assert_eq!(r.rate_limit.limit, Some(20));
}

#[tokio::test]
async fn the_network_map_summary() {
    let c = fixture_client();
    let r = c.network_map_summary(&NetworkMapOpts::new()).await.unwrap();
    assert_eq!(r.body.node_count, Some(4));
    assert_eq!(r.body.connectable["yes"], 3);
    assert_eq!(r.body.connectable["unknown"], 0);
    assert_eq!(r.body.tiers[1].tier, 1);
    assert_eq!(r.body.tiers[1].count, 2);
    let second = c.network_map_summary(&NetworkMapOpts::new()).await.unwrap();
    assert_eq!(second.status, 304);
    assert!(second.from_cache);
    assert_eq!(second.body, r.body);
    // a closed-enum violation is refused locally
    let e = c
        .network_map_summary(
            &NetworkMapOpts::new().connectable([Connectable::Other("maybe".into())]),
        )
        .await
        .unwrap_err();
    assert_eq!(e.parameter(), Some("connectable"));
}

#[tokio::test]
async fn api_version_is_exposed_on_results_and_errors() {
    let c = fixture_client();
    let ok = c.list_peers(&PeerQuery::new()).await.unwrap();
    assert_eq!(ok.api_version.as_deref(), Some("1"));
    let cached = c.list_peers(&PeerQuery::new()).await.unwrap();
    assert_eq!(cached.status, 304);
    assert_eq!(
        cached.api_version.as_deref(),
        Some("1"),
        "from the 304 itself"
    );
    let e = c.get_peer("198.51.100.254").await.unwrap_err();
    assert_eq!(e.api_version(), Some("1"));

    // informational: any other value is carried, never an error
    let t = Arc::new(ScriptedTransport::new(vec![Ok(json_response(
        200,
        r#"{"version":"v1","available":true,"stale":false,"chain":"Mainnet","count":0,"total":0,"next_cursor":null,"peers":[]}"#,
        &[("API-Version", "7")],
    ))]));
    let r = client_with(t).list_peers(&PeerQuery::new()).await.unwrap();
    assert_eq!(r.api_version.as_deref(), Some("7"));
}

#[tokio::test]
async fn retries_are_off_by_default_one_503_is_one_request() {
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(FixtureTransport::with_scenario(Some(
        "producer-unavailable",
    )));
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .build()
        .unwrap();
    let e = c.list_peers(&PeerQuery::new()).await.unwrap_err();
    assert_eq!(e.kind(), "Unavailable");
    assert_eq!(t.request_count(), 1);
    assert!(sleeper.recorded().is_empty());

    // and a 429 raises at once, without sleeping
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(FixtureTransport::with_scenario(Some("rate-limited")));
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .build()
        .unwrap();
    assert_eq!(
        c.list_peers(&PeerQuery::new()).await.unwrap_err().kind(),
        "RateLimited"
    );
    assert_eq!(t.request_count(), 1);
    assert!(sleeper.recorded().is_empty());
}

#[tokio::test]
async fn an_explicit_max_retries_opts_in() {
    let sleeper = Arc::new(RecordingSleeper::default());
    let t = Arc::new(ScriptedTransport::new(vec![
        Ok(json_response(503, "", &[])),
        Ok(json_response(
            200,
            r#"{"version":"v1","available":true,"stale":false,"chain":"Mainnet","count":0,"total":0,"next_cursor":null,"peers":[]}"#,
            &[("API-Version", "1")],
        )),
    ]));
    let c = Client::builder()
        .base_url("https://fixtures.invalid")
        .transport(t.clone())
        .sleeper(sleeper.clone())
        .max_retries(3)
        .build()
        .unwrap();
    let r = c.list_peers(&PeerQuery::new()).await.unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(t.calls(), 2);
    assert_eq!(sleeper.recorded().len(), 1);
}

fn unix(t: std::time::SystemTime) -> i64 {
    t.duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64
}

#[tokio::test]
async fn rate_limited_reset_at_comes_from_the_injected_clock() {
    let now = support::parse_rfc3339_z("2026-09-21T02:35:00Z");
    let client = |scenario: &str| {
        Client::builder()
            .base_url("https://fixtures.invalid")
            .transport(Arc::new(FixtureTransport::with_scenario(Some(scenario))))
            .clock(Arc::new(FixedClock(now)))
            .build()
            .unwrap()
    };

    // now + retry_after (numeric)
    match client("rate-limited")
        .list_peers(&PeerQuery::new())
        .await
        .unwrap_err()
    {
        Error::RateLimited {
            reset_at,
            signup_url,
            api_version,
            ..
        } => {
            assert_eq!(unix(reset_at.unwrap()), now + 42);
            assert_eq!(
                signup_url.as_deref(),
                Some("https://hypervisor.gg/docs/peer-api/authentication")
            );
            assert_eq!(api_version.as_deref(), Some("1"));
        }
        other => panic!("expected RateLimited, got {other:?}"),
    }
    // now + retry_after (HTTP-date, resolved against the same clock)
    match client("rate-limited-http-date")
        .list_peers(&PeerQuery::new())
        .await
        .unwrap_err()
    {
        Error::RateLimited { reset_at, .. } => assert_eq!(unix(reset_at.unwrap()), now + 60),
        other => panic!("expected RateLimited, got {other:?}"),
    }
    // Retry-After unparseable ⇒ absent ⇒ now + RateLimit-Reset, if sent
    let fx = support::fixture("error_429_retry_after_unparseable");
    let reset = fx["response"]["headers"]["RateLimit-Reset"]
        .as_str()
        .and_then(|s| s.parse::<i64>().ok());
    match client("rate-limited-bad-retry-after")
        .list_peers(&PeerQuery::new())
        .await
        .unwrap_err()
    {
        Error::RateLimited { reset_at, .. } => {
            assert_eq!(reset_at.map(unix), reset.map(|r| now + r))
        }
        other => panic!("expected RateLimited, got {other:?}"),
    }
    // neither header ⇒ None
    let t = Arc::new(ScriptedTransport::new(vec![Ok(json_response(
        429,
        "",
        &[],
    ))]));
    match client_with(t)
        .list_peers(&PeerQuery::new())
        .await
        .unwrap_err()
    {
        Error::RateLimited { reset_at, .. } => assert_eq!(reset_at, None),
        other => panic!("expected RateLimited, got {other:?}"),
    }
}

/// `spec/peer-api-v1.fields.json` is generated from the spec. The Rust types
/// must cover exactly those field sets — read at test time, so a spec change
/// fails here rather than drifting silently.
#[test]
fn the_types_cover_exactly_the_spec_field_sets() {
    use std::collections::BTreeSet;
    let spec: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../spec/peer-api-v1.fields.json"),
        )
        .unwrap(),
    )
    .unwrap();
    let want = |k: &str| -> BTreeSet<String> {
        spec[k]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect()
    };
    let keys = |v: serde_json::Value| -> BTreeSet<String> {
        v.as_object().unwrap().keys().cloned().collect()
    };

    let peer: hl_peers::Peer = serde_json::from_str(r#"{"id":"192.0.2.1"}"#).unwrap();
    let peer_v = serde_json::to_value(&peer).unwrap();
    assert_eq!(keys(peer_v.clone()), want("peer"), "Peer");
    assert_eq!(
        keys(peer_v["mempool"].clone()),
        want("peer_mempool"),
        "PeerMempool"
    );
    assert_eq!(
        keys(peer_v["auction"].clone()),
        want("peer_auction"),
        "PeerAuction"
    );

    let list: hl_peers::PeerList = serde_json::from_str(
        r#"{"version":"v1","available":true,"stale":false,"chain":"Mainnet","peers":[]}"#,
    )
    .unwrap();
    assert_eq!(
        keys(serde_json::to_value(&list.envelope).unwrap()),
        want("envelope"),
        "Envelope"
    );
    assert_eq!(
        keys(serde_json::to_value(&list).unwrap()),
        want("peer_list"),
        "PeerList"
    );

    let facets: hl_peers::Facets = serde_json::from_str(
        r#"{"version":"v1","available":true,"stale":false,"chain":"Mainnet","facets":{}}"#,
    )
    .unwrap();
    assert_eq!(
        keys(serde_json::to_value(&facets).unwrap()),
        want("facets"),
        "Facets"
    );

    let graph: hl_peers::NetworkMapGraph =
        serde_json::from_str(r#"{"version":"v1","available":true,"stale":false}"#).unwrap();
    assert_eq!(
        keys(serde_json::to_value(&graph).unwrap()),
        want("network_map_graph"),
        "NetworkMapGraph"
    );
    let summary: hl_peers::NetworkMapSummary =
        serde_json::from_str(r#"{"version":"v1","available":true,"stale":false}"#).unwrap();
    assert_eq!(
        keys(serde_json::to_value(&summary).unwrap()),
        want("network_map_summary"),
        "NetworkMapSummary"
    );
    let edge: hl_peers::NetworkMapEdge =
        serde_json::from_str(r#"{"from":"192.0.2.1","to":"192.0.2.2"}"#).unwrap();
    assert_eq!(
        keys(serde_json::to_value(&edge).unwrap()),
        want("network_map_edge")
    );
    let city: hl_peers::CityCluster = serde_json::from_str(
        r#"{"id":"JP-tokyo","name":"Tokyo","cc":"JP","lat":35.7,"lon":139.7,"peers":1,"tiers":{}}"#,
    )
    .unwrap();
    assert_eq!(
        keys(serde_json::to_value(&city).unwrap()),
        want("network_map_city"),
        "CityCluster"
    );

    // Query parameters: a query with every field set emits exactly the spec's
    // list_peers params; facets accept exactly the spec's facets params.
    let full = PeerQuery::new()
        .accepting(true)
        .asn(["AS1"])
        .auction([None])
        .cc(["DE"])
        .chain(hl_peers::Chain::Mainnet)
        .connectable([Connectable::Yes])
        .country(["DE"])
        .cursor("c")
        .fields(["id"])
        .format(hl_peers::Format::Json)
        .label(["sentry"])
        .limit(1)
        .max_blocks_behind(0)
        .max_latency_ms(1)
        .max_tier(1)
        .min_uptime(0.5)
        .region(["aws:x"])
        .role(["r"])
        .seen_within("1h")
        .serves_mempool([Some(true)])
        .sort(Sort::Tier)
        .state([hl_peers::PeerState::Full])
        .tier([TierFilter::Exact(1)])
        .try_new_peers(true)
        .vantage("eu")
        .version(["1"]);
    let sent: BTreeSet<String> = full
        .to_pairs()
        .unwrap()
        .into_iter()
        .map(|(k, _)| k)
        .collect();
    assert_eq!(sent, want("list_peers_params"));
    let mut facets_ok = full.clone();
    facets_ok.sort = None;
    facets_ok.limit = None;
    facets_ok.cursor = None;
    facets_ok.fields.clear();
    facets_ok.format = None;
    facets_ok.try_new_peers = None;
    let facet_sent: BTreeSet<String> = facets_ok
        .to_pairs()
        .unwrap()
        .into_iter()
        .map(|(k, _)| k)
        .collect();
    assert_eq!(facet_sent, want("facets_params"));

    let nm = NetworkMapOpts::new()
        .cc("DE")
        .connectable([Connectable::Yes])
        .hide_unreachable(true)
        .label("sentry")
        .max_tier(1)
        .operator("op")
        .serves_mempool([Some(true)])
        .auction([None])
        .show_unreached(true);
    let nm_sent: BTreeSet<String> = nm
        .to_query_string()
        .unwrap()
        .split('&')
        .map(|kv| kv.split('=').next().unwrap().to_string())
        .collect();
    assert_eq!(nm_sent, want("network_map_params"));

    // Closed enums: every spec member is accepted, and nothing else is.
    let e = &spec["enums"];
    for s in e["state"].as_array().unwrap() {
        let q = PeerQuery::new().state([hl_peers::PeerState::from_wire(s.as_str().unwrap())]);
        assert!(q.validate().is_ok(), "state {s}");
    }
    for s in e["connectable"].as_array().unwrap() {
        let q = PeerQuery::new().connectable([Connectable::from_wire(s.as_str().unwrap())]);
        assert!(q.validate().is_ok(), "connectable {s}");
    }
    for s in e["auction"].as_array().unwrap() {
        let s = s.as_str().unwrap();
        let q = PeerQuery::new().auction([(s != "null").then(|| AuctionStatus::from_wire(s))]);
        assert!(q.validate().is_ok(), "auction {s}");
        assert!(q.to_query_string().unwrap().ends_with(s));
    }
    for s in e["sort"].as_array().unwrap() {
        assert!(Sort::parse(s.as_str().unwrap()).is_ok());
    }
}

// -{66} city clusters, map filters ---

#[tokio::test]
async fn the_listening_state_is_decoded_and_selectable() {
    use hl_peers::PeerState;
    let t = Arc::new(FixtureTransport::new());
    let r = client_with(t.clone())
        .list_peers(&PeerQuery::new().state([PeerState::Listening]))
        .await
        .unwrap();
    assert_eq!(
        t.last_url().unwrap(),
        "https://fixtures.invalid/peers?state=listening"
    );
    let p = &r.body.peers[0];
    assert_eq!(p.state, Some(PeerState::Listening));
    assert_eq!(p.tier, None, "listening is never placed: tier null, not 0");
    assert_eq!(p.latest_height, None);
    assert_eq!(p.city, None);
    assert!(PeerQuery::new()
        .state([PeerState::Listening])
        .validate()
        .is_ok());
    let e = PeerQuery::new()
        .state([PeerState::from_wire("LISTENING")])
        .validate()
        .unwrap_err();
    assert_eq!(e.parameter(), Some("state"));
}

#[tokio::test]
async fn the_graph_carries_cities_and_attribution() {
    let r = fixture_client()
        .network_map_graph(&NetworkMapOpts::new().max_tier(2))
        .await
        .unwrap();
    let g = &r.body;
    let cities = g.cities.as_ref().expect("cities present");
    assert_eq!(cities.len(), 1);
    assert_eq!(cities[0].id, "DE-frankfurt-am-main");
    assert_eq!(cities[0].cc, "DE");
    assert_eq!(cities[0].peers, 2);
    assert_eq!(cities[0].tiers["1"], 1);
    assert!((cities[0].lat - 50.1).abs() < 1e-9);
    let attribution = g.attribution.as_deref().unwrap();
    assert!(attribution.contains("IPinfo") && attribution.contains("DB-IP"));
    assert_eq!(g.nodes[0].city, None);
    assert_eq!(g.nodes[1].city.as_deref(), Some("DE-frankfurt-am-main"));
}

#[tokio::test]
async fn the_summary_carries_mempool_auction_counts_and_cities() {
    let r = fixture_client()
        .network_map_summary(&NetworkMapOpts::new())
        .await
        .unwrap();
    let s = &r.body;
    assert_eq!(s.serves_mempool["true"], 2);
    assert_eq!(s.serves_mempool["null"], 1);
    assert_eq!(s.auction["holds_slot"], 1);
    assert_eq!(s.auction["null"], 1);
    assert_eq!(s.cities.as_ref().unwrap()[0].id, "DE-frankfurt-am-main");
    assert!(s.attribution.as_deref().unwrap().contains("DB-IP"));
}

#[tokio::test]
async fn the_map_takes_the_mempool_and_auction_filters() {
    let t = Arc::new(FixtureTransport::new());
    let r = client_with(t.clone())
        .network_map_graph(
            &NetworkMapOpts::new()
                .serves_mempool([Some(true)])
                .auction([Some(AuctionStatus::HoldsSlot)]),
        )
        .await
        .unwrap();
    assert_eq!(
        t.last_url().unwrap(),
        "https://fixtures.invalid/network-map/graph?auction=holds_slot&serves_mempool=true"
    );
    let g = &r.body;
    assert_eq!(g.nodes.len(), 1);
    assert!(g.edges.is_empty());
    assert_eq!(g.cities, None, "absent is not an empty list");
    assert_eq!(g.attribution, None);
    assert_eq!(g.nodes[0].city, None);
    // closed enum, refused before any request
    let calls = t.request_count();
    let e = client_with(t.clone())
        .network_map_graph(
            &NetworkMapOpts::new().auction([Some(AuctionStatus::from_wire("holding"))]),
        )
        .await
        .unwrap_err();
    assert_eq!(e.parameter(), Some("auction"));
    assert_eq!(t.request_count(), calls);
}

#[tokio::test]
async fn the_flood_429_is_rate_limited_too() {
    match scenario_client("rate-limited-flood")
        .list_peers(&PeerQuery::new())
        .await
        .unwrap_err()
    {
        Error::RateLimited {
            retry_after,
            limit,
            remaining,
            policy,
            signup_url,
            ..
        } => {
            assert_eq!(retry_after, Some(1));
            assert_eq!(limit, Some(100));
            assert_eq!(remaining, Some(0));
            assert_eq!(policy.as_deref(), Some("100;w=1"));
            assert_eq!(
                signup_url.as_deref(),
                Some("https://hypervisor.gg/docs/peer-api/authentication")
            );
        }
        other => panic!("expected RateLimited, got {other:?}"),
    }
}

#[tokio::test]
async fn mempool_verdicts_are_the_live_tokens() {
    let r = fixture_client()
        .list_peers(&PeerQuery::new())
        .await
        .unwrap();
    let verdicts: Vec<Option<&str>> = r
        .body
        .peers
        .iter()
        .map(|p| p.mempool.verdict.as_deref())
        .collect();
    assert!(verdicts.contains(&Some("serves")));
    assert!(verdicts.contains(&Some("connected_no_mempool")));
}

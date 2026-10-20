//! End-to-end against `tools/mock_server.py`, over a real socket.
//!
//! Feature-gated so the default `cargo test` run needs no Python and no
//! network:
//!
//!     cargo test --features mock-server-tests,cli --test mock_server

#![cfg(all(feature = "mock-server-tests", feature = "client"))]

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use hl_peers::{Client, Geo, IterOpts, PeerQuery, Sort};

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf()
}

fn free_port() -> u16 {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    l.local_addr().unwrap().port()
}

struct MockServer {
    child: Child,
    port: u16,
}

impl MockServer {
    fn start() -> MockServer {
        let port = free_port();
        let mut child = Command::new("python3")
            .arg(repo_root().join("tools/mock_server.py"))
            .arg("--port")
            .arg(port.to_string())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("python3 tools/mock_server.py");
        let deadline = Instant::now() + Duration::from_secs(15);
        while Instant::now() < deadline {
            if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                return MockServer { child, port };
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = child.kill();
        let _ = child.wait();
        panic!("the mock server did not come up on port {port}");
    }

    fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }
}

impl Drop for MockServer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn the_whole_surface_against_the_real_mock_server() {
    let server = MockServer::start();
    let client = Client::builder()
        .base_url(server.base_url())
        .build()
        .unwrap();

    // Owner queries.
    let q1 = client
        .list_peers(&PeerQuery::new().cc(["DE"]))
        .await
        .unwrap();
    assert_eq!(q1.peers.len(), 2);
    assert_eq!(q1.rate_limit.limit, Some(20));

    assert_eq!(
        client
            .list_peers(&PeerQuery::not_full(Geo::cc(["SG"])))
            .await
            .unwrap()
            .peers
            .len(),
        1
    );
    // The owner queries are single-page: every non-null next_cursor must be
    // served by another fixture, so these hand out none.
    let t1 = client.list_peers(&PeerQuery::tier1()).await.unwrap();
    assert_eq!(t1.total, Some(1));
    assert!(t1.next_cursor.is_none());
    let s4 = client.list_peers(&PeerQuery::sentries()).await.unwrap();
    assert_eq!(s4.total, Some(1));
    assert!(s4.next_cursor.is_none());

    // …so an iter_peers walk over one terminates instead of 404-ing.
    let walked = client
        .list_all_peers(&PeerQuery::tier1(), IterOpts::default())
        .await
        .unwrap();
    assert_eq!(walked.len(), 1);

    // Pagination over three real HTTP round trips.
    let peers = client
        .list_all_peers(
            &PeerQuery::new().limit(2).sort(Sort::Tier),
            IterOpts::default(),
        )
        .await
        .unwrap();
    assert_eq!(peers.len(), 5);

    // The ETag cache: the second identical call is a real 304.
    let first = client.list_peers(&PeerQuery::new()).await.unwrap();
    assert_eq!(first.status, 200);
    let second = client.list_peers(&PeerQuery::new()).await.unwrap();
    assert_eq!(second.status, 304);
    assert!(second.from_cache);
    assert_eq!(second.peers.len(), first.peers.len());

    // Single peer, and the 404 that must not collapse into the 400.
    assert_eq!(
        client.get_peer("192.0.2.10").await.unwrap().body.peer.id,
        "192.0.2.10"
    );
    assert_eq!(
        client.get_peer("198.51.100.254").await.unwrap_err().kind(),
        "NotFound"
    );

    // Facets.
    let f = client.facets(None).await.unwrap();
    assert_eq!(f.available("asn"), Some(false));

    // 400 filter-unavailable.
    let e = client
        .list_peers(&PeerQuery::new().asn(["AS24940"]))
        .await
        .unwrap_err();
    assert_eq!(e.kind(), "FilterUnavailable");

    // Owner query 1 whole, typed signals, API-Version, network map.
    let q1m = client
        .list_peers(&PeerQuery::mempool_servers().cc(["DE"]))
        .await
        .unwrap();
    assert_eq!(q1m.api_version.as_deref(), Some("1"));
    assert_eq!(q1m.peers[0].mempool.serves, Some(true));
    let g = client
        .network_map_graph(&hl_peers::NetworkMapOpts::new().max_tier(2))
        .await
        .unwrap();
    assert_eq!(g.body.nodes.len(), 4);
    let s = client
        .network_map_summary(&hl_peers::NetworkMapOpts::new())
        .await
        .unwrap();
    assert_eq!(s.body.connectable["yes"], 3);

    // format=gossip_config, straight into the offline editor.
    let cfg = client
        .list_peers_as_gossip_config(&PeerQuery::new().cc(["DE"]))
        .await
        .unwrap();
    assert!(cfg.body.validate().is_ok());
}

#[tokio::test(flavor = "multi_thread")]
async fn update_gossip_config_end_to_end() {
    let server = MockServer::start();
    let client = Client::builder()
        .base_url(server.base_url())
        .build()
        .unwrap();

    let dir = std::env::temp_dir().join(format!("hl-peers-mock-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("override_gossip_config.json");

    let res = hl_peers::update_gossip_config(
        &client,
        &PeerQuery::new().cc(["DE"]),
        Some(&path),
        &hl_peers::UpdateOpts::default(),
    )
    .await
    .unwrap();

    assert!(res.changed);
    assert_eq!(res.written_path.as_ref(), Some(&path));
    assert_eq!(res.config.root_ips(), vec!["192.0.2.10", "192.0.2.11"]);
    assert_eq!(std::fs::read_to_string(&path).unwrap(), res.json);

    // Idempotent: the same query again writes nothing.
    let again = hl_peers::update_gossip_config(
        &client,
        &PeerQuery::new().cc(["DE"]),
        Some(&path),
        &hl_peers::UpdateOpts::default(),
    )
    .await
    .unwrap();
    assert!(!again.changed);
    assert!(again.written_path.is_none());

    let _ = std::fs::remove_dir_all(&dir);
}

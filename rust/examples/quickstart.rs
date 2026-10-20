//! Every owner example query, against the fixture mock server.
//!
//!     python3 ../tools/mock_server.py --port 8931 &
//!     cargo run --example quickstart -- http://127.0.0.1:8931

use hl_peers::{Client, Geo, IterOpts, PeerQuery};

#[tokio::main(flavor = "current_thread")]
async fn main() -> hl_peers::Result<()> {
    let base = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "http://127.0.0.1:8931".to_string());
    let client = Client::builder().base_url(base).build()?;

    // Owner query 1, whole — serves the mempool AND is in DE. Peers whose
    // status is unknown (`serves: null`) are excluded: "unknown" is not "does not serve".
    let page = client
        .list_peers(&PeerQuery::mempool_servers().cc(["DE"]))
        .await?;
    println!(
        "q1 mempool cc=DE       -> {} peers (API-Version {:?})",
        page.peers.len(),
        page.api_version
    );
    for p in &page.peers {
        println!(
            "   {} serves={:?} auction={:?}",
            p.id,
            p.mempool.serves,
            p.auction.status.as_ref().map(|s| s.as_str())
        );
    }

    // Owner query 2 — "not full". An ESTIMATE of free inbound room, never a guarantee.
    let page = client
        .list_peers(&PeerQuery::not_full(Geo::cc(["SG"])))
        .await?;
    println!("q2 not_full cc=SG      -> {} peers", page.peers.len());

    // Owner query 3 — tier=1 is EXACT; max_tier=1 would also return tier 0.
    let page = client.list_peers(&PeerQuery::tier1()).await?;
    println!("q3 tier=1              -> {} peers", page.peers.len());

    // Owner query 4 — label=sentry, not tier=0.
    let page = client.list_peers(&PeerQuery::sentries()).await?;
    println!("q4 label=sentry        -> {} peers", page.peers.len());

    // The three-page cursor walk.
    let walked = client
        .list_all_peers(
            &PeerQuery::new().limit(2).sort(hl_peers::Sort::Tier),
            IterOpts::default(),
        )
        .await?;
    println!("pagination walk        -> {} peers", walked.len());

    // The server-rendered gossip config, parsed; write it with `save()`.
    let cfg = client
        .list_peers_as_gossip_config(&PeerQuery::new().cc(["DE"]))
        .await?;
    print!("{}", cfg.body.to_json());

    // The network map.
    let summary = client
        .network_map_summary(&hl_peers::NetworkMapOpts::new())
        .await?;
    println!(
        "network map            -> {:?} nodes, connectable {:?}",
        summary.body.node_count, summary.body.connectable
    );

    // Discovery: is an attribute even collected?
    let facets = client.facets(None).await?;
    println!(
        "facets: asn available? {:?} (false means NOT COLLECTED, not 'nothing matched')",
        facets.available("asn")
    );

    Ok(())
}

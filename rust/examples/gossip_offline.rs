//! Edit an `override_gossip_config.json` with no network at all.
//!
//! Runs under `--no-default-features`, which is the point: an operator on a box
//! with no API access can still use the config half of this crate.
//!
//!     cargo run --example gossip_offline --no-default-features

use hl_peers::{GossipConfig, MergeOpts};

fn main() -> hl_peers::Result<()> {
    // Build from a ranked list — the order is the API's ranking, never re-sorted.
    let opts = MergeOpts::default()
        .with_chain("Mainnet")
        .with_try_new_peers(true)
        .with_keep_existing(["192.0.2.10"]);
    let (mut cfg, warnings) =
        GossipConfig::from_peers(["198.51.100.7", "203.0.113.1", "203.0.113.2"], &opts)?;

    cfg.add_root_ip("203.0.113.9");
    cfg.remove_root_ip("203.0.113.1");
    cfg.add_reserved_peer_ip("198.51.100.20");
    cfg.set_n_gossip_peers(20)?;

    for w in warnings.iter().chain(cfg.validate()?.iter()) {
        eprintln!("warning: {w}");
    }

    // Print it and let the operator place it: the safest option, because this
    // crate will not guess where your node reads from.
    print!("{}", cfg.to_json());
    eprintln!(
        "\nThe node reads this file when it starts — placing it takes effect on the node's next start, which is yours to schedule."
    );
    Ok(())
}

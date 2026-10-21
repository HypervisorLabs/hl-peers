//! `hl-peers` — the optional CLI of `CLIENT_SPEC.md` §8.
//!
//! 🔴 It **never** restarts a node and has no `--restart` / `--apply` flag.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use clap::{Args, Parser, Subcommand};
use hl_peers::{
    BlockingClient, Error, Format, GossipConfig, IterOpts, MergeMode, MergeOpts, PeerQuery,
    SaveOpts, Sort, TierFilter, UpdateOpts, Warning, WarningCode,
};

#[derive(Parser, Debug)]
#[command(
    name = "hl-peers",
    about = "Query the Hyperliquid Peer API and maintain an hl-node override_gossip_config.json",
    version
)]
struct Cli {
    /// Base URL. Defaults to $HL_PEER_API_BASE_URL, then `https://api.hypervisor.gg`.
    #[arg(long, global = true)]
    base_url: Option<String>,
    /// API key. Defaults to $HL_PEER_API_KEY. Anonymous is a supported tier.
    #[arg(long, global = true)]
    api_key: Option<String>,
    /// Per-attempt timeout, seconds.
    #[arg(long, global = true, default_value_t = 10)]
    timeout: u64,
    /// Attempts after the first. 0 by default: auto-retry is off; a 429
    /// exits 3 with the reset time. Pass e.g. 3 to opt in.
    #[arg(long, global = true, default_value_t = 0)]
    max_retries: u32,
    /// Turn the ETag cache off.
    #[arg(long, global = true)]
    no_cache: bool,
    /// Suppress informational warnings. The root-count, truncation and
    /// exclude-beats-keep warnings are never suppressed.
    #[arg(long, global = true)]
    quiet: bool,
    /// Print the request plan to stderr.
    #[arg(long, global = true)]
    verbose: bool,
    /// Print errors as one JSON object on stderr.
    #[arg(long, global = true)]
    json_errors: bool,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Query peers.
    Query {
        #[command(flatten)]
        filters: Filters,
        /// Walk every page.
        #[arg(long)]
        all: bool,
        /// Hard guard on --all.
        #[arg(long, default_value_t = 10_000)]
        max_items: usize,
        /// json | table | ips
        #[arg(long, default_value = "json")]
        output: String,
    },
    /// Facet counts over the filtered set. Takes every filter flag, but not
    /// --sort, --limit, --fields or --format.
    Facets {
        #[command(flatten)]
        filters: Filters,
    },
    /// One peer's public dossier.
    Peer {
        /// The peer's IPv4.
        ip: String,
    },
    /// Work on an override_gossip_config.json.
    #[command(subcommand)]
    Gossip(GossipCmd),
}

// A clap subcommand enum is naturally wide and is constructed exactly once per
// process, so the variant-size spread is not worth a Box here.
#[allow(clippy::large_enum_variant)]
#[derive(Subcommand, Debug)]
enum GossipCmd {
    /// Print the file, re-rendered canonically.
    Show {
        #[arg(long)]
        path: PathBuf,
    },
    /// Validate the file and print its warnings.
    Validate {
        #[arg(long)]
        path: PathBuf,
    },
    /// Query the API and update the file's root_node_ips.
    Update {
        #[arg(long)]
        path: Option<PathBuf>,
        #[command(flatten)]
        filters: Filters,
        /// Hard cap on root_node_ips. 8 by default: a shorter root list starts the node faster.
        #[arg(long, default_value_t = 8)]
        max_roots: usize,
        /// replace | append | prepend
        #[arg(long, default_value = "replace")]
        mode: String,
        #[arg(long)]
        no_dedupe: bool,
        /// IPs to pin, CSV.
        #[arg(long)]
        keep: Option<String>,
        /// IPs never to include, CSV. Beats --keep.
        #[arg(long)]
        exclude: Option<String>,
        #[arg(long)]
        try_new_peers: bool,
        #[arg(long)]
        no_try_new_peers: bool,
        #[arg(long)]
        n_gossip_peers: Option<i64>,
        /// Permission bits for a newly created file, e.g. 0600. 🔴 `--file-mode`,
        /// not `--mode`: `--mode` is the merge strategy.
        #[arg(long, value_parser = parse_octal)]
        file_mode: Option<u32>,
        #[arg(long)]
        backup: bool,
        /// Print the unified diff and write nothing.
        #[arg(long)]
        dry_run: bool,
        /// Print the rendered JSON instead of writing.
        #[arg(long)]
        stdout: bool,
    },
}

#[derive(Args, Debug, Default)]
struct Filters {
    #[arg(long)]
    chain: Option<String>,
    #[arg(long)]
    cc: Option<String>,
    #[arg(long)]
    region: Option<String>,
    #[arg(long)]
    asn: Option<String>,
    /// Exact tier, CSV. `null` is a selectable member.
    #[arg(long)]
    tier: Option<String>,
    /// Tier ceiling. Not a synonym for --tier.
    #[arg(long)]
    max_tier: Option<i64>,
    #[arg(long)]
    role: Option<String>,
    #[arg(long)]
    label: Option<String>,
    /// yes | no | unknown, CSV.
    #[arg(long)]
    connectable: Option<String>,
    #[arg(long)]
    accepting: bool,
    /// responsive | open | full | syncing | unreachable | unknown, CSV.
    #[arg(long)]
    state: Option<String>,
    /// Country code, CSV. A server-side alias of --cc (both given ⇒ union).
    #[arg(long)]
    country: Option<String>,
    /// true | false | null, CSV. `null` = unknown, which is NOT `false`.
    #[arg(long)]
    serves_mempool: Option<String>,
    /// holds_slot | bidding | bid_before | null, CSV.
    #[arg(long)]
    auction: Option<String>,
    #[arg(long)]
    max_blocks_behind: Option<i64>,
    #[arg(long)]
    min_uptime: Option<f64>,
    #[arg(long)]
    max_latency_ms: Option<i64>,
    #[arg(long)]
    vantage: Option<String>,
    #[arg(long)]
    seen_within: Option<String>,
    #[arg(long)]
    version: Option<String>,
    #[arg(long)]
    sort: Option<String>,
    #[arg(long)]
    limit: Option<i64>,
    #[arg(long)]
    fields: Option<String>,
    /// json | gossip-config
    #[arg(long)]
    format: Option<String>,
}

fn parse_octal(s: &str) -> Result<u32, String> {
    let t = s.trim_start_matches("0o");
    u32::from_str_radix(t, 8).map_err(|_| format!("{s} is not octal permission bits"))
}

fn csv(v: &Option<String>) -> Vec<String> {
    v.as_deref()
        .map(|s| s.split(',').map(str::trim).map(str::to_string).collect())
        .unwrap_or_default()
}

impl Filters {
    fn to_query(&self) -> Result<PeerQuery, Error> {
        let mut q = PeerQuery::new()
            .cc(csv(&self.cc))
            .region(csv(&self.region))
            .asn(csv(&self.asn))
            .role(csv(&self.role))
            .label(csv(&self.label))
            .version(csv(&self.version))
            .fields(csv(&self.fields))
            .country(csv(&self.country));
        if let Some(c) = &self.chain {
            q = q.chain(hl_peers::Chain::parse(c)?);
        }
        if let Some(t) = &self.tier {
            let mut tiers = Vec::new();
            for part in t.split(',').map(str::trim) {
                tiers.push(if part == "null" {
                    TierFilter::Unplaced
                } else {
                    TierFilter::Exact(part.parse::<i64>().map_err(|_| {
                        Error::invalid("tier", format!("{part} is not an integer or `null`"))
                    })?)
                });
            }
            q = q.tier(tiers);
        }
        if let Some(v) = self.max_tier {
            q = q.max_tier(v);
        }
        if self.accepting {
            q = q.accepting(true);
        }
        for m in csv(&self.connectable) {
            q.connectable.push(hl_peers::Connectable::from_wire(&m));
        }
        for m in csv(&self.state) {
            q.state.push(hl_peers::PeerState::from_wire(&m));
        }
        let mut serves = Vec::new();
        for m in csv(&self.serves_mempool) {
            serves.push(match m.as_str() {
                "true" => Some(true),
                "false" => Some(false),
                "null" => None,
                other => {
                    return Err(Error::invalid(
                        "serves_mempool",
                        format!("{other} is not one of true, false, null"),
                    ))
                }
            });
        }
        q = q.serves_mempool(serves);
        // Closed-enum membership is checked by `PeerQuery::validate`.
        q = q.auction(
            csv(&self.auction)
                .iter()
                .map(|m| (m != "null").then(|| hl_peers::AuctionStatus::from_wire(m))),
        );
        if let Some(v) = self.max_blocks_behind {
            q = q.max_blocks_behind(v);
        }
        if let Some(v) = self.min_uptime {
            q = q.min_uptime(v);
        }
        if let Some(v) = self.max_latency_ms {
            q = q.max_latency_ms(v);
        }
        if let Some(v) = &self.vantage {
            q = q.vantage(v.clone());
        }
        if let Some(v) = &self.seen_within {
            q = q.seen_within(v.clone());
        }
        if let Some(v) = &self.sort {
            q = q.sort(Sort::parse(v)?);
        }
        if let Some(v) = self.limit {
            q = q.limit(v);
        }
        if let Some(v) = &self.format {
            q = q.format(Format::parse(v)?);
        }
        Ok(q)
    }
}

/// Warnings go to stderr, always. These four are never suppressed by --quiet.
fn always_loud(code: WarningCode) -> bool {
    matches!(
        code,
        WarningCode::ManyRootsGt24
            | WarningCode::ManyRootsGt64
            | WarningCode::Truncated
            | WarningCode::ExcludeOverridesKeepExisting
    )
}

fn print_warnings(warnings: &[Warning], quiet: bool) {
    for w in warnings {
        if !quiet || always_loud(w.code) {
            eprintln!("warning[{}]: {}", w.code, w.message);
        }
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(&cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            if cli.json_errors {
                let obj = serde_json::json!({
                    "kind": e.kind(),
                    "parameter": e.parameter(),
                    "message": e.to_string(),
                    "reason": e.reason(),
                });
                eprintln!("{obj}");
            } else {
                eprintln!("error[{}]: {e}", e.kind());
                if let Error::RateLimited {
                    signup_url: Some(url),
                    ..
                } = &e
                {
                    eprintln!("a key raises the limit: {url}");
                }
            }
            ExitCode::from(e.exit_code() as u8)
        }
    }
}

fn build_client(cli: &Cli) -> Result<BlockingClient, Error> {
    let mut b = BlockingClient::builder()
        .timeout(Duration::from_secs(cli.timeout))
        .max_retries(cli.max_retries)
        .cache_enabled(!cli.no_cache);
    if let Some(u) = &cli.base_url {
        b = b.base_url(u.clone());
    }
    if let Some(k) = &cli.api_key {
        b = b.api_key(k.clone());
    }
    b.build()
}

fn run(cli: &Cli) -> Result<(), Error> {
    match &cli.command {
        Command::Query {
            filters,
            all,
            max_items,
            output,
        } => {
            let query = filters.to_query()?;
            let client = build_client(cli)?;
            if cli.verbose {
                eprintln!(
                    "GET {}/peers?{}",
                    client.async_client().base_path(),
                    query.to_query_string()?
                );
            }
            if query.format == Some(Format::GossipConfig) {
                let cfg = client.list_peers_as_gossip_config(&query)?;
                print!("{}", cfg.body.to_json());
                return Ok(());
            }
            if *all {
                let peers = client
                    .list_all_peers(&query, IterOpts::default().with_max_items(*max_items))?;
                render(&peers, output)?;
            } else {
                let page = client.list_peers(&query)?;
                if !page.body.envelope.available {
                    eprintln!(
                        "warning: available:false — the producer could not build a graph. The empty list is NOT a finding about the network."
                    );
                }
                render(&page.body.peers, output)?;
            }
            Ok(())
        }
        Command::Facets { filters } => {
            let query = filters.to_query()?;
            let client = build_client(cli)?;
            let f = client.facets(Some(&query))?;
            println!("{}", serde_json::to_string_pretty(&f.body).unwrap());
            Ok(())
        }
        Command::Peer { ip } => {
            let client = build_client(cli)?;
            let p = client.get_peer(ip)?;
            println!("{}", serde_json::to_string_pretty(&p.body).unwrap());
            Ok(())
        }
        Command::Gossip(GossipCmd::Show { path }) => {
            print!("{}", GossipConfig::load(path)?.to_json());
            Ok(())
        }
        Command::Gossip(GossipCmd::Validate { path }) => {
            let warnings = GossipConfig::load(path)?.validate()?;
            print_warnings(&warnings, cli.quiet);
            println!("ok");
            Ok(())
        }
        Command::Gossip(GossipCmd::Update {
            path,
            filters,
            max_roots,
            mode,
            no_dedupe,
            keep,
            exclude,
            try_new_peers,
            no_try_new_peers,
            n_gossip_peers,
            file_mode,
            backup,
            dry_run,
            stdout,
        }) => {
            let mut merge = MergeOpts::default()
                .with_max_roots(*max_roots)
                .with_mode(MergeMode::parse(mode)?)
                .with_dedupe(!*no_dedupe)
                .with_keep_existing(csv(keep))
                .with_exclude(csv(exclude));
            // One `--chain`, from the shared filter flags: it selects the chain
            // to query AND the chain written into the config. They must agree —
            // a config whose chain disagrees with the node's --chain stops the node from starting.
            if let Some(c) = &filters.chain {
                merge = merge.with_chain(c.clone());
            }
            if *try_new_peers {
                merge = merge.with_try_new_peers(true);
            }
            if *no_try_new_peers {
                merge = merge.with_try_new_peers(false);
            }

            let opts = UpdateOpts::default()
                .with_merge(merge)
                .with_save({
                    let mut so = SaveOpts::default()
                        .with_backup(*backup)
                        .with_dry_run(*dry_run);
                    if let Some(m) = file_mode {
                        so = so.with_file_mode(*m);
                    }
                    so
                })
                .with_no_write(*stdout);

            let client = build_client(cli)?;
            let query = filters.to_query()?;
            let mut res =
                client.update_gossip_config(&query, path.as_deref().filter(|_| !*stdout), &opts)?;

            if let Some(n) = n_gossip_peers {
                res.config.set_n_gossip_peers(*n)?;
                res.json = res.config.to_json();
            }

            print_warnings(&res.warnings, cli.quiet);

            if *stdout {
                print!("{}", res.json);
            } else if *dry_run {
                print!("{}", res.diff);
            } else if let Some(p) = &res.written_path {
                eprintln!(
                    "wrote {}. The node reads this file when it starts — the change takes effect on the node's next start, which is yours to schedule.",
                    p.display()
                );
            } else {
                eprintln!("no change; nothing written.");
            }
            Ok(())
        }
    }
}

fn render(peers: &[hl_peers::Peer], output: &str) -> Result<(), Error> {
    match output {
        "json" => println!("{}", serde_json::to_string_pretty(peers).unwrap()),
        "ips" => {
            for p in peers {
                println!("{}", p.id);
            }
        }
        "table" => {
            println!(
                "{:<16} {:>4}  {:<3} {:<8} {:<7} {:<10}",
                "ip", "tier", "cc", "conn", "mempool", "auction"
            );
            for p in peers {
                println!(
                    "{:<16} {:>4}  {:<3} {:<8} {:<7} {:<10}",
                    p.id,
                    p.tier.map(|t| t.to_string()).unwrap_or_else(|| "-".into()),
                    p.cc.as_deref().unwrap_or("-"),
                    p.connectable.as_ref().map(|c| c.as_str()).unwrap_or("-"),
                    // `?` = unknown, which is not `no`.
                    match p.mempool.serves {
                        Some(true) => "yes",
                        Some(false) => "no",
                        None => "?",
                    },
                    p.auction.status.as_ref().map(|a| a.as_str()).unwrap_or("-"),
                );
            }
        }
        other => {
            return Err(Error::invalid(
                "output",
                format!("{other} is not one of json, table, ips"),
            ))
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::Cli;
    use clap::CommandFactory;

    /// clap's own consistency check: duplicate argument names, bad defaults and
    /// the like. It is a debug assertion, so nothing but a test catches it.
    #[test]
    fn the_cli_definition_is_well_formed() {
        Cli::command().debug_assert();
    }
}

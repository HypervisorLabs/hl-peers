"""``hl-peers`` — the CLI. argparse only, no dependencies.

The flags are those of CLIENT_SPEC §8, identically across the four languages, so
one runbook works against any of them.

🔴 This CLI **never** restarts a node and has no ``--restart`` / ``--apply``
flag. After a successful write it says so on stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO

from . import __version__
from .client import PeerApiClient
from .errors import (
    BadRequest,
    HlPeersError,
    Invalid,
    NotFound,
    RateLimited,
    ServerError,
    TransportError,
    Unauthorized,
    Unavailable,
)
from .gossip import ConfigWarning, GossipConfig, default_config_path
from .models import NotModified
from .query import PeerQuery
from .update import update_gossip_config

__all__ = ["main"]

WROTE_NOTE = (
    "wrote %s. The node reads this file when it starts — the change takes "
    "effect on the node's next start, which is yours to schedule."
)

#: Never suppressed, not even under --quiet.
LOUD_WARNINGS = (
    "many_roots_gt_24",
    "many_roots_gt_64",
    "truncated",
    "exclude_overrides_keep_existing",
)

EXIT_OK = 0
EXIT_CLIENT_ERROR = 1
EXIT_USAGE = 2
EXIT_RATE_LIMITED = 3
EXIT_UNAUTHORIZED = 4
EXIT_NOT_FOUND = 5
EXIT_UNAVAILABLE = 6


def exit_code_for(error: HlPeersError) -> int:
    if isinstance(error, RateLimited):
        return EXIT_RATE_LIMITED
    if isinstance(error, Unauthorized):
        return EXIT_UNAUTHORIZED
    if isinstance(error, NotFound):
        return EXIT_NOT_FOUND
    if isinstance(error, (Unavailable, ServerError, TransportError)):
        return EXIT_UNAVAILABLE
    if isinstance(error, (Invalid, BadRequest)):
        return EXIT_CLIENT_ERROR
    return EXIT_CLIENT_ERROR


# ---------------------------------------------------------------------------
# argument plumbing
# ---------------------------------------------------------------------------
def _global_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--base-url", default=argparse.SUPPRESS)
    parser.add_argument("--api-key", default=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--max-retries", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--no-cache", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--json-errors", action="store_true", default=argparse.SUPPRESS)
    return parser


def _octal(value: str) -> int:
    """``--file-mode 0600`` is octal, the way chmod is written."""
    try:
        return int(value, 8)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not octal permission bits" % value) from None


def _add_network_flag(parser: argparse.ArgumentParser) -> None:
    """``--network mainnet|testnet`` (testnet is beta). Unset sends nothing (mainnet).
    Checked by the client, so an out-of-enum value is an ``Invalid`` like ``--chain``."""
    parser.add_argument(
        "--network",
        default=argparse.SUPPRESS,
        help="mainnet|testnet (testnet is beta); unset = mainnet",
    )


def _add_filter_flags(parser: argparse.ArgumentParser) -> None:
    """Every filter of CLIENT_SPEC §2.1 — shared by ``query``, ``facets`` and
    ``gossip update``."""
    parser.add_argument(
        "--chain", default=argparse.SUPPRESS, help="Mainnet|Testnet; only Mainnet is served"
    )
    _add_network_flag(parser)
    parser.add_argument("--cc", default=argparse.SUPPRESS, help="ISO-3166-1 alpha-2, CSV")
    parser.add_argument(
        "--country", default=argparse.SUPPRESS, help="alias of --cc server-side (union), CSV"
    )
    parser.add_argument(
        "--region", default=argparse.SUPPRESS, help="provider:region; cloud-hosted peers only"
    )
    parser.add_argument("--asn", default=argparse.SUPPRESS)
    parser.add_argument("--tier", default=argparse.SUPPRESS, help="EXACT tier, CSV; 'null' allowed")
    parser.add_argument("--max-tier", type=int, default=argparse.SUPPRESS, help="tier ceiling")
    parser.add_argument("--role", default=argparse.SUPPRESS)
    parser.add_argument("--label", default=argparse.SUPPRESS)
    parser.add_argument("--connectable", default=argparse.SUPPRESS)
    parser.add_argument("--accepting", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--state", default=argparse.SUPPRESS)
    parser.add_argument("--max-blocks-behind", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--min-uptime", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--max-latency-ms", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--vantage", default=argparse.SUPPRESS)
    parser.add_argument("--seen-within", default=argparse.SUPPRESS)
    parser.add_argument("--version", dest="node_version", default=argparse.SUPPRESS)
    parser.add_argument(
        "--serves-mempool",
        default=argparse.SUPPRESS,
        help="CSV of true,false,null; null = unknown (NOT false)",
    )
    parser.add_argument(
        "--auction",
        default=argparse.SUPPRESS,
        help="CSV of holds_slot,bidding,bid_before,null",
    )


def _add_query_flags(parser: argparse.ArgumentParser) -> None:
    _add_filter_flags(parser)
    parser.add_argument("--sort", default=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--fields", default=argparse.SUPPRESS)
    parser.add_argument("--format", dest="wire_format", default=argparse.SUPPRESS)


def build_query(args: argparse.Namespace) -> PeerQuery:
    query = PeerQuery()

    def get(name: str) -> Any:
        return getattr(args, name, None)

    if get("chain"):
        query = query.chain(args.chain)
    if get("network") is not None:
        query = query.network(args.network)

    if get("cc"):
        query = query.cc(args.cc)
    if get("country"):
        query = query.country(args.country)
    if get("region"):
        query = query.region(args.region)
    if get("asn"):
        query = query.asn(args.asn)
    if get("tier"):
        query = query.tier(args.tier)
    if get("max_tier") is not None:
        query = query.max_tier(args.max_tier)
    if get("role"):
        query = query.role(args.role)
    if get("label"):
        query = query.label(args.label)
    if get("connectable"):
        query = query.connectable(args.connectable)
    if get("accepting"):
        query = query.accepting(True)
    if get("state"):
        query = query.state(args.state)
    if get("max_blocks_behind") is not None:
        query = query.max_blocks_behind(args.max_blocks_behind)
    if get("min_uptime") is not None:
        query = query.min_uptime(args.min_uptime)
    if get("max_latency_ms") is not None:
        query = query.max_latency_ms(args.max_latency_ms)
    if get("vantage"):
        query = query.vantage(args.vantage)
    if get("seen_within"):
        query = query.seen_within(args.seen_within)
    if get("node_version"):
        query = query.version(args.node_version)
    if get("serves_mempool"):
        query = query.serves_mempool(args.serves_mempool)
    if get("auction"):
        query = query.auction(args.auction)
    if get("sort"):
        query = query.sort(args.sort)
    if get("limit") is not None:
        query = query.limit(args.limit)
    if get("fields"):
        query = query.fields(args.fields)
    if get("wire_format"):
        query = query.format(args.wire_format.replace("-", "_"))
    return query


def build_parser() -> argparse.ArgumentParser:
    globals_parser = _global_parser()
    parser = argparse.ArgumentParser(
        prog="hl-peers",
        parents=[globals_parser],
        description="Query the Hyperliquid Peer API and maintain override_gossip_config.json. "
        "Never restarts a node.",
    )
    parser.add_argument("--version", action="version", version="hl-peers %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    query_cmd = sub.add_parser("query", parents=[globals_parser], help="list peers")
    _add_query_flags(query_cmd)
    query_cmd.add_argument("--all", action="store_true", help="walk every page")
    query_cmd.add_argument("--max-items", type=int, default=10_000)
    query_cmd.add_argument("--output", choices=("json", "table", "ips"), default="json")

    facets_cmd = sub.add_parser("facets", parents=[globals_parser], help="what is filterable")
    _add_filter_flags(facets_cmd)

    peer_cmd = sub.add_parser("peer", parents=[globals_parser], help="one peer's dossier")
    peer_cmd.add_argument("ip")
    _add_network_flag(peer_cmd)

    gossip = sub.add_parser("gossip", parents=[globals_parser], help="the override file")
    gsub = gossip.add_subparsers(dest="gossip_command")

    show = gsub.add_parser("show", parents=[globals_parser])
    show.add_argument("--path", default=default_config_path())

    validate = gsub.add_parser("validate", parents=[globals_parser])
    validate.add_argument("--path", default=default_config_path())

    for name in ("add-root", "remove-root"):
        root_cmd = gsub.add_parser(name, parents=[globals_parser])
        root_cmd.add_argument("ip")
        root_cmd.add_argument("--path", default=default_config_path())
        root_cmd.add_argument("--backup", action="store_true")
        root_cmd.add_argument("--file-mode", type=_octal, default=None)
        root_cmd.add_argument("--dry-run", action="store_true")
        root_cmd.add_argument("--stdout", action="store_true")

    update = gsub.add_parser("update", parents=[globals_parser])
    _add_query_flags(update)
    update.add_argument("--path", default=None)
    update.add_argument("--max-roots", type=int, default=8)
    update.add_argument("--mode", choices=("replace", "append", "prepend"), default="replace")
    update.add_argument("--no-dedupe", action="store_true")
    update.add_argument("--keep", default="")
    update.add_argument("--exclude", default="")
    update.add_argument("--try-new-peers", dest="try_new_peers", action="store_true", default=None)
    update.add_argument("--no-try-new-peers", dest="try_new_peers", action="store_false")
    update.add_argument("--n-gossip-peers", type=int, default=None)
    update.add_argument("--file-mode", type=_octal, default=None)
    update.add_argument("--backup", action="store_true")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--stdout", action="store_true")
    return parser


def make_client(args: argparse.Namespace) -> PeerApiClient:
    from .cache import EtagCache

    return PeerApiClient(
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
        timeout=getattr(args, "timeout", 10.0),
        # Retries are OFF by default (CLIENT_SPEC §5.2); --max-retries opts in.
        max_retries=getattr(args, "max_retries", 0),
        cache=None if not getattr(args, "no_cache", False) else EtagCache(capacity=0),
    )


def print_warnings(
    warnings: Sequence[ConfigWarning], args: argparse.Namespace, err: TextIO
) -> None:
    quiet = getattr(args, "quiet", False)
    for warning in warnings:
        if quiet and warning.code not in LOUD_WARNINGS:
            continue
        err.write("warning: %s\n" % warning)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def _cmd_query(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    client = make_client(args)
    query = build_query(args)
    if query.params.get("format") == "gossip_config":
        # The server's own override file, re-rendered canonically. Printed, not
        # written: place it yourself, or use `gossip update`.
        out.write(client.list_peers_as_gossip_config(query).to_json())
        return EXIT_OK
    if args.all:
        iterator = client.iter_peers(query, max_items=args.max_items)
        peers = list(iterator)
        page = iterator.page
        if iterator.truncated:
            err.write("warning: stopped at --max-items %d\n" % args.max_items)
    else:
        result = client.list_peers(query)
        if isinstance(result, NotModified):  # pragma: no cover - only with a caller ETag
            out.write("304 Not Modified\n")
            return EXIT_OK
        page, peers = result, result.peers

    if page is not None and not page.available:
        err.write(
            "warning: available:false — the producer could not build a graph. The empty "
            "list is NOT a finding about the network.\n"
        )
    if page is not None and page.stale:
        err.write("warning: stale:true — the snapshot is older than the staleness bound.\n")

    if args.output == "ips":
        for peer in peers:
            out.write("%s\n" % peer.id)
    elif args.output == "table":
        out.write("%-16s %5s %4s %-10s %s\n" % ("ip", "tier", "cc", "connect", "name"))
        for peer in peers:
            out.write(
                "%-16s %5s %4s %-10s %s\n"
                % (
                    peer.id,
                    "null" if peer.tier is None else peer.tier,
                    peer.cc or "-",
                    peer.connectable or "-",
                    peer.name or "",
                )
            )
    else:
        body = dict(page.raw) if page is not None else {}
        body["peers"] = [p.raw if hasattr(p, "raw") else _peer_dict(p) for p in peers]
        out.write(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    return EXIT_OK


def _peer_dict(peer: Any) -> Dict[str, Any]:
    from dataclasses import asdict

    data = asdict(peer)
    data.pop("extra", None)
    data.update(getattr(peer, "extra", {}) or {})
    return data


def _cmd_facets(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    client = make_client(args)
    facets = client.facets(build_query(args))
    out.write(json.dumps(facets.raw, indent=2, ensure_ascii=False) + "\n")
    return EXIT_OK


def _cmd_peer(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    client = make_client(args)
    detail = client.get_peer(args.ip, network=getattr(args, "network", None))
    out.write(json.dumps(detail.raw, indent=2, ensure_ascii=False) + "\n")
    return EXIT_OK


def _cmd_gossip_show(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    out.write(GossipConfig.load(args.path).to_json())
    return EXIT_OK


def _cmd_gossip_validate(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    config = GossipConfig.load(args.path)
    warnings = config.validate()
    print_warnings(warnings, args, err)
    out.write("ok: %s (%d roots)\n" % (args.path, len(config.root_ips())))
    return EXIT_OK


def _cmd_gossip_root(args: argparse.Namespace, out: TextIO, err: TextIO, add: bool) -> int:
    config = GossipConfig.load(args.path)
    config.add_root_ip(args.ip) if add else config.remove_root_ip(args.ip)
    if args.stdout:
        out.write(config.to_json())
        return EXIT_OK
    result = config.save(
        args.path, dry_run=args.dry_run, backup=args.backup, file_mode=args.file_mode
    )
    print_warnings(result.warnings, args, err)
    if args.dry_run:
        out.write(result.diff)
    elif result.written:
        err.write(WROTE_NOTE % args.path + "\n")
    else:
        err.write("no change: %s\n" % args.path)
    return EXIT_OK


def _cmd_gossip_update(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    client = make_client(args)
    query = build_query(args)
    result = update_gossip_config(
        client,
        query,
        None if args.stdout else args.path,
        max_roots=args.max_roots,
        mode=args.mode,
        dedupe=not args.no_dedupe,
        keep_existing=[x for x in args.keep.split(",") if x],
        exclude=[x for x in args.exclude.split(",") if x],
        chain=getattr(args, "chain", None),
        try_new_peers=args.try_new_peers,
        n_gossip_peers=args.n_gossip_peers,
        backup=args.backup,
        file_mode=args.file_mode,
        dry_run=args.dry_run,
    )
    print_warnings(result.warnings, args, err)
    if args.stdout:
        out.write(result.json)
    elif args.dry_run:
        out.write(result.diff)
    elif result.written_path:
        err.write(WROTE_NOTE % result.written_path + "\n")
    else:
        err.write("no change: %s\n" % args.path)
    return EXIT_OK


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main(
    argv: Optional[List[str]] = None,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", None)
    if command is None:
        parser.print_help(err)
        return EXIT_USAGE

    handlers: Dict[str, Callable[..., int]] = {
        "query": _cmd_query,
        "facets": _cmd_facets,
        "peer": _cmd_peer,
    }
    handler: Optional[Callable[..., int]] = handlers.get(command)
    if command == "gossip":
        gossip_command = getattr(args, "gossip_command", None)
        if gossip_command is None:
            parser.print_help(err)
            return EXIT_USAGE
        gossip_handlers: Dict[str, Callable[..., int]] = {
            "show": _cmd_gossip_show,
            "validate": _cmd_gossip_validate,
            "update": _cmd_gossip_update,
            "add-root": lambda a, o, e: _cmd_gossip_root(a, o, e, True),
            "remove-root": lambda a, o, e: _cmd_gossip_root(a, o, e, False),
        }
        handler = gossip_handlers.get(gossip_command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.print_help(err)
        return EXIT_USAGE

    try:
        return handler(args, out, err)
    except HlPeersError as exc:
        if getattr(args, "json_errors", False):
            err.write(
                json.dumps(
                    {
                        "kind": exc.kind,
                        "message": exc.message,
                        "parameter": getattr(exc, "parameter", None),
                        "problem": exc.problem,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        else:
            err.write("error [%s]: %s\n" % (exc.kind, exc.message))
        return exit_code_for(exc)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

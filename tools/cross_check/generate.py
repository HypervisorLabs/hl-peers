#!/usr/bin/env python3
"""Generate the cross-client differential programme set.

Pure stdlib, seeded and deterministic: the same ``--seed`` always produces the
same programmes, so a disagreement found in CI is reproducible by hand.

Three families of request are emitted, all as NDJSON:

``kind: "gossip"``
    A ``GossipConfig`` programme — a starting point plus an ordered list of
    operations, exactly the shape ``fixtures/gossip/cases`` uses. Every case in
    that directory is replayed, and then several hundred randomised programmes
    on top.

``kind: "query_url"``
    A query builder's fields; the harness answers with the canonical request
    URL (``CLIENT_SPEC.md`` §2.3) or the client-side ``Invalid`` it raised.

``kind: "http"``
    One request against ``tools/mock_server.py``; the harness answers with the
    decoded envelope, the peer ids, the rate-limit block and the error kind.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

REPO = Path(__file__).resolve().parents[2]
CASES = REPO / "fixtures" / "gossip" / "cases"
HTTP_FIXTURES = REPO / "fixtures" / "http"

# RFC 5737 documentation space only. tools/check_fixtures.py enforces the same
# rule on the fixtures; the generator must never widen it.
VALID_IPS = (
    ["192.0.2.%d" % n for n in range(1, 90)]
    + ["198.51.100.%d" % n for n in range(1, 90)]
    + ["203.0.113.%d" % n for n in range(1, 90)]
)

INVALID_IPS = [
    "192.0.2.256",
    "192.0.2",
    "192.0.2.1.5",
    "1.2.3.4.5",
    "",
    " 192.0.2.1",
    "192.0.2.1 ",
    "192.0.2.-1",
    "192.0.2.1/32",
    "192.0.2.1:8080",
    "example.invalid",
    "0x7f.0.0.1",
]

IPV6 = ["::1", "2001:db8::1", "fe80::1%eth0", "::ffff:192.0.2.1", "[2001:db8::1]"]

BOUNDARY_SIZES = [1, 8, 24, 25, 64, 65]

UNKNOWN_KEY_BLOBS = [
    {"zz_future": 1},
    {"aaa": [3, 1, 2], "zzz": {"b": 2, "a": 1}},
    {"note": "Betreiber — Köln ✓", "Ip": "not a root"},
    {"nested": {"deep": {"z": 1, "a": [{"b": 2, "a": 1}]}}},
    {"": "empty key", "é": "accent", "0": "digit"},
    {"float_like": 1.5, "big": 9007199254740991},
]


def _roots(ips: List[str]) -> List[Dict[str, str]]:
    return [{"Ip": ip} for ip in ips]


def base_configs(rng: random.Random) -> List[Optional[Dict[str, Any]]]:
    """A spread of starting configs, including the boundary root counts."""
    out: List[Optional[Dict[str, Any]]] = [None]
    for size in BOUNDARY_SIZES:
        out.append(
            {
                "chain": "Mainnet",
                "root_node_ips": _roots(VALID_IPS[:size]),
                "try_new_peers": True,
            }
        )
    out.append({"root_node_ips": _roots(VALID_IPS[:2])})  # chain absent
    out.append(
        {
            "chain": "Testnet",
            "root_node_ips": _roots(VALID_IPS[:3]),
            "try_new_peers": False,
            "reserved_peer_ips": VALID_IPS[100:103],
            "n_gossip_peers": 20,
            "split_client_blocks": True,
        }
    )
    for blob in UNKNOWN_KEY_BLOBS:
        cfg: Dict[str, Any] = {
            "chain": "Mainnet",
            "root_node_ips": _roots(rng.sample(VALID_IPS, 3)),
            "try_new_peers": True,
        }
        cfg.update(blob)
        out.append(cfg)
    return out


# Odd-but-valid input JSON: whitespace, key order, escapes. Every one of these
# must parse to the same logical config and re-render to the same bytes.
ODD_JSON_TEXTS = [
    '{"root_node_ips":[{"Ip":"192.0.2.10"}],"chain":"Mainnet"}',
    '{\n\t"try_new_peers"  :   true ,\n  "root_node_ips" : [ { "Ip" : "192.0.2.10" } ] ,'
    '\n "chain":"Mainnet"\n}\n',
    '   {"chain":"Testnet","root_node_ips":[{"Ip":"198.51.100.1"},{"Ip":"203.0.113.1"}],'
    '"reserved_peer_ips":["192.0.2.1"]}   ',
    '{"chain":"Mainnet","root_node_ips":[{"Ip":"192.0.2.10"}],"note":"a \\u00e9 b \\/ c"}',
    '{"chain":"Mainnet","root_node_ips":[{"Ip":"192.0.2.10"}],"n_gossip_peers":8,'
    '"split_client_blocks":false}',
    '{"root_node_ips":[{"Ip":"192.0.2.10"}],"zzz":1,"aaa":2,"chain":"Mainnet",'
    '"try_new_peers":true}',
    '{"chain":"Mainnet","root_node_ips":[{"Ip":"192.0.2.10"}],"emoji":"\\ud83d\\udd34 \\u00fc"}',
]

MALFORMED_JSON_TEXTS = [
    "{",
    "[]",
    '"a string"',
    "null",
    "42",
    '{"root_node_ips": 5}',
    '{"root_node_ips": [{"ip": "192.0.2.1"}]}',
    '{"root_node_ips": ["192.0.2.1"]}',
    '{"root_node_ips": [{"Ip": "192.0.2.1", "port": 8080}]}',
    '{"chain": "mainnet", "root_node_ips": [{"Ip": "192.0.2.1"}]}',
    '{"root_node_ips": []}',
    '{"root_node_ips": [{"Ip": "192.0.2.1"}], "n_gossip_peers": 7}',
    '{"root_node_ips": [{"Ip": "192.0.2.1"}], "try_new_peers": "yes"}',
]

MERGE_MODES = ["replace", "append", "prepend"]


def random_merge_opts(rng: random.Random) -> Dict[str, Any]:
    opts: Dict[str, Any] = {}
    if rng.random() < 0.7:
        opts["max_roots"] = rng.choice([1, 2, 8, 24, 25, 64, 65])
    if rng.random() < 0.7:
        opts["mode"] = rng.choice(MERGE_MODES)
    if rng.random() < 0.3:
        opts["dedupe"] = rng.choice([True, False])
    if rng.random() < 0.4:
        opts["keep_existing"] = rng.sample(VALID_IPS, rng.randint(1, 4))
    if rng.random() < 0.4:
        opts["exclude"] = rng.sample(VALID_IPS, rng.randint(1, 4))
    if rng.random() < 0.3:
        opts["chain"] = rng.choice(["Mainnet", "Testnet"])
    if rng.random() < 0.3:
        opts["try_new_peers"] = rng.choice([True, False])
    return opts


def random_peers(rng: random.Random) -> List[Any]:
    n = rng.choice([0, 1, 2, 3, 5, 9, 26, 66])
    pool = VALID_IPS
    peers: List[Any] = []
    for _ in range(n):
        roll = rng.random()
        if roll < 0.06:
            peers.append(rng.choice(INVALID_IPS))
        elif roll < 0.09:
            peers.append(rng.choice(IPV6))
        elif roll < 0.35:
            peers.append({"id": rng.choice(pool), "tier": rng.choice([None, 0, 1, 2])})
        else:
            peers.append(rng.choice(pool))
    # Deliberately seed repeats so `dedupe` has something to do.
    if peers and rng.random() < 0.4:
        peers.append(peers[0])
    return peers


def random_op(rng: random.Random, tmp_slot: int) -> Dict[str, Any]:
    op = rng.choice(
        [
            "add_root_ip",
            "remove_root_ip",
            "set_root_ips",
            "add_reserved_peer_ip",
            "remove_reserved_peer_ip",
            "set_reserved_peer_ips",
            "set_chain",
            "set_try_new_peers",
            "set_n_gossip_peers",
            "set_split_client_blocks",
            "unset_split_client_blocks",
            "merge_peers",
            "to_json",
            "validate",
            "root_ips",
            "reserved_peer_ips",
            "has_root_ip",
            "save",
        ]
    )

    def ip() -> str:
        roll = rng.random()
        if roll < 0.08:
            return rng.choice(INVALID_IPS)
        if roll < 0.12:
            return rng.choice(IPV6)
        return rng.choice(VALID_IPS)

    if op in ("add_root_ip", "remove_root_ip", "has_root_ip"):
        return {"op": op, "args": [ip()]}
    if op in ("add_reserved_peer_ip", "remove_reserved_peer_ip"):
        return {"op": op, "args": [ip()]}
    if op in ("set_root_ips", "set_reserved_peer_ips"):
        size = rng.choice(BOUNDARY_SIZES + [0, 2, 3])
        ips = [ip() for _ in range(size)]
        if ips and rng.random() < 0.3:
            ips.append(ips[0])
        return {"op": op, "args": [ips]}
    if op == "set_chain":
        return {"op": op, "args": [rng.choice(["Mainnet", "Testnet", "mainnet", "MAINNET", "Foo"])]}
    if op == "set_try_new_peers":
        return {"op": op, "args": [rng.choice([True, False])]}
    if op == "set_n_gossip_peers":
        return {"op": op, "args": [rng.choice([7, 8, 9, 50, 100, 101, 0, -1])]}
    if op == "set_split_client_blocks":
        return {"op": op, "args": [rng.choice([True, False])]}
    if op == "merge_peers":
        return {"op": op, "args": [random_peers(rng)], "opts": random_merge_opts(rng)}
    if op == "save":
        opts: Dict[str, Any] = {}
        if rng.random() < 0.4:
            opts["dry_run"] = True
        if rng.random() < 0.2:
            opts["backup"] = True
        return {"op": op, "args": ["$TMPDIR/override_gossip_config.json"], "opts": opts}
    return {"op": op, "args": []}


def gossip_programmes(rng: random.Random, count: int) -> Iterator[Dict[str, Any]]:
    # 1. Every shared fixture case, replayed through the differential harness.
    for path in sorted(CASES.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        prog: Dict[str, Any] = {
            "id": "case:%s" % path.stem,
            "kind": "gossip",
            "operations": case.get("operations", []),
        }
        if "input_json_text" in case:
            prog["input_json_text"] = case["input_json_text"]
        else:
            prog["input_config"] = case.get("input_config")
        yield prog

    # 2. Odd-but-valid and malformed raw input text.
    for index, text in enumerate(ODD_JSON_TEXTS):
        yield {
            "id": "odd_json:%d" % index,
            "kind": "gossip",
            "input_json_text": text,
            "operations": [{"op": "to_json", "args": []}],
        }
    for index, text in enumerate(MALFORMED_JSON_TEXTS):
        yield {
            "id": "malformed_json:%d" % index,
            "kind": "gossip",
            "input_json_text": text,
            "operations": [{"op": "validate", "args": []}],
        }

    # 3. Boundary-sized root lists, on their own.
    for size in BOUNDARY_SIZES + [0]:
        yield {
            "id": "boundary_roots:%d" % size,
            "kind": "gossip",
            "input_config": None,
            "operations": [
                {"op": "new", "args": ["Mainnet", VALID_IPS[:size]]},
                {"op": "validate", "args": []},
            ],
        }
        yield {
            "id": "boundary_merge:%d" % size,
            "kind": "gossip",
            "input_config": None,
            "operations": [
                {
                    "op": "from_peers",
                    "args": [VALID_IPS[:size]],
                    "opts": {"chain": "Mainnet", "max_roots": size or 1},
                }
            ],
        }

    # 4. Explicitly invalid / IPv6 / zero-root attempts.
    for index, bad in enumerate(INVALID_IPS + IPV6):
        yield {
            "id": "bad_ip:%d" % index,
            "kind": "gossip",
            "input_config": None,
            "operations": [
                {"op": "from_peers", "args": [[bad]], "opts": {"chain": "Mainnet"}},
            ],
        }
        yield {
            "id": "bad_ip_root:%d" % index,
            "kind": "gossip",
            "input_config": {"chain": "Mainnet", "root_node_ips": _roots(["192.0.2.1"])},
            "operations": [
                {"op": "add_root_ip", "args": [bad]},
                {"op": "validate", "args": []},
            ],
        }

    for index, opts in enumerate(
        [
            {"chain": "Mainnet"},
            {"chain": "Mainnet", "keep_existing": []},
            {"chain": "Mainnet", "exclude": ["192.0.2.1"]},
        ]
    ):
        yield {
            "id": "zero_roots:%d" % index,
            "kind": "gossip",
            "input_config": None,
            "operations": [{"op": "from_peers", "args": [["192.0.2.1"]], "opts": opts}],
        }

    # 5. Randomised operation sequences.
    bases = base_configs(rng)
    for n in range(count):
        base = rng.choice(bases)
        ops: List[Dict[str, Any]] = []
        if base is None:
            if rng.random() < 0.5:
                size = rng.choice([0, 1, 3, 8, 25])
                ops.append({"op": "new", "args": [rng.choice(["Mainnet", "Testnet"]), VALID_IPS[:size]]})
            else:
                ops.append(
                    {
                        "op": "from_peers",
                        "args": [random_peers(rng)],
                        "opts": dict(random_merge_opts(rng), chain=rng.choice(["Mainnet", "Testnet"])),
                    }
                )
        for _ in range(rng.randint(1, 6)):
            ops.append(random_op(rng, n))
        prog = {"id": "rand:%d" % n, "kind": "gossip", "operations": ops}
        if base is None:
            prog["input_config"] = None
        else:
            prog["input_config"] = base
        yield prog


# ----------------------------------------------------------------------------
# Query-builder canonicalisation
# ----------------------------------------------------------------------------

QUERY_FIELDS: Dict[str, Any] = {
    "chain": ["Mainnet", "Testnet"],
    "cc": [["DE"], ["de", "nl"], ["DE", "NL", "SG"], "DE,NL", ["a b"], ["Ä"], ["a,b"]],
    "region": [["eu-central"], ["eu-central", "us-east"]],
    "asn": [["AS13335"], ["AS13335", "AS15169"]],
    "tier": [[1], [0, 1], [1, None], [None], "1,null"],
    "max_tier": [0, 2, 9],
    "role": [["validator"], ["validator", "seed"]],
    "label": [["sentry"], ["sentry", "public"]],
    "connectable": [["yes"], ["yes", "no", "unknown"]],
    "accepting": [True, False],
    "state": [["full"], ["full", "syncing"], ["responsive", "open"], ["listening"],
              ["listening", "unreachable"]],
    "country": [["JP"], ["jp", "de"]],
    "serves_mempool": [[True], [True, None], [False], [None], "true,null"],
    "auction": [["holds_slot"], ["holds_slot", "bidding"], ["bid_before", None], "bidding,null"],
    "try_new_peers": [True, False],
    "max_blocks_behind": [0, 5],
    "min_uptime": [0.0, 0.95, 1.0],
    "max_latency_ms": [0, 80],
    "vantage": ["eu", "us east"],
    "seen_within": ["1h", "30m", "7d"],
    "version": [["1.2.3"], ["1.2.3", "1.2.4"]],
    "sort": ["tier", "blocks_behind", "out_deg", "last_seen", "random"],
    "limit": [1, 50, 500],
    "cursor": ["abc", "a+b/c=", "ünï"],
    "fields": [["id", "tier"], ["id", "tier", "cc"]],
    "format": ["json", "gossip_config"],
}

# Queries that must be refused client-side (§2.2). Each must produce the same
# `Invalid` parameter in all four clients.
INVALID_QUERIES: List[Dict[str, Any]] = [
    {"seen_within": "1"},
    {"seen_within": "1s"},
    {"seen_within": "h"},
    {"seen_within": "1H"},
    {"seen_within": " 1h"},
    {"min_uptime": -0.1},
    {"min_uptime": 1.1},
    {"max_tier": -1},
    {"max_blocks_behind": -1},
    {"max_latency_ms": -1, "vantage": "eu"},
    {"limit": 0},
    {"limit": -5},
    {"max_latency_ms": 80},
    {"chain": "mainnet"},
    {"sort": "TIER"},
    {"format": "gossip-config"},
    {"connectable": ["yes", "bogus"]},
    {"connectable": ["true"]},
    {"state": ["FULL"]},
    {"state": ["LISTENING"]},
    {"serves_mempool": ["yes"]},
    {"auction": ["holding"]},
    {"auction": ["HOLDS_SLOT"]},
]


def query_programmes(rng: random.Random, count: int) -> Iterator[Dict[str, Any]]:
    names = sorted(QUERY_FIELDS)
    for name in names:
        for index, value in enumerate(QUERY_FIELDS[name]):
            yield {"id": "q1:%s:%d" % (name, index), "kind": "query_url", "query": {name: value}}
    for index, q in enumerate(INVALID_QUERIES):
        yield {"id": "qbad:%d" % index, "kind": "query_url", "query": q}
    for preset in ["tier1", "sentries", "not_full_cc"]:
        yield {"id": "qpreset:%s" % preset, "kind": "query_url", "preset": preset}
    for n in range(count):
        chosen = rng.sample(names, rng.randint(1, 6))
        query = {name: rng.choice(QUERY_FIELDS[name]) for name in chosen}
        if "max_latency_ms" in query and "vantage" not in query:
            query["vantage"] = "eu"
        yield {"id": "qrand:%d" % n, "kind": "query_url", "query": query}


# ----------------------------------------------------------------------------
# Against the mock server
# ----------------------------------------------------------------------------


def http_programmes() -> Iterator[Dict[str, Any]]:
    """One request per HTTP fixture, driven through each client for real.

    ``server`` is consumed by ``run.py``, not by the harnesses: it says which
    fixtures the mock server should hold for that programme. Serving exactly one
    fixture (with its ``X-Mock-Scenario`` header stripped) is how a scenario-only
    response is reached, since no client sends that header — it is a mock
    affordance, not part of the API.
    """
    walk = []
    for path in sorted(HTTP_FIXTURES.glob("*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        request = fixture["request"]
        req_path = request["path"]
        query = dict(request.get("query", {}))
        headers = dict(request.get("headers") or {})

        # The 304 is only reachable by sending `If-None-Match`, which a client
        # sends from its own ETag cache. `http:etag_revalidate` below drives it
        # the way a caller actually would.
        if fixture["name"] == "peers_304_not_modified":
            continue

        if req_path == "/peers" and query.get("format") == "gossip_config":
            op = "list_peers_as_gossip_config"
            ip = None
        elif req_path == "/peers":
            op = "list_peers"
            ip = None
        elif req_path == "/peers/facets":
            op = "facets"
            ip = None
        elif req_path == "/network-map/graph":
            op = "network_map_graph"
            ip = None
        elif req_path == "/network-map/summary":
            op = "network_map_summary"
            ip = None
        else:
            op = "get_peer"
            ip = req_path.rsplit("/", 1)[-1]

        if fixture["name"].startswith("peers_page_"):
            walk.append(fixture["name"])

        prog: Dict[str, Any] = {
            "id": "http:%s" % fixture["name"],
            "kind": "http",
            "op": op,
            "query": query,
            "server": {"only": [fixture["name"]], "strip_headers": True},
        }
        if ip is not None:
            prog["ip"] = ip
        if "Authorization" in headers:
            prog["api_key"] = headers["Authorization"].split(" ", 1)[-1]
        clock = fixture.get("clock", {}).get("now")
        if clock:
            prog["clock_now"] = clock
        yield prog

    # The network-map mempool / auction filters are closed enums, refused
    # client-side exactly like the /peers builder's (§2.2(5), §4.8). The server
    # entry is never reached; it is there so a client that does send gets a 200.
    for index, bad in enumerate([{"serves_mempool": "yes"}, {"auction": "holding"},
                                 {"auction": "HOLDS_SLOT"}]):
        for op in ("network_map_graph", "network_map_summary"):
            yield {
                "id": "http:nm_invalid:%s:%d" % (op, index),
                "kind": "http",
                "op": op,
                "query": bad,
                "server": {"only": ["network_map_graph", "network_map_summary"],
                           "strip_headers": True},
            }

    # The full 3-page cursor walk, as one iterator run, and the max_items guard.
    walk_query = {"limit": "2", "sort": "tier"}
    yield {
        "id": "http:iter_walk",
        "kind": "http",
        "op": "iter_peers",
        "query": walk_query,
        "server": {"only": walk, "strip_headers": True},
    }
    yield {
        "id": "http:iter_max_items",
        "kind": "http",
        "op": "iter_peers",
        "query": walk_query,
        "max_items": 3,
        "server": {"only": walk, "strip_headers": True},
    }
    # The ETag cache: the same request twice must revalidate and answer 304.
    yield {
        "id": "http:etag_revalidate",
        "kind": "http",
        "op": "list_peers_twice",
        "query": {"tier": "1"},
        "server": {"only": ["peers_owner_q3_tier1"], "strip_headers": True},
    }
    # Retry decisions: 503 fewer times than max_retries, then more.
    yield {
        "id": "http:retry_recovers",
        "kind": "http",
        "op": "list_peers",
        "query": {"tier": "1"},
        "max_retries": 3,   # retries are OFF by default; opt in
        "server": {"only": ["peers_owner_q3_tier1"], "strip_headers": True, "fail_first_n": 2},
    }
    yield {
        "id": "http:retry_exhausts",
        "kind": "http",
        "op": "list_peers",
        "query": {"tier": "1"},
        "max_retries": 3,
        "server": {"only": ["peers_owner_q3_tier1"], "strip_headers": True, "fail_first_n": 9},
    }
    # The default: no retry at all. One 503 is the caller's answer.
    yield {
        "id": "http:retry_off_by_default",
        "kind": "http",
        "op": "list_peers",
        "query": {"tier": "1"},
        "server": {"only": ["peers_owner_q3_tier1"], "strip_headers": True, "fail_first_n": 1},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--random-ops", type=int, default=240)
    parser.add_argument("--random-queries", type=int, default=120)
    parser.add_argument("--only", choices=["gossip", "query_url", "http"], default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    programmes: List[Dict[str, Any]] = []
    if args.only in (None, "gossip"):
        programmes.extend(gossip_programmes(rng, args.random_ops))
    if args.only in (None, "query_url"):
        programmes.extend(query_programmes(rng, args.random_queries))
    if args.only in (None, "http"):
        programmes.extend(http_programmes())

    stream = args.out.open("w", encoding="utf-8") if args.out else sys.stdout
    try:
        for prog in programmes:
            stream.write(json.dumps(prog, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        if args.out:
            stream.close()
    print("%d programmes" % len(programmes), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

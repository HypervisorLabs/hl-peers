#!/usr/bin/env python3
"""JSON-in / JSON-out differential harness for the Python client.

One NDJSON request per line on stdin, one NDJSON answer per line on stdout.
``tools/cross_check/run.py`` drives this and the three sibling harnesses side by
side and asserts that all four answer identically.

The harness holds no expectations of its own: it reports what the client did.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from hl_peers import (
    Client,
    GossipConfig,
    NotModified,
    PeerQuery,
    mempool_servers,
    not_full,
    sentries,
    tier1,
)
from hl_peers.errors import HlPeersError, RateLimited

# The taxonomy name of CLIENT_SPEC.md §5.1, from this client's exception class.
KIND_OF = {
    "RateLimited": "RateLimited",
    "FilterUnavailable": "FilterUnavailable",
    "ChainNotServed": "ChainNotServed",
    "CursorExpired": "CursorExpired",
    "BadRequest": "BadRequest",
    "Unauthorized": "Unauthorized",
    "NotFound": "NotFound",
    "Unavailable": "Unavailable",
    "ServerError": "Server",
    "TransportError": "Transport",
    "DecodeError": "Decode",
    "Invalid": "Invalid",
    "Truncated": "Truncated",
}

BOOL_FIELDS = {"accepting", "try_new_peers"}
INT_FIELDS = {"max_tier", "max_blocks_behind", "max_latency_ms", "limit"}
FLOAT_FIELDS = {"min_uptime"}
LIST_FIELDS = {
    "auction",
    "cc",
    "country",
    "serves_mempool",
    "region",
    "asn",
    "role",
    "label",
    "connectable",
    "state",
    "version",
    "fields",
    "tier",
}


def error_payload(exc: HlPeersError, at_op: Optional[int] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"kind": KIND_OF[type(exc).__name__]}
    # `status` is deliberately not reported: CLIENT_SPEC §5.1 puts it on the
    # `Server` variant alone, so exposing it elsewhere is a per-client accident.
    for name in ("parameter", "filter", "retry_after", "seen"):
        value = getattr(exc, name, None)
        if value is not None:
            out[name] = value
    if at_op is not None:
        out["at_op"] = at_op
    return out


# ---------------------------------------------------------------------------
# gossip
# ---------------------------------------------------------------------------


def _subst(value: Any, tmp: str) -> Any:
    if isinstance(value, str):
        return value.replace("$TMPDIR", tmp)
    if isinstance(value, list):
        return [_subst(v, tmp) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v, tmp) for k, v in value.items()}
    return value


def _unsubst(value: Any, tmp: str) -> Any:
    if isinstance(value, str):
        return value.replace(tmp, "$TMPDIR")
    if isinstance(value, list):
        return [_unsubst(v, tmp) for v in value]
    if isinstance(value, dict):
        return {k: _unsubst(v, tmp) for k, v in value.items()}
    return value


def _read_if_exists(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run_gossip(req: Dict[str, Any]) -> Dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="hlp-xcheck-py-")
    try:
        return _run_gossip(req, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_gossip(req: Dict[str, Any], tmp: str) -> Dict[str, Any]:
    subject: Optional[GossipConfig] = None
    value: Any = None
    last_warnings: Optional[List[str]] = None
    saved_text: Optional[str] = None

    try:
        if req.get("input_json_text") is not None:
            subject = GossipConfig.parse(req["input_json_text"])
        elif req.get("input_config") is not None:
            # fixtures/README: `parse(render(input_config))`.
            subject = GossipConfig.parse(json.dumps(req["input_config"], ensure_ascii=False))
    except HlPeersError as exc:
        return {"ok": False, "error": error_payload(exc, -1)}

    for index, operation in enumerate(req.get("operations", [])):
        name = operation["op"]
        args = _subst(operation.get("args", []), tmp)
        opts = _subst(operation.get("opts", {}), tmp)
        try:
            if name == "new":
                subject = GossipConfig.new(*args, **opts)
                value = None
            elif name == "from_peers":
                subject = GossipConfig.from_peers(*args, **opts)
                last_warnings = sorted(w.code for w in subject.pending_warnings)
                value = None
            elif name == "merge_peers":
                assert subject is not None
                subject.merge_peers(*args, **opts)
                last_warnings = sorted(w.code for w in subject.pending_warnings)
                value = None
            elif name == "save":
                assert subject is not None
                result = subject.save(*args, **opts)
                value = {
                    "written": result.written,
                    "changed": result.changed,
                    "diff": _unsubst(result.diff, tmp),
                    "backup": result.backup_path is not None,
                }
                last_warnings = sorted(w.code for w in result.warnings)
                path = args[0]
                saved_text = _read_if_exists(path)
            elif name == "validate":
                assert subject is not None
                value = "ok"
                last_warnings = sorted(w.code for w in subject.validate())
            else:
                assert subject is not None
                out = getattr(subject, {"has_root_ip": "has_root_ip"}.get(name, name))(
                    *args, **opts
                )
                value = None if isinstance(out, GossipConfig) else out
        except HlPeersError as exc:
            return {"ok": False, "error": error_payload(exc, index)}
        except (TypeError, ValueError) as exc:  # a harness bug, not a client one
            return {"ok": False, "harness_error": "%s: %s" % (type(exc).__name__, exc)}

    answer: Dict[str, Any] = {"ok": True, "value": _unsubst(value, tmp)}
    answer["json_text"] = subject.to_json() if subject is not None else None
    answer["op_warnings"] = last_warnings
    answer["saved_text"] = saved_text
    if subject is None:
        answer["warnings"] = None
    else:
        try:
            answer["warnings"] = sorted(w.code for w in subject.validate())
        except HlPeersError as exc:
            answer["warnings"] = None
            answer["validate_error"] = error_payload(exc)
    return answer


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


def build_query(spec: Dict[str, Any]) -> PeerQuery:
    query = PeerQuery()
    for name in sorted(spec):
        value = spec[name]
        if not callable(getattr(PeerQuery, name, None)):
            # A parameter the builder does not model (e.g. the removed `mempool=`
            # filter, which the server answers `400 filter-removed`): send it raw
            # through the constructor, the builder's escape hatch.
            query = PeerQuery(dict(query.params, **{name: value}))
            continue
        if name in BOOL_FIELDS:
            if isinstance(value, str):
                value = value == "true"
            query = getattr(query, name)(value)
        elif name in INT_FIELDS:
            query = getattr(query, name)(int(value))
        elif name in FLOAT_FIELDS:
            query = getattr(query, name)(float(value))
        elif name in LIST_FIELDS:
            query = getattr(query, name)(value)
        else:
            query = getattr(query, name)(value)
    return query


PRESETS = {
    "tier1": lambda: tier1(),
    "sentries": lambda: sentries(),
    "not_full_cc": lambda: not_full(cc=["SG"]),
    "mempool_servers": lambda: mempool_servers(),
}

BASE = "https://api.hypervisor.gg"


def run_query_url(req: Dict[str, Any]) -> Dict[str, Any]:
    try:
        query = PRESETS[req["preset"]]() if "preset" in req else build_query(req.get("query", {}))
        qs = query.to_query_string()
    except HlPeersError as exc:
        return {"ok": False, "error": error_payload(exc)}
    url = BASE + "/peers" + (("?" + qs) if qs else "")
    return {"ok": True, "url": url}


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------


def rate_limit_payload(rl: Any) -> Dict[str, Any]:
    if rl is None:
        return {}
    return {
        "limit": rl.limit,
        "remaining": rl.remaining,
        "reset": rl.reset,
        "policy": rl.policy,
        "retry_after": rl.retry_after,
    }


def envelope_payload(env: Any) -> Dict[str, Any]:
    return {
        "version": env.version,
        "available": env.available,
        "stale": env.stale,
        "chain": env.chain,
        "generated_at": env.generated_at,
        "next_sweep_at": env.next_sweep_at,
        "attribution": env.attribution,
        "network": env.network,
    }


def map_envelope_payload(result: Any) -> Dict[str, Any]:
    return {
        "version": result.version,
        "available": result.available,
        "stale": result.stale,
        "generated_at": result.generated_at,
        "network": result.network,
    }


def signals(peers: Any) -> List[List[Any]]:
    return [
        [p.id, p.mempool.serves, p.mempool.source, p.auction.status, p.auction.slot] for p in peers
    ]


def rows(peers: Any) -> List[List[Any]]:
    """``[[id, state, tier, city, version], ...]`` in peer order (``version``
    since the testnet beta)."""
    return [[p.id, p.state, p.tier, p.city, p.version] for p in peers]


def cities_payload(cities: Any) -> Any:
    """1.1.0: ``[[id, name, cc, lat, lon, peers], ...]``, or null when absent."""
    if cities is None:
        return None
    return [[c.id, c.name, c.cc, c.lat, c.lon, c.peers] for c in cities]


# serves_mempool / auction pass through as the fixture's pre-joined CSV strings:
# the client accepts those exactly like the /peers builder does.
MAP_BOOL = {"show_unreached", "hide_unreachable"}


def map_kwargs(spec: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, value in spec.items():
        if name == "max_tier":
            out[name] = int(value)
        elif name in MAP_BOOL:
            out[name] = value if isinstance(value, bool) else value == "true"
        else:
            out[name] = value
    return out


def map_common(result: Any) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": result.status,
        "tiers": [[t.tier, t.count] for t in result.tiers],
        "node_count": result.node_count,
        "edge_count": result.edge_count,
        "unreached": result.unreached,
        "operator_nodes_count": result.operator_nodes.count,
        "envelope": map_envelope_payload(result),
        "api_version": result.api_version,
        "rate_limit": rate_limit_payload(result.rate_limit),
        "etag": result.etag,
        "from_cache": result.from_cache,
    }


def run_http(req: Dict[str, Any]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "base_url": req["base_url"],
        "sleep": lambda _seconds: None,
        "random": lambda: 0.0,
        "env": {},
    }
    if "max_retries" in req:
        kwargs["max_retries"] = int(req["max_retries"])
    if req.get("api_key"):
        kwargs["api_key"] = req["api_key"]
    if req.get("clock_now"):
        moment = datetime.fromisoformat(req["clock_now"].replace("Z", "+00:00"))
        kwargs["clock"] = lambda: moment.astimezone(timezone.utc)
    client = Client(**kwargs)
    op = req["op"]

    try:
        if op == "get_peer":
            # `network` is get_peer's per-call option (CLIENT_SPEC §4.3).
            detail = client.get_peer(req["ip"], network=req.get("query", {}).get("network"))
            return {
                "ok": True,
                "status": detail.status,
                "peer_ids": [detail.peer.id],
                "signals": signals([detail.peer]),
                "rows": rows([detail.peer]),
                "envelope": envelope_payload(detail.envelope),
                "rate_limit": rate_limit_payload(detail.rate_limit),
                "etag": detail.etag,
                "from_cache": detail.from_cache,
                "api_version": detail.api_version,
            }
        if op == "network_map_graph":
            graph = client.network_map_graph(**map_kwargs(req.get("query", {})))
            answer = map_common(graph)
            answer["peer_ids"] = graph.ips()
            answer["edges"] = [[e.from_, e.to, e.span] for e in graph.edges]
            answer["truncated"] = graph.truncated
            answer["node_cities"] = [n.city for n in graph.nodes]
            answer["node_versions"] = [n.version for n in graph.nodes]
            answer["cities"] = cities_payload(graph.cities)
            answer["attribution"] = graph.attribution
            return answer
        if op == "network_map_summary":
            summary = client.network_map_summary(**map_kwargs(req.get("query", {})))
            answer = map_common(summary)
            answer["connectable"] = dict(summary.connectable)
            answer["serves_mempool"] = dict(summary.serves_mempool)
            answer["auction"] = dict(summary.auction)
            answer["cities"] = cities_payload(summary.cities)
            answer["attribution"] = summary.attribution
            return answer
        if op == "list_peers_as_gossip_config":
            result = client.list_peers_as_gossip_config(build_query(req.get("query", {})))
            return {
                "ok": True,
                "status": result.status,
                "json_text": result.to_json(),
                "api_version": result.api_version,
                "rate_limit": rate_limit_payload(result.rate_limit),
                "etag": result.etag,
                "from_cache": result.from_cache,
            }
        if op == "facets":
            facets = client.facets(build_query(req.get("query", {})) or None)
            return {
                "ok": True,
                "status": facets.status,
                "envelope": envelope_payload(facets.envelope),
                "total": facets.total,
                "facets": {
                    name: {
                        "available": facet.available,
                        "values": [[v.value, v.count] for v in facet.values],
                    }
                    for name, facet in sorted(facets.facets.items())
                },
                "rate_limit": rate_limit_payload(facets.rate_limit),
                "etag": facets.etag,
                "from_cache": facets.from_cache,
                "api_version": facets.api_version,
            }
        if op in ("list_peers", "list_peers_twice"):
            page = client.list_peers(build_query(req.get("query", {})))
            if op == "list_peers_twice":
                page = client.list_peers(build_query(req.get("query", {})))
            if isinstance(page, NotModified):
                return {
                    "ok": True,
                    "status": page.status,
                    "not_modified": True,
                    "etag": page.etag,
                    "rate_limit": rate_limit_payload(page.rate_limit),
                    "api_version": page.api_version,
                }
            return {
                "ok": True,
                "status": page.status,
                "peer_ids": [p.id for p in page.peers],
                "signals": signals(page.peers),
                "rows": rows(page.peers),
                "count": page.count,
                "total": page.total,
                "next_cursor": page.next_cursor,
                "envelope": envelope_payload(page.envelope),
                "rate_limit": rate_limit_payload(page.rate_limit),
                "etag": page.etag,
                "from_cache": page.from_cache,
                "api_version": page.api_version,
            }
        if op == "iter_peers":
            iterator = client.iter_peers(
                build_query(req.get("query", {})),
                max_items=req.get("max_items", 10000),
            )
            ids = [p.id for p in iterator]
            return {"ok": True, "peer_ids": ids, "truncated": iterator.truncated}
        return {"ok": False, "harness_error": "unknown op %r" % op}
    except HlPeersError as exc:
        payload = error_payload(exc)
        if isinstance(exc, RateLimited):
            payload["signup_url"] = exc.signup_url  # always, null if absent
            if req.get("clock_now") and exc.reset_at is not None:
                payload["reset_at"] = exc.reset_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "ok": False,
            "error": payload,
            "rate_limit": rate_limit_payload(getattr(exc, "rate_limit", None)),
        }


HANDLERS = {"gossip": run_gossip, "query_url": run_query_url, "http": run_http}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        try:
            answer = HANDLERS[req["kind"]](req)
        except Exception as exc:
            answer = {"ok": False, "harness_error": "%s: %s" % (type(exc).__name__, exc)}
        answer["id"] = req["id"]
        sys.stdout.write(json.dumps(answer, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

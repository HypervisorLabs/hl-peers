#!/usr/bin/env python3
"""Regenerate fixtures/http/*.json and fixtures/gossip/cases/*.json.

Stdlib only. This file also carries the REFERENCE implementation of the
GossipConfig serializer (CLIENT_SPEC.md §6.3) — the four language clients must
produce byte-identical output to `render()` below.

    python3 tools/gen_fixtures.py          # rewrite every fixture
    python3 tools/check_fixtures.py        # validate them
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTP = os.path.join(ROOT, "fixtures", "http")
CASES = os.path.join(ROOT, "fixtures", "gossip", "cases")

# CLIENT_SPEC.md §6.3(1) — the fixed key order for known keys.
KNOWN_ORDER = [
    "chain",
    "root_node_ips",
    "try_new_peers",
    "reserved_peer_ips",
    "n_gossip_peers",
    "split_client_blocks",
]


# --------------------------------------------------------------------------
# reference serializer — CLIENT_SPEC.md §6.3
# --------------------------------------------------------------------------
def _order(cfg: dict) -> dict:
    out = {}
    for k in KNOWN_ORDER:
        if k in cfg:
            out[k] = cfg[k]
    for k in sorted(k for k in cfg if k not in KNOWN_ORDER):
        out[k] = _sort_unknown(cfg[k])
    return out


def _sort_unknown(v):
    if isinstance(v, dict):
        return {k: _sort_unknown(v[k]) for k in sorted(v)}
    if isinstance(v, list):
        return [_sort_unknown(x) for x in v]
    return v


def render(cfg: dict) -> str:
    """The normative rendering. 2-space indent, fixed key order, trailing \\n."""
    return (
        json.dumps(_order(cfg), indent=2, ensure_ascii=False, separators=(",", ": "))
        + "\n"
    )


# --------------------------------------------------------------------------
# HTTP fixtures
# --------------------------------------------------------------------------
GEN_AT = "2026-09-21T02:35:00Z"
NEXT_SWEEP = "2026-09-21T02:40:00Z"
LAST_SEEN = "2026-09-21T02:34:58Z"


UNKNOWN_MEMPOOL = {"serves": None, "last_checked": None, "verdict": None,
                   "first_frame_ms": None, "source": None}
UNKNOWN_AUCTION = {"status": None, "slot": None, "since": None, "last_bid_at": None}
# The live attribution strings (upstream Envelope.attribution, 2026-10-12): IPinfo
# Lite for country/ASN; DB-IP's credit appended only where city positions are served.
ATTRIBUTION = "IP address data is powered by IPinfo (https://ipinfo.io), CC BY-SA 4.0"
ATTRIBUTION_WITH_CITIES = (ATTRIBUTION +
                           "; IP Geolocation by DB-IP (https://db-ip.com), CC BY 4.0")
SIGNUP_URL = "https://hypervisor.gg/docs/peer-api/authentication"


def mempool(serves, verdict=None, first_frame_ms=None, source="probe",
            last_checked="2026-09-21T02:30:00Z"):
    return {"serves": serves, "last_checked": last_checked, "verdict": verdict,
            "first_frame_ms": first_frame_ms, "source": source}


def auction(status, slot=None, since=None, last_bid_at=None):
    return {"status": status, "slot": slot, "since": since, "last_bid_at": last_bid_at}


def peer(ip, tier=1, cc="DE", role=None, name=None, operator=None,
         connectable="yes", labels=None, in_deg=3, out_deg=5, behind=0,
         state="responsive", asn="AS64500", org="Example Hosting",
         cc_confidence="medium", cc_source="db", mempool_=None, auction_=None,
         feeders=None, uptime=0.97, city=None):
    return {
        "id": ip,
        "tier": tier,
        "state": state,
        "role": role,
        "cc": cc,
        "cc_confidence": cc_confidence if cc else None,
        "cc_source": cc_source if cc else None,
        "region": None,
        "asn": asn,
        "org": org,
        "city": city,
        "operator": operator,
        "name": name,
        "in_deg": in_deg,
        "out_deg": out_deg,
        "latest_height": 812345678 - behind,
        "blocks_behind": behind,
        "live": True,
        "connectable": connectable,
        "labels": labels or [],
        "feeders": feeders or [],
        "last_seen": LAST_SEEN,
        "uptime": uptime,
        "version": None,
        "mempool": dict(mempool_ or UNKNOWN_MEMPOOL),
        "auction": dict(auction_ or UNKNOWN_AUCTION),
    }


def envelope(**over):
    e = {
        "version": "v1",
        "available": True,
        "stale": False,
        "chain": "Mainnet",
        "generated_at": GEN_AT,
        "next_sweep_at": NEXT_SWEEP,
        "attribution": ATTRIBUTION,
    }
    e.update(over)
    return e


def peer_list(peers, total=None, next_cursor=None, **over):
    body = envelope(**over)
    body["count"] = len(peers)
    body["total"] = len(peers) if total is None else total
    body["next_cursor"] = next_cursor
    body["peers"] = peers
    return body


def etag_of(body) -> str:
    canon = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return '"sha256-%s"' % hashlib.sha256(canon).hexdigest()[:16]


CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=240"
# Sent on every API-host response, errors included (upstream Plugs.ApiVersion).
API_VERSION = {"API-Version": "1"}
RL_ANON = {
    "RateLimit-Limit": "20",
    "RateLimit-Remaining": "17",
    "RateLimit-Reset": "42",
    "RateLimit-Policy": '"anon-min";q=20;w=60, "anon-day";q=2000;w=86400',
}


def ok(name, description, path, query, body, req_headers=None, content_type="application/json"):
    tag = etag_of(body)
    headers = {"Content-Type": content_type, "ETag": tag, "Cache-Control": CACHE_CONTROL}
    headers.update(API_VERSION)
    headers.update(RL_ANON)
    return {
        "name": name,
        "description": description,
        "request": {"method": "GET", "path": path, "query": query,
                    "headers": req_headers or {}},
        "response": {"status": 200, "headers": headers, "body": body},
    }


def problem(name, description, path, query, status, body, extra_headers=None,
            req_headers=None):
    headers = {"Content-Type": "application/problem+json"}
    headers.update(API_VERSION)
    headers.update(RL_ANON)
    headers.update(extra_headers or {})
    return {
        "name": name,
        "description": description,
        "request": {"method": "GET", "path": path, "query": query,
                    "headers": req_headers or {}},
        "response": {"status": status, "headers": headers, "body": body},
    }


def http_fixtures():
    f = []
    P = "/peers"   # plain paths at the host root (no /api, no /v1)

    de1 = peer("192.0.2.10", tier=1, cc="DE", role="sentry", name="Example Operator",
               operator="Example Operator", labels=["sentry"],
               cc_confidence="high", cc_source="db",
               mempool_=mempool(True, verdict="serves", first_frame_ms=412),
               auction_=auction("holds_slot", slot=3, since="2026-09-20T11:00:00Z",
                                last_bid_at="2026-09-20T10:59:12Z"))
    de2 = peer("192.0.2.11", tier=2, cc="DE", in_deg=1, out_deg=2, behind=1,
               mempool_=mempool(False, verdict="connected_no_mempool", source="probe"),
               auction_=auction("bid_before", last_bid_at="2026-09-12T08:00:00Z"))
    sg = peer("198.51.100.7", tier=1, cc="SG", in_deg=2, out_deg=1, asn="AS64501",
              org="Example Cloud", cc_confidence="high", cc_source="cloud-range",
              mempool_=mempool(True, verdict="serves", first_frame_ms=95, source="probe"),
              auction_=auction("bidding", last_bid_at="2026-09-21T01:10:00Z"))
    jp = peer("198.51.100.20", tier=1, cc="JP", connectable="no", in_deg=4, out_deg=6,
              uptime=None)
    sentry = peer("192.0.2.200", tier=0, cc="US", role="sentry", name="Foundation #13",
                  connectable="no", in_deg=0, out_deg=8,
                  labels=["tier0", "sentry", "sentry:foundation"], cc_confidence="low")

    f.append(ok("peers_default", "No filters — the unfiltered first page.",
                P, {}, peer_list([de1, de2, sg, jp, sentry], total=5)))

    f.append(ok("peers_owner_q1_cc_de",
                "Example query 1 — peers by geo. `cc` is the working geo key; "
                "`region` is not populated today and answers 400 filter-unavailable. "
                "(The mempool half is peers_owner_q1_mempool_cc_de.)",
                P, {"cc": "DE"},
                peer_list([de1, de2], total=2)))

    # 🪤 The example-query fixtures are SINGLE-PAGE: next_cursor is null and total
    # equals count. The upstream OpenAPI examples show a dangling next_cursor
    # (total 14 / 137 / 61), which cannot be followed — an iter_peers walk over
    # them would request a cursor no fixture serves. Pagination is covered by the
    # dedicated peers_page_{1,2,3}_of_3 walk instead.
    f.append(ok("peers_owner_q2_not_full_cc_sg",
                "Example query 2 — 'not full'. accepting=true is exactly connectable=yes, "
                "an ESTIMATE, not a guarantee. Single page; see peers_page_1_of_3 for a "
                "cursor walk.",
                P, {"accepting": "true", "cc": "SG"},
                peer_list([sg], next_cursor=None)))

    f.append(ok("peers_owner_q3_tier1",
                "Example query 3 — tier=1 is EXACT. max_tier=1 would also return tier 0. "
                "Single page.",
                P, {"tier": "1"},
                peer_list([jp], next_cursor=None)))

    f.append(ok("peers_owner_q4_sentries",
                "Example query 4 — label=sentry (our own identity set), not tier=0. "
                "Single page.",
                P, {"label": "sentry"},
                peer_list([sentry], next_cursor=None)))

    f.append(ok("peers_unavailable",
                "available:false — the producer could not build a graph. The empty list "
                "is NOT a finding about the network and MUST NOT become an error.",
                P, {}, peer_list([], total=0, available=False,
                                 generated_at=None, next_sweep_at=None),
                req_headers={"X-Mock-Scenario": "unavailable"}))

    f.append(ok("peers_stale",
                "stale:true with available:true — check available FIRST; a dark "
                "subsystem reports both false.",
                P, {}, peer_list([de1], total=1, stale=True),
                req_headers={"X-Mock-Scenario": "stale"}))

    gossip_body = {
        "root_node_ips": [{"Ip": "192.0.2.10"}, {"Ip": "192.0.2.11"},
                          {"Ip": "198.51.100.7"}],
        "try_new_peers": True,
        "chain": "Mainnet",
    }
    f.append(ok("peers_format_gossip_config",
                "format=gossip_config — the drop-in override file. Note the capital `Ip`.",
                P, {"cc": "DE", "format": "gossip_config"},
                gossip_body,
                content_type='application/json; profile="gossip-config"'))
    # 🔴 GOLDEN: the exact bytes a client's gossip-config helper writes for this
    # response — the §6.3 canonical rendering (key order chain, root_node_ips,
    # try_new_peers), which is NOT the server's key order. Every client asserts
    # its helper's to_json() AND the file it saves equal this, byte for byte.
    f[-1]["expect"] = {"gossip_config_json_text": render(gossip_body)}

    # --- mempool + auction ---------------------------------------------------
    f.append(ok("peers_owner_q1_mempool_cc_de",
                "Example query 1, whole — serves the mempool AND in DE. serves_mempool is "
                "a closed tri-state (true|false|null); null = unknown, NOT no.",
                P, {"cc": "DE", "serves_mempool": "true"},
                peer_list([de1], total=1)))
    f.append(ok("peers_serves_mempool_unknown",
                "serves_mempool=null selects peers whose mempool status is unknown. Their mempool "
                "object is present with every field null.",
                P, {"serves_mempool": "null"},
                peer_list([jp, sentry], total=2)))
    f.append(ok("peers_auction_holds_slot_or_bidding",
                "auction=holds_slot,bidding — the on-chain gossip read-priority auction. "
                "slot/since are set only for holds_slot.",
                P, {"auction": "holds_slot,bidding"},
                peer_list([de1, sg], total=2)))
    f.append(problem("error_400_filter_removed",
                     "`mempool` (the old internal 3-state bucket) stays removed; the "
                     "server says to use serves_mempool. A BadRequest, not FilterUnavailable.",
                     P, {"mempool": "serving"}, 400,
                     {"type": "https://api.hypervisor.gg/problems/filter-removed",
                      "title": "Filter removed", "status": 400,
                      "detail": "mempool was removed from the public contract; filter on "
                                "serves_mempool=true|false|null instead",
                      "parameter": "mempool"}))
    f.append(problem("error_400_filter_unavailable_mempool",
                     "The mempool facet carries no data in this generation: "
                     "400 filter-unavailable, never an empty list.",
                     P, {"serves_mempool": "true"}, 400,
                     {"type": "https://api.hypervisor.gg/problems/filter-unavailable",
                      "title": "Filter not available", "status": 400,
                      "detail": "serves_mempool is not collected by this producer right "
                                "now; see /peers/facets",
                      "parameter": "serves_mempool"},
                     req_headers={"X-Mock-Scenario": "mempool-unavailable"}))

    # --- /network-map/* (API host, plain paths) -------------------------------
    # City clusters (DB-IP City Lite): the graph and the
    # summary carry `cities` + the combined `attribution`; each node carries its
    # cluster id in `city` (null when placed by region/country or not at all).
    def in_city(p, cid):
        return dict(p, city=cid)

    nm_nodes = [sentry, in_city(de1, "DE-frankfurt-am-main"), sg,
                in_city(de2, "DE-frankfurt-am-main")]
    nm_edges = [{"from": "192.0.2.200", "to": "192.0.2.10", "span": 1},
                {"from": "192.0.2.200", "to": "198.51.100.7", "span": 1},
                {"from": "192.0.2.10", "to": "192.0.2.11", "span": 1}]
    nm_cities = [{"id": "DE-frankfurt-am-main", "name": "Frankfurt am Main", "cc": "DE",
                  "lat": 50.1, "lon": 8.7, "peers": 2, "tiers": {"1": 1, "2": 1}}]
    nm_env = {"version": "v1", "available": True, "operator_nodes": {"count": None},
              "stale": False, "generated_at": GEN_AT, "node_count": 4, "edge_count": 3,
              "tier_count": 3, "unreached": 0,
              "tiers": [{"tier": 0, "count": 1}, {"tier": 1, "count": 2},
                        {"tier": 2, "count": 1}]}
    f.append(ok("network_map_graph",
                "/network-map/graph — nodes (same fields as a /peers row), edges "
                "{from,to,span}, tier histogram, and (where the producer serves city "
                "positions) `cities` + `attribution`; a node's `city` is its cluster "
                "id. operator_nodes.count null = the producer publishes its own nodes "
                "as ordinary rows.",
                "/network-map/graph", {"max_tier": "2"},
                dict(nm_env, truncated=False, nodes=nm_nodes, edges=nm_edges,
                     cities=nm_cities, attribution=ATTRIBUTION_WITH_CITIES)))
    f.append(ok("network_map_summary",
                "/network-map/summary — the envelope plus zero-filled connectable, "
                "serves_mempool and auction counts, without nodes/edges/truncated. "
                "The live server also sends `cities` + `attribution` here, which the "
                "upstream schema does not list (CLIENT_SPEC §10 D-S).",
                "/network-map/summary", {},
                dict(nm_env, connectable={"yes": 3, "no": 1, "unknown": 0},
                     serves_mempool={"true": 2, "false": 1, "null": 1},
                     auction={"holds_slot": 1, "bidding": 1, "bid_before": 1, "null": 1},
                     cities=nm_cities, attribution=ATTRIBUTION_WITH_CITIES)))
    # The mempool / auction filters on the network map.
    nm_f = {"version": "v1", "available": True, "operator_nodes": {"count": None},
            "stale": False, "generated_at": GEN_AT, "node_count": 1, "edge_count": 0,
            "tier_count": 1, "unreached": 0, "tiers": [{"tier": 1, "count": 1}]}
    f.append(ok("network_map_graph_mempool_auction",
                "/network-map/graph?serves_mempool=true&auction=holds_slot — the same "
                "closed-enum filters as /peers; edges survive only between surviving "
                "nodes. No city positions in this generation: `cities`/`attribution` "
                "absent, every `city` null.",
                "/network-map/graph", {"auction": "holds_slot", "serves_mempool": "true"},
                dict(nm_f, truncated=False, nodes=[de1], edges=[])))

    # `listening` — accepts inbound connections,
    # other details unknown. Tier unplaced, height unknown.
    lis = peer("203.0.113.40", tier=None, cc="FI", connectable="unknown", in_deg=1,
               out_deg=0, state="listening", uptime=None)
    lis["latest_height"] = None
    lis["blocks_behind"] = None
    lis["live"] = False
    f.append(ok("peers_state_listening",
                "state=listening — the peer accepts inbound connections; its other "
                "details are unknown. Not `unreachable`, not `responsive`; tier null "
                "(not placed), height unknown.",
                P, {"state": "listening"},
                peer_list([lis], total=1)))

    # --- pagination, three pages, limit=2 --------------------------------
    pg = [peer("203.0.113.%d" % i, tier=1, cc="NL") for i in range(1, 6)]
    f.append(ok("peers_page_1_of_3", "Cursor pagination, page 1/3.",
                P, {"limit": "2", "sort": "tier"},
                peer_list(pg[0:2], total=5, next_cursor="cursor-page-2")))
    f.append(ok("peers_page_2_of_3", "Cursor pagination, page 2/3.",
                P, {"cursor": "cursor-page-2", "limit": "2", "sort": "tier"},
                peer_list(pg[2:4], total=5, next_cursor="cursor-page-3")))
    f.append(ok("peers_page_3_of_3",
                "Cursor pagination, page 3/3 — next_cursor null ends the walk.",
                P, {"cursor": "cursor-page-3", "limit": "2", "sort": "tier"},
                peer_list(pg[4:5], total=5, next_cursor=None)))

    # --- 304 --------------------------------------------------------------
    default_tag = etag_of(peer_list([de1, de2, sg, jp, sentry], total=5))
    f.append({
        "name": "peers_304_not_modified",
        "description": "If-None-Match matched. No body. Costs no quota unit, which is "
                       "why conditional polling is the behaviour to subsidise.",
        "request": {"method": "GET", "path": P, "query": {},
                    "headers": {"If-None-Match": default_tag}},
        "response": {"status": 304,
                     "headers": dict({"ETag": default_tag,
                                      "Cache-Control": CACHE_CONTROL},
                                     **API_VERSION, **RL_ANON),
                     "body": None},
    })

    # --- single peer ------------------------------------------------------
    dossier = dict(de1)
    dossier["feeders"] = ["192.0.2.200"]
    f.append(ok("peer_found", "One peer's PUBLIC dossier — not the internal one.",
                "/peers/192.0.2.10", {},
                dict(envelope(), peer=dossier)))

    f.append(problem("peer_not_found",
                     "A well-formed IPv4 in no source is 404 — distinct from the 400 "
                     "a malformed IPv4 gets. These must not collapse.",
                     "/peers/198.51.100.254", {}, 404,
                     {"type": "https://api.hypervisor.gg/problems/peer-not-found",
                      "title": "Peer not found", "status": 404,
                      "detail": "198.51.100.254 appears in no source."}))

    f.append(problem("peer_bad_ip",
                     "A malformed IPv4 is 400, never 404.",
                     "/peers/not-an-ip", {}, 400,
                     {"type": "https://api.hypervisor.gg/problems/invalid-filter-value",
                      "title": "Invalid IP", "status": 400,
                      "detail": "'not-an-ip' is not an IPv4 address",
                      "parameter": "ip"}))

    # --- facets -----------------------------------------------------------
    facets_body = dict(envelope())
    facets_body.pop("next_sweep_at")
    facets_body["total"] = 912
    facets_body["facets"] = {
        "tier": {"available": True, "values": [
            {"value": "0", "count": 61}, {"value": "1", "count": 137},
            {"value": "2", "count": 604}, {"value": "null", "count": 110}]},
        "cc": {"available": True, "values": [
            {"value": "JP", "count": 341}, {"value": "DE", "count": 122},
            {"value": "US", "count": 118}]},
        "region": {"available": False, "values": []},
        "asn": {"available": False, "values": []},
        "role": {"available": True, "values": [
            {"value": "sentry", "count": 61}, {"value": "null", "count": 851}]},
        "label": {"available": True, "values": [
            {"value": "tier0", "count": 61}, {"value": "sentry", "count": 61},
            {"value": "sentry:foundation", "count": 25},
            {"value": "sentry:operator", "count": 36},
            {"value": "unreachable", "count": 94}]},
        "state": {"available": True, "values": [
            {"value": "responsive", "count": 818}, {"value": "open", "count": 0},
            {"value": "full", "count": 0}, {"value": "syncing", "count": 0},
            {"value": "unreachable", "count": 94}, {"value": "unknown", "count": 0}]},
        "connectable": {"available": True, "values": [
            {"value": "yes", "count": 517}, {"value": "no", "count": 301},
            {"value": "unknown", "count": 94}]},
        "version": {"available": False, "values": []},
        "uptime": {"available": True, "values": [
            {"value": "0.99", "count": 402}, {"value": "0.95", "count": 611},
            {"value": "0.9", "count": 700}, {"value": "0.5", "count": 790},
            {"value": "null", "count": 122}]},
        "serves_mempool": {"available": True, "values": [
            {"value": "true", "count": 143}, {"value": "false", "count": 388},
            {"value": "null", "count": 381}]},
        "auction": {"available": True, "values": [
            {"value": "holds_slot", "count": 9}, {"value": "bidding", "count": 14},
            {"value": "bid_before", "count": 31}, {"value": "null", "count": 858}]},
    }
    facets_body["geo_coverage"] = {
        "peers": 912, "asn": 0.99, "cc": 0.97, "region": 0.12,
        "cc_confidence": {"high": 0.41, "medium": 0.33, "low": 0.23},
        "withheld_contradicted": 6, "source": "dbip-lite", "db_version": "2026-09"}
    f.append(ok("facets",
                "Discovery. available:false = the attribute is NOT COLLECTED; an empty "
                "values list under available:true means collected-and-nothing-matches.",
                "/peers/facets", {}, facets_body))

    # --- network=testnet (beta) -------------------------------------------
    # The same shapes as mainnet. A testnet body adds "network": "testnet" and
    # states chain Testnet; each row's `version` is the node's reported hardfork
    # version (null when the node did not report one), geo/ASN as on mainnet, no
    # operator labels, and the mempool / auction signals are the unknown shape.
    def tn_peer(ip, version, tier=1, cc="DE", **kw):
        r = peer(ip, tier=tier, cc=cc, **kw)
        r["version"] = version
        r["latest_height"] = 744212345 - (r["blocks_behind"] or 0)
        return r

    TN = {"chain": "Testnet", "network": "testnet"}
    tn_root = tn_peer("192.0.2.50", "1550", tier=0, cc="JP", in_deg=0, out_deg=4,
                      asn="AS64502", org="Example Cloud", cc_confidence="high",
                      cc_source="cloud-range", connectable="no")
    tn_root["region"] = "aws:ap-northeast-1"
    tn_a = tn_peer("198.51.100.60", "1550", tier=1, cc="US", in_deg=1, out_deg=0,
                   asn="AS64503", org="Example Hosting US")
    tn_b = tn_peer("203.0.113.70", "1549", tier=1, cc="FI", in_deg=1, out_deg=0,
                   behind=3, cc_confidence="low")
    tn_c = tn_peer("203.0.113.71", None, tier=None, cc=None, in_deg=1, out_deg=0,
                   asn=None, org=None, connectable="unknown", state="listening",
                   uptime=None)
    tn_c["latest_height"] = None
    tn_c["blocks_behind"] = None
    tn_c["live"] = False
    tn_rows = [tn_root, tn_a, tn_b, tn_c]
    f.append(ok("peers_testnet",
                "network=testnet (beta) — the testnet peer graph. Same row shape as "
                "mainnet; `version` is the node's reported hardfork version (null when "
                "not reported), geo/ASN as on mainnet, signals unknown.",
                P, {"network": "testnet"},
                peer_list(tn_rows, total=4, **TN)))
    f.append(ok("peer_found_testnet",
                "One testnet peer, network=testnet: the dossier carries the hardfork "
                "version and the geo/ASN profile.",
                "/peers/192.0.2.50", {"network": "testnet"},
                dict(envelope(**TN), peer=tn_root)))
    tn_facets = dict(envelope(**TN))
    tn_facets.pop("next_sweep_at")
    tn_facets["total"] = 4
    tn_facets["facets"] = {
        "tier": {"available": True, "values": [
            {"value": "0", "count": 1}, {"value": "1", "count": 2},
            {"value": "null", "count": 1}]},
        "cc": {"available": True, "values": [
            {"value": "FI", "count": 1}, {"value": "JP", "count": 1},
            {"value": "US", "count": 1}]},
        "version": {"available": True, "values": [
            {"value": "1550", "count": 2}, {"value": "1549", "count": 1},
            {"value": "null", "count": 1}]},
        "label": {"available": False, "values": []},
        "serves_mempool": {"available": False, "values": []},
        "auction": {"available": False, "values": []},
    }
    tn_facets["geo_coverage"] = {"peers": 4, "asn": 0.75, "cc": 0.75, "region": 0.25}
    f.append(ok("facets_testnet",
                "Facets over the testnet graph: the `version` facet counts hardfork "
                "versions; labels and signals are not collected on testnet.",
                "/peers/facets", {"network": "testnet"}, tn_facets))
    tn_env = {"version": "v1", "available": True, "operator_nodes": {"count": None},
              "stale": False, "generated_at": GEN_AT, "node_count": 4, "edge_count": 2,
              "tier_count": 2, "unreached": 1,
              "tiers": [{"tier": 0, "count": 1}, {"tier": 1, "count": 2}],
              "network": "testnet"}
    tn_edges = [{"from": "192.0.2.50", "to": "198.51.100.60", "span": 1},
                {"from": "192.0.2.50", "to": "203.0.113.70", "span": 1}]
    f.append(ok("network_map_graph_testnet",
                "/network-map/graph?network=testnet — the testnet graph; nodes carry "
                "`version` (hardfork version) and geo/ASN. No city positions.",
                "/network-map/graph", {"network": "testnet"},
                dict(tn_env, truncated=False, nodes=tn_rows, edges=tn_edges)))
    f.append(ok("network_map_summary_testnet",
                "/network-map/summary?network=testnet — the signals are unknown on "
                "testnet, so serves_mempool and auction count everything as null.",
                "/network-map/summary", {"network": "testnet"},
                dict(tn_env, connectable={"yes": 2, "no": 1, "unknown": 1},
                     serves_mempool={"true": 0, "false": 0, "null": 4},
                     auction={"holds_slot": 0, "bidding": 0, "bid_before": 0, "null": 4})))
    f.append(problem("error_400_network_not_served",
                     "network=testnet while the deployment does not serve testnet: a 400 "
                     "`network-not-served`, never a mainnet answer. Clients map it to "
                     "BadRequest with parameter `network` (CLIENT_SPEC §10 D-U).",
                     P, {"network": "testnet"}, 400,
                     {"type": "https://api.hypervisor.gg/problems/network-not-served",
                      "title": "Invalid parameter", "status": 400,
                      "detail": "network=testnet is not served yet",
                      "parameter": "network", "allowed": ["mainnet", "testnet"]},
                     req_headers={"X-Mock-Scenario": "network-not-served"}))

    # --- errors -----------------------------------------------------------
    f.append(problem("error_400_filter_unavailable",
                     "The attribute is not collected. An empty list would have lied.",
                     P, {"asn": "AS24940"}, 400,
                     {"type": "https://api.hypervisor.gg/problems/filter-unavailable",
                      "title": "Filter not available", "status": 400,
                      "detail": "asn is not collected by this producer; see /peers/facets",
                      "parameter": "asn"}))

    f.append(problem("error_400_filter_unavailable_region",
                     "region is the other uncollected attribute today.",
                     P, {"region": "aws:eu-central-1"}, 400,
                     {"type": "https://api.hypervisor.gg/problems/filter-unavailable",
                      "title": "Filter not available", "status": 400,
                      "detail": "region is not collected by this producer; see /peers/facets",
                      "parameter": "region"}))

    f.append(problem("error_400_invalid_enum",
                     "An unrecognised closed-enum member is a 400, never a silent drop.",
                     P, {"connectable": "yess"}, 400,
                     {"type": "https://api.hypervisor.gg/problems/invalid-filter-value",
                      "title": "Invalid filter value", "status": 400,
                      "detail": "connectable: 'yess' is not a recognised value",
                      "parameter": "connectable",
                      "allowed": ["yes", "no", "unknown"]}))

    f.append(problem("error_400_chain_not_served",
                     "Testnet is refused explicitly rather than answered with Mainnet "
                     "data — the single most dangerous wrong answer this API could give.",
                     P, {"chain": "Testnet"}, 400,
                     {"type": "https://api.hypervisor.gg/problems/chain-not-served",
                      "title": "Chain not served", "status": 400,
                      "detail": "This deployment serves Mainnet only.",
                      "parameter": "chain", "allowed": ["Mainnet"]}))

    f.append(problem("error_400_cursor_expired",
                     "A cursor is bound to its graph generation. Clients MUST NOT "
                     "auto-restart the walk — that would mix two generations.",
                     P, {"cursor": "cursor-from-a-previous-sweep"}, 400,
                     {"type": "https://api.hypervisor.gg/problems/cursor-expired",
                      "title": "Cursor expired", "status": 400,
                      "detail": "This cursor was issued in an earlier graph generation.",
                      "parameter": "cursor"}))

    f.append(problem("error_401_invalid_key",
                     "A key was supplied and is invalid. Omitting a key is NOT a 401 — "
                     "anonymous is a supported tier.",
                     P, {}, 401,
                     {"type": "https://api.hypervisor.gg/problems/invalid-api-key",
                      "title": "Invalid API key", "status": 401,
                      "detail": "The supplied bearer key is not recognised. "
                                "Call without a key for anonymous access."},
                     req_headers={"Authorization": "Bearer hlp_live_invalidkey0000000000"}))

    rl = dict(problem("error_429_rate_limited",
                     "Quota exhausted. Every RateLimit-* header plus Retry-After is "
                     "present on the 429 itself. 🪤 The figures in `detail` are PROSE "
                     "and disagree with the plan's tier table — never parse them.",
                     P, {}, 429,
                     {"type": "https://api.hypervisor.gg/problems/rate-limited",
                      "title": "Rate limit exceeded", "status": 429,
                      "detail": "Anonymous callers get 20 requests/minute and 2000/day. "
                                "A free API key raises this to 120/minute and 50000/day.",
                      "signup_url": SIGNUP_URL},
                     extra_headers={"Retry-After": "42", "RateLimit-Remaining": "0",
                                    "RateLimit-Reset": "42"},
                     req_headers={"X-Mock-Scenario": "rate-limited"}))
    rl["expect"] = {"retry_after_seconds": 42}
    f.append(rl)

    rl_long = dict(problem("error_429_rate_limited_long_retry_after",
                     "Retry-After above the client cap (default 60 s): the client raises "
                     "RateLimited immediately rather than blocking the caller for minutes.",
                     P, {}, 429,
                     {"type": "https://api.hypervisor.gg/problems/rate-limited",
                      "title": "Rate limit exceeded", "status": 429,
                      "detail": "Daily cap reached.",
                      "signup_url": SIGNUP_URL},
                     extra_headers={"Retry-After": "3600", "RateLimit-Remaining": "0",
                                    "RateLimit-Reset": "3600"},
                     req_headers={"X-Mock-Scenario": "rate-limited-long"}))
    rl_long["expect"] = {"retry_after_seconds": 3600}
    f.append(rl_long)

    http_date = dict(problem(
        "error_429_retry_after_http_date",
        "Retry-After as an RFC 9110 IMF-fixdate rather than a delay in seconds. "
        "The client parses it against its INJECTABLE clock — inject `clock.now` and "
        "assert `expect.retry_after_seconds`, never wall-clock time.",
        P, {}, 429,
        {"type": "https://api.hypervisor.gg/problems/rate-limited",
         "title": "Rate limit exceeded", "status": 429,
         "detail": "Window exhausted.",
         "signup_url": SIGNUP_URL},
        extra_headers={"Retry-After": "Mon, 21 Sep 2026 02:36:00 GMT",
                       "RateLimit-Remaining": "0", "RateLimit-Reset": "60"},
        req_headers={"X-Mock-Scenario": "rate-limited-http-date"}))
    http_date["clock"] = {"now": "2026-09-21T02:35:00Z"}
    http_date["expect"] = {"retry_after_seconds": 60}
    f.append(http_date)

    bad_date = dict(problem(
        "error_429_retry_after_unparseable",
        "Retry-After that is neither digits nor an HTTP-date is treated as ABSENT: "
        "fall back to exponential backoff. Do not guess, do not fail the request.",
        P, {}, 429,
        {"type": "https://api.hypervisor.gg/problems/rate-limited",
         "title": "Rate limit exceeded", "status": 429,
         "detail": "Window exhausted.",
         "signup_url": SIGNUP_URL},
        extra_headers={"Retry-After": "soon-ish", "RateLimit-Remaining": "0"},
        req_headers={"X-Mock-Scenario": "rate-limited-bad-retry-after"}))
    bad_date["expect"] = {"retry_after_seconds": None}
    f.append(bad_date)

    flood = dict(problem(
        "error_429_ip_flood",
        "The per-address flood limit (upstream IpRateLimit, before tiering) answers "
        "RFC 9457 problem+json too: a `rate-limited` type, "
        "Retry-After in seconds, its own RateLimit-* window and no-store. The client "
        "maps it exactly like the tier 429.",
        P, {}, 429,
        {"type": "https://api.hypervisor.gg/problems/rate-limited",
         "title": "Too many requests", "status": 429,
         "detail": "Per-address request rate exceeded (100 per 1 s). Retry after 1 s.",
         "signup_url": SIGNUP_URL},
        extra_headers={"Retry-After": "1", "RateLimit-Limit": "100",
                       "RateLimit-Remaining": "0", "RateLimit-Reset": "1",
                       "RateLimit-Policy": "100;w=1", "Cache-Control": "no-store"},
        req_headers={"X-Mock-Scenario": "rate-limited-flood"}))
    flood["expect"] = {"retry_after_seconds": 1}
    f.append(flood)

    f.append(problem("error_503_producer_unavailable",
                     "503 is reserved for the build path actually raising. A graph that "
                     "merely cannot be built is a 200 with available:false.",
                     P, {}, 503,
                     {"type": "https://api.hypervisor.gg/problems/producer-unavailable",
                      "title": "Peer graph unavailable", "status": 503,
                      "detail": "The peer graph producer is not currently able to answer."},
                     req_headers={"X-Mock-Scenario": "producer-unavailable"}))

    f.append({
        "name": "error_500_non_problem_body",
        "description": "A 5xx whose body is NOT problem+json. Exercises the fallback: "
                       "map on status alone, problem=null, body excerpt <= 512 bytes.",
        "request": {"method": "GET", "path": P, "query": {},
                    "headers": {"X-Mock-Scenario": "server-error"}},
        "response": {"status": 500,
                     "headers": {"Content-Type": "text/plain; charset=utf-8",
                                 "API-Version": "1"},
                     "body_text": "Internal Server Error"},
    })

    return f


# --------------------------------------------------------------------------
# Gossip cases
# --------------------------------------------------------------------------
def case(name, description, ops, expect, input_config=None, input_json_text=None):
    c = {"name": name, "description": description}
    if input_json_text is not None:
        c["input_json_text"] = input_json_text
    else:
        c["input_config"] = input_config
    c["operations"] = ops
    c["expect"] = expect
    return c


def op(name, *args, **kw):
    o = {"op": name}
    if args:
        o["args"] = list(args)
    if kw:
        o["opts"] = kw
    return o


MINIMAL = {"chain": "Mainnet", "root_node_ips": [{"Ip": "192.0.2.10"}],
           "try_new_peers": False}

REFERENCE = {
    "chain": "Mainnet",
    "root_node_ips": [{"Ip": "192.0.2.10"}, {"Ip": "192.0.2.11"},
                      {"Ip": "198.51.100.7"}],
    "try_new_peers": True,
    "reserved_peer_ips": ["198.51.100.20", "198.51.100.21"],
    "n_gossip_peers": 20,
    "split_client_blocks": False,
}


def err(kind, parameter=None, at_op=None, reason_contains=None):
    e = {"kind": kind}
    if parameter:
        e["parameter"] = parameter
    if at_op is not None:
        e["at_op"] = at_op
    if reason_contains:
        e["reason_contains"] = reason_contains
    return e


def warns(cfg, pending=()):
    """Reference warning set for a config — CLIENT_SPEC.md §6.4 + §6.5.

    Merge-time warnings are `pending`: they ride on the config and are
    re-reported by validate()/save(), so a caller who ignored the merge's
    return value cannot lose them.
    """
    w = set(pending)
    roots = cfg.get("root_node_ips") or []
    n = len(roots)
    if n > 64:
        w.add("many_roots_gt_64")
    elif n > 24:
        w.add("many_roots_gt_24")
    if "chain" not in cfg:
        w.add("chain_absent")
    if cfg.get("try_new_peers") is False and n > 1:
        w.add("try_new_peers_false_multi_root")
    return sorted(w)


def ex_json(final, pending=()):
    return {"json_text": render(final), "warnings": warns(final, pending)}


def ex_value(value, final, pending=(), **extra):
    e = {"value": value, "warnings": warns(final, pending)}
    e.update(extra)
    return e


def roots(*ips):
    return [{"Ip": ip} for ip in ips]


def gossip_cases():
    c = []
    A = c.append

    # MINIMAL is the live production shape: one root, try_new_peers false.
    # OPEN is its try_new_peers:true sibling, used by every case that ends with
    # MORE than one root, so the behaviour under test is not masked by an
    # incidental try_new_peers_false_multi_root warning.
    OPEN = {"chain": "Mainnet", "root_node_ips": roots("192.0.2.10"),
            "try_new_peers": True}

    # ---- construction and byte-exactness --------------------------------
    A(case("new_minimal_live_form",
           "new() from bare strings stores the {\"Ip\": …} shape. This is byte-for-byte "
           "the minimal form a production box actually runs.",
           [op("new", "Mainnet", ["192.0.2.10"]), op("set_try_new_peers", False)],
           ex_json(MINIMAL)))

    ref_true = dict(REFERENCE, split_client_blocks=True)
    A(case("reference_example_all_six_keys",
           "Every contract field present, in the normative key order. The parsed "
           "split_client_blocks:true survives the round trip untouched (§6.2) — only "
           "an explicit setter call removes the key.",
           [op("to_json")], ex_json(ref_true), input_config=ref_true))

    A(case("key_order_is_canonical_not_input_order",
           "Input key order is discarded; output order is fixed (§6.3(1)) so four "
           "languages emit identical bytes.",
           [op("to_json")], ex_json(MINIMAL),
           input_json_text='{"try_new_peers": false, "root_node_ips": '
                           '[{"Ip": "192.0.2.10"}], "chain": "Mainnet"}'))

    A(case("indent_is_two_spaces_and_one_trailing_newline",
           "Exactly one trailing \\n, never \\r\\n; 2-space nesting-proportional indent.",
           [op("to_json")], ex_json(MINIMAL), input_config=MINIMAL))

    unk = dict(OPEN, zzz_future_key="kept", aaa_future_key={"b": 2, "a": 1},
               mid_future_list=[3, 1, 2])
    A(case("unknown_keys_preserved_and_sorted_after_known",
           "Unknown keys survive a round trip, are emitted AFTER every known key, and "
           "are sorted by code point. Nested object keys sort; array order is preserved.",
           [op("to_json")], ex_json(unk), input_config=unk))

    unk2 = dict(unk, root_node_ips=roots("192.0.2.10", "192.0.2.11"))
    A(case("unknown_keys_survive_mutation",
           "Mutating a known field must not disturb unknown keys.",
           [op("add_root_ip", "192.0.2.11")], ex_json(unk2), input_config=unk))

    empty = dict(MINIMAL, reserved_peer_ips=[])
    A(case("empty_array_renders_inline",
           "An empty array is `[]` on one line, not expanded.",
           [op("to_json")], ex_json(empty), input_config=empty))

    nonascii = dict(MINIMAL, operator_note="Betreiber — Köln ✓")
    A(case("non_ascii_emitted_literally",
           "UTF-8 literal, never \\u-escaped.",
           [op("to_json")], ex_json(nonascii), input_config=nonascii))

    # ---- root ip algebra -------------------------------------------------
    open2 = dict(OPEN, root_node_ips=roots("192.0.2.10", "192.0.2.11"))
    A(case("add_root_ip", "Appends.",
           [op("add_root_ip", "192.0.2.11")], ex_json(open2), input_config=OPEN))

    A(case("add_root_ip_duplicate_is_a_noop",
           "A duplicate add is a no-op, not an error.",
           [op("add_root_ip", "192.0.2.10")], ex_json(MINIMAL), input_config=MINIMAL))

    two = dict(MINIMAL, root_node_ips=roots("192.0.2.10", "192.0.2.11"))
    A(case("remove_root_ip", "Removes by bare string.",
           [op("remove_root_ip", "192.0.2.11")], ex_json(MINIMAL), input_config=two))

    A(case("remove_root_ip_absent_is_a_noop", "Removing an absent IP is a no-op.",
           [op("remove_root_ip", "203.0.113.9")], ex_json(MINIMAL),
           input_config=MINIMAL))

    A(case("set_root_ips_replaces_and_dedupes_first_wins",
           "Wholesale replace, given order preserved, first occurrence wins.",
           [op("set_root_ips", ["198.51.100.7", "192.0.2.10", "198.51.100.7"])],
           ex_json(dict(OPEN, root_node_ips=roots("198.51.100.7", "192.0.2.10"))),
           input_config=OPEN))

    A(case("has_root_ip_true", "Query accessor.", [op("has_root_ip", "192.0.2.10")],
           ex_value(True, MINIMAL), input_config=MINIMAL))
    A(case("has_root_ip_false", "Query accessor.", [op("has_root_ip", "203.0.113.1")],
           ex_value(False, MINIMAL), input_config=MINIMAL))

    acc = dict(OPEN, root_node_ips=roots("192.0.2.10", "198.51.100.7"))
    A(case("root_ips_accessor_returns_bare_strings",
           "The {\"Ip\": …} wrapper is an encoding detail and does not leak here.",
           [op("root_ips")],
           ex_value(["192.0.2.10", "198.51.100.7"], acc), input_config=acc))

    # ---- reserved peers: BARE STRINGS ------------------------------------
    A(case("add_reserved_peer_ip_is_a_bare_string",
           "🪤 reserved_peer_ips entries are bare strings while root_node_ips entries "
           "are {\"Ip\": …} objects. Two shapes, same file.",
           [op("add_reserved_peer_ip", "198.51.100.20")],
           ex_json(dict(MINIMAL, reserved_peer_ips=["198.51.100.20"])),
           input_config=MINIMAL))

    resv = dict(MINIMAL, reserved_peer_ips=["198.51.100.20", "198.51.100.21"])
    A(case("remove_reserved_peer_ip", "Removes a bare string.",
           [op("remove_reserved_peer_ip", "198.51.100.21")],
           ex_json(dict(MINIMAL, reserved_peer_ips=["198.51.100.20"])),
           input_config=resv))

    A(case("set_reserved_peer_ips", "Wholesale replace, bare strings.",
           [op("set_reserved_peer_ips", ["203.0.113.5"])],
           ex_json(dict(MINIMAL, reserved_peer_ips=["203.0.113.5"])),
           input_config=resv))

    A(case("both_entry_shapes_round_trip_together",
           "The file carries both shapes at once and neither leaks into the other.",
           [op("to_json")], ex_json(resv), input_config=resv))

    A(case("reserved_peer_ip_wrapped_object_is_rejected",
           "A {\"Ip\": …} object inside reserved_peer_ips is the classic shape "
           "confusion and must be refused, not silently accepted.",
           [op("validate")], {"error": err("Invalid", "reserved_peer_ips[0]")},
           input_config=dict(MINIMAL, reserved_peer_ips=[{"Ip": "198.51.100.20"}])))

    # ---- remaining setters ------------------------------------------------
    A(case("set_try_new_peers", "Boolean setter.",
           [op("set_try_new_peers", True)],
           ex_json(dict(MINIMAL, try_new_peers=True)), input_config=MINIMAL))

    A(case("set_chain", "Chain setter; spelling is exact.",
           [op("set_chain", "Testnet")],
           ex_json(dict(MINIMAL, chain="Testnet")), input_config=MINIMAL))

    A(case("set_n_gossip_peers_in_range", "20 is inside [8,100].",
           [op("set_n_gossip_peers", 20)],
           ex_json(dict(MINIMAL, n_gossip_peers=20)), input_config=MINIMAL))

    A(case("set_n_gossip_peers_below_range_is_refused",
           "D-C: refuse, never silently clamp. 7 < 8.",
           [op("set_n_gossip_peers", 7)],
           {"error": err("Invalid", "n_gossip_peers", at_op=0)}, input_config=MINIMAL))

    A(case("set_n_gossip_peers_above_range_is_refused", "101 > 100.",
           [op("set_n_gossip_peers", 101)],
           {"error": err("Invalid", "n_gossip_peers", at_op=0)}, input_config=MINIMAL))

    A(case("split_client_blocks_false_removes_the_key",
           "The node's serializer omits it when false, so the setter removes it.",
           [op("set_split_client_blocks", False)], ex_json(MINIMAL),
           input_config=dict(MINIMAL, split_client_blocks=True)))

    A(case("split_client_blocks_true_is_written", "true is written explicitly.",
           [op("set_split_client_blocks", True)],
           ex_json(dict(MINIMAL, split_client_blocks=True)), input_config=MINIMAL))

    A(case("split_client_blocks_explicit_false_survives_round_trip",
           "An explicit false already in the file is PRESERVED — only the setter "
           "removes it. Round-tripping must not rewrite an operator's file.",
           [op("to_json")], ex_json(dict(MINIMAL, split_client_blocks=False)),
           input_config=dict(MINIMAL, split_client_blocks=False)))

    A(case("unset_split_client_blocks", "Explicit removal in either state.",
           [op("unset_split_client_blocks")], ex_json(MINIMAL),
           input_config=dict(MINIMAL, split_client_blocks=False)))

    # ---- from_peers / merge_peers ----------------------------------------
    twelve = ["203.0.113.%d" % i for i in range(1, 13)]

    A(case("from_peers_basic",
           "Build from an API result. Order is the API's ranking; the client never "
           "re-sorts.",
           [op("from_peers", [{"id": "192.0.2.10"}, {"id": "192.0.2.11"}],
               chain="Mainnet", try_new_peers=True)],
           ex_json({"chain": "Mainnet",
                    "root_node_ips": roots("192.0.2.10", "192.0.2.11"),
                    "try_new_peers": True}),
           input_config=None))

    A(case("from_peers_accepts_bare_ip_strings",
           "Bare strings are accepted on input alongside peer objects.",
           [op("from_peers", ["192.0.2.10"], chain="Mainnet", try_new_peers=False)],
           ex_json(MINIMAL), input_config=None))

    A(case("from_peers_without_chain_is_refused",
           "Never default the chain to Mainnet: a chain that disagrees with the node's "
           "--chain stops the node from starting, and guessing is how that happens.",
           [op("from_peers", ["192.0.2.10"])],
           {"error": err("Invalid", "chain", at_op=0)}, input_config=None))

    A(case("merge_peers_max_roots_default_8_truncates_head",
           "12 candidates, default max_roots=8, head kept, `truncated` warning emitted "
           "by the merge AND re-reported by validate (§6.5). A shorter root list starts "
           "the node faster; a longer one buys nothing.",
           [op("merge_peers", twelve, mode="replace")],
           ex_json(dict(OPEN, root_node_ips=roots(*twelve[:8])), ("truncated",)),
           input_config=OPEN))

    A(case("merge_peers_max_roots_explicit_24",
           "The upstream contract's suggested ceiling, opted into explicitly. Nothing "
           "is dropped, so no `truncated`.",
           [op("merge_peers", twelve, mode="replace", max_roots=24)],
           ex_json(dict(OPEN, root_node_ips=roots(*twelve))), input_config=OPEN))

    A(case("merge_peers_mode_append",
           "append = keep_existing ++ existing ++ candidates.",
           [op("merge_peers", ["203.0.113.1"], mode="append")],
           ex_json(dict(OPEN, root_node_ips=roots("192.0.2.10", "203.0.113.1"))),
           input_config=OPEN))

    A(case("merge_peers_mode_prepend",
           "prepend = keep_existing ++ candidates ++ existing.",
           [op("merge_peers", ["203.0.113.1"], mode="prepend")],
           ex_json(dict(OPEN, root_node_ips=roots("203.0.113.1", "192.0.2.10"))),
           input_config=OPEN))

    A(case("merge_peers_keep_existing_is_placed_first",
           "Operator-pinned IPs always survive and lead the list.",
           [op("merge_peers", ["203.0.113.1", "203.0.113.2"], mode="replace",
               keep_existing=["192.0.2.10"])],
           ex_json(dict(OPEN, root_node_ips=roots("192.0.2.10", "203.0.113.1",
                                                  "203.0.113.2"))),
           input_config=OPEN))

    A(case("merge_peers_exclude_beats_keep_existing",
           "🪤 exclude wins over keep_existing, and the conflict is named in a warning "
           "that validate re-reports (§6.5).",
           [op("merge_peers", ["203.0.113.1"], mode="replace",
               keep_existing=["192.0.2.10"], exclude=["192.0.2.10"])],
           ex_json(dict(OPEN, root_node_ips=roots("203.0.113.1")),
                   ("exclude_overrides_keep_existing",)),
           input_config=OPEN))

    A(case("merge_peers_dedupe_first_wins",
           "Repeats collapse, first occurrence wins, order otherwise preserved. "
           "Collapsing a duplicate is not a truncation and raises no warning.",
           [op("merge_peers", ["203.0.113.1", "203.0.113.2", "203.0.113.1"],
               mode="replace")],
           ex_json(dict(OPEN, root_node_ips=roots("203.0.113.1", "203.0.113.2"))),
           input_config=OPEN))

    A(case("merge_peers_no_dedupe_with_duplicates_is_refused",
           "dedupe:false is only usable on already-distinct input; validate rejects "
           "the duplicate rather than writing it.",
           [op("merge_peers", ["203.0.113.1", "203.0.113.1"], mode="replace",
               dedupe=False)],
           {"error": err("Invalid", "root_node_ips", reason_contains="duplicate")},
           input_config=OPEN))

    A(case("merge_peers_empty_result_is_refused",
           "🔴 The filter matched nothing. Writing this would empty root_node_ips and "
           "crash-loop the node. Refuse; never fall back to the previous roots silently.",
           [op("merge_peers", [], mode="replace")],
           {"error": err("Invalid", "root_node_ips",
                         reason_contains="empty root_node_ips")},
           input_config=MINIMAL))

    A(case("merge_peers_invalid_candidate_is_an_error_not_a_silent_drop",
           "A non-IPv4 candidate is refused; silently dropping it would shrink the "
           "root list without telling the operator.",
           [op("merge_peers", ["203.0.113.1", "not-an-ip"], mode="replace")],
           {"error": err("Invalid", reason_contains="not an IPv4")},
           input_config=MINIMAL))

    A(case("merge_peers_truncation_warning_survives_to_save",
           "§6.5: a merge warning is PENDING on the config, so save() re-reports it "
           "even though the caller ignored what merge_peers returned.",
           [op("merge_peers", twelve, mode="replace"),
            op("save", "$TMPDIR/override_gossip_config.json")],
           ex_value({"written": True, "changed": True},
                    dict(OPEN, root_node_ips=roots(*twelve[:8])), ("truncated",),
                    file_after={"path": "$TMPDIR/override_gossip_config.json",
                                "json_text": render(dict(OPEN, root_node_ips=roots(*twelve[:8])))}),
           input_config=OPEN))

    # ---- validate ---------------------------------------------------------
    A(case("validate_zero_roots_is_refused",
           "🔴 The single most important guard in the library.",
           [op("validate")],
           {"error": err("Invalid", "root_node_ips",
                         reason_contains="empty root_node_ips")},
           input_config={"chain": "Mainnet", "root_node_ips": [], "try_new_peers": False}))

    A(case("validate_missing_root_node_ips_is_refused",
           "Absent is the same failure as empty.",
           [op("validate")], {"error": err("Invalid", "root_node_ips")},
           input_config={"chain": "Mainnet", "try_new_peers": False}))

    A(case("validate_bare_string_root_entry_is_refused",
           "A bare string inside a PARSED root_node_ips is the file the node will not "
           "accept. Bare strings are accepted only as constructor INPUT.",
           [op("validate")], {"error": err("Invalid", "root_node_ips[0]")},
           input_config={"chain": "Mainnet", "root_node_ips": ["192.0.2.10"]}))

    A(case("validate_lowercase_ip_key_is_refused",
           "🪤 The capitalization is load-bearing: `ip` is not `Ip`.",
           [op("validate")], {"error": err("Invalid", "root_node_ips[0]")},
           input_config={"chain": "Mainnet", "root_node_ips": [{"ip": "192.0.2.10"}]}))

    A(case("validate_root_entry_with_port_is_refused",
           "The entry has no port field.",
           [op("validate")], {"error": err("Invalid", "root_node_ips[0]")},
           input_config={"chain": "Mainnet",
                         "root_node_ips": [{"Ip": "192.0.2.10", "port": 8080}]}))

    A(case("validate_ipv6_is_refused",
           "D-B: IPv6 is out of contract; the node's support for it is unverified. "
           "Refusing is the safe side of an unknown whose failure mode is a crash loop.",
           [op("validate")], {"error": err("Invalid", "root_node_ips[0]",
                                           reason_contains="IPv6")},
           input_config={"chain": "Mainnet",
                         "root_node_ips": [{"Ip": "2001:db8::1"}]}))

    A(case("validate_malformed_ipv4_is_refused", "999.0.2.10 is not an IPv4.",
           [op("validate")], {"error": err("Invalid", "root_node_ips[0]",
                                           reason_contains="not an IPv4")},
           input_config={"chain": "Mainnet", "root_node_ips": [{"Ip": "999.0.2.10"}]}))

    A(case("validate_duplicate_roots_are_refused", "Duplicates inside root_node_ips.",
           [op("validate")],
           {"error": err("Invalid", "root_node_ips", reason_contains="duplicate")},
           input_config={"chain": "Mainnet",
                         "root_node_ips": roots("192.0.2.10", "192.0.2.10")}))

    A(case("validate_duplicate_reserved_are_refused",
           "The two lists are validated independently.",
           [op("validate")],
           {"error": err("Invalid", "reserved_peer_ips", reason_contains="duplicate")},
           input_config=dict(MINIMAL, reserved_peer_ips=["198.51.100.20",
                                                         "198.51.100.20"])))

    both = dict(MINIMAL, reserved_peer_ips=["192.0.2.10"])
    A(case("validate_same_ip_in_both_lists_is_allowed",
           "An IP MAY be both a root and a reserved peer.",
           [op("validate")], ex_value("ok", both), input_config=both))

    A(case("validate_bad_chain_spelling_is_refused",
           "`mainnet` is not `Mainnet`; case matters.",
           [op("validate")], {"error": err("Invalid", "chain")},
           input_config=dict(MINIMAL, chain="mainnet")))

    r25 = dict(OPEN, root_node_ips=roots(*["203.0.113.%d" % i for i in range(1, 26)]))
    A(case("validate_warns_above_24_roots",
           "Warning, never fatal: a long root list starts the node slower. "
           "try_new_peers is true here so this case isolates the ONE warning under test.",
           [op("validate")], ex_value("ok", r25), input_config=r25))

    r65 = dict(OPEN, root_node_ips=roots(
        *["203.0.113.%d" % i for i in range(1, 66)]))
    A(case("validate_warns_above_64_roots",
           "Above 64 the warning escalates: many_roots_gt_64 fires INSTEAD OF "
           "many_roots_gt_24, not alongside it.",
           [op("validate")], ex_value("ok", r65), input_config=r65))

    no_chain = {"root_node_ips": roots("192.0.2.10"), "try_new_peers": True}
    r25_pinned = dict(MINIMAL, root_node_ips=roots(
        *["203.0.113.%d" % i for i in range(1, 26)]))
    A(case("validate_warns_many_roots_and_pinning_together",
           "🔴 The warning codes are INDEPENDENT. 25 roots with try_new_peers:false "
           "reports BOTH many_roots_gt_24 and try_new_peers_false_multi_root. A "
           "many_roots_* warning does not suppress, absorb or outrank the pinning "
           "warning — they describe two unrelated facts, and swallowing one would hide "
           "the harder problem behind the easier one. Only gt_24/gt_64 are mutually "
           "exclusive.",
           [op("validate")], ex_value("ok", r25_pinned), input_config=r25_pinned))

    r65_pinned = dict(MINIMAL, root_node_ips=roots(
        *["203.0.113.%d" % i for i in range(1, 66)]))
    A(case("validate_warns_escalated_many_roots_and_pinning_together",
           "The same independence at the escalated threshold: many_roots_gt_64 fires "
           "instead of gt_24, and still alongside try_new_peers_false_multi_root.",
           [op("validate")], ex_value("ok", r65_pinned), input_config=r65_pinned))

    A(case("validate_warns_when_chain_absent",
           "The library cannot tell which chain the node runs, and a disagreement "
           "stops the node from starting.",
           [op("validate")], ex_value("ok", no_chain), input_config=no_chain))

    multi_pinned = dict(MINIMAL, root_node_ips=roots("192.0.2.10", "192.0.2.11"))
    A(case("validate_warns_try_new_peers_false_with_multiple_roots",
           "Unverified: whether try_new_peers:false restricts a MULTI-entry list to "
           "exactly that list. Only a single root is known to pin.",
           [op("validate")], ex_value("ok", multi_pinned), input_config=multi_pinned))

    A(case("validate_single_root_with_try_new_peers_false_does_not_warn",
           "The known pinning case is clean: no warnings at all.",
           [op("validate")], ex_value("ok", MINIMAL), input_config=MINIMAL))

    # ---- parse errors ------------------------------------------------------
    A(case("parse_malformed_json_is_a_decode_error", "Not JSON at all.",
           [op("to_json")], {"error": err("Decode")},
           input_json_text='{"chain": "Mainnet", "root_node_ips": ['))

    A(case("parse_non_object_top_level_is_refused", "A JSON array is not a config.",
           [op("to_json")], {"error": err("Invalid", "$")},
           input_json_text='["192.0.2.10"]'))

    A(case("parse_root_node_ips_not_an_array_is_refused", "Type mismatch on a known key.",
           [op("validate")], {"error": err("Invalid", "root_node_ips")},
           input_json_text='{"chain": "Mainnet", "root_node_ips": "192.0.2.10"}'))

    # ---- save --------------------------------------------------------------
    A(case("save_is_idempotent",
           "Saving byte-identical content twice writes once. The second call is a "
           "no-op: no write, no mtime change, no .bak.",
           [op("save", "$TMPDIR/override_gossip_config.json"),
            op("save", "$TMPDIR/override_gossip_config.json")],
           ex_value({"written": False, "changed": False}, MINIMAL),
           input_config=MINIMAL))

    A(case("save_dry_run_writes_nothing_and_returns_a_diff",
           "dry_run renders the unified diff against what is on disk and writes nothing.",
           [op("save", "$TMPDIR/override_gossip_config.json"),
            op("add_root_ip", "192.0.2.99"),
            op("save", "$TMPDIR/override_gossip_config.json", dry_run=True)],
           ex_value({"written": False, "changed": True},
                    dict(OPEN, root_node_ips=roots("192.0.2.10", "192.0.2.99")),
                    diff_contains=["192.0.2.99"],
                    file_after={"path": "$TMPDIR/override_gossip_config.json",
                                "json_text": render(OPEN)}),
           input_config=OPEN))

    A(case("save_refuses_to_write_an_invalid_config",
           "validate runs FIRST; nothing reaches the filesystem.",
           [op("set_root_ips", []),
            op("save", "$TMPDIR/override_gossip_config.json")],
           {"error": err("Invalid", "root_node_ips"),
            "file_absent": "$TMPDIR/override_gossip_config.json"},
           input_config=MINIMAL))

    # ---- cross-client resolutions (tools/cross_check found these) --------
    # Each of these pins a point where the four clients disagreed and
    # CLIENT_SPEC was silent; the resolution is now written into the spec.

    A(case("merge_peers_invalid_candidate_names_the_input_index",
           "🪤 An invalid merge candidate is reported as peers[<n>] — the index in "
           "what the CALLER passed, not a position in a config the value never "
           "reached. Four clients spelled this three different ways until it was "
           "pinned here.",
           [op("merge_peers", ["203.0.113.1", "not-an-ip", "203.0.113.2"],
               mode="replace")],
           {"error": err("Invalid", "peers[1]", at_op=0,
                         reason_contains="not an IPv4")},
           input_config=OPEN))

    A(case("mutators_do_not_validate_ips_validate_does",
           "🔴 Only set_n_gossip_peers refuses at the mutator (§6.2). Every other "
           "setter stores what it was given and validate() owns the rule, so a "
           "sequence of edits fails at the same operation in all four languages.",
           [op("add_root_ip", "192.0.2.256"), op("validate")],
           {"error": err("Invalid", "root_node_ips[1]", at_op=1,
                         reason_contains="not an IPv4")},
           input_config=OPEN))

    A(case("set_chain_defers_the_spelling_rule_to_validate",
           "Same rule as above for chain: stored as given, refused by validate() "
           "(§6.4(5)) — not by the setter.",
           [op("set_chain", "MAINNET"), op("validate")],
           {"error": err("Invalid", "chain", at_op=1)},
           input_config=OPEN))

    A(case("remove_reserved_peer_ip_does_not_create_the_key",
           "A no-op remove on a config with no reserved_peer_ips leaves it without "
           "one. Materialising an empty array changes the file's bytes for a call "
           "that did nothing.",
           [op("remove_reserved_peer_ip", "198.51.100.20")],
           ex_json(OPEN),
           input_config=OPEN))

    A(case("merge_peers_leaves_chain_and_try_new_peers_alone",
           "🔴 §6.5(9): a merge touches root_node_ips and nothing else. The opts bag "
           "is shared with from_peers, so chain/try_new_peers are ACCEPTED here and "
           "deliberately not applied — silently rewriting the chain of a file an "
           "operator already has can stop the node from starting.",
           [op("merge_peers", ["203.0.113.1"], mode="replace",
               chain="Testnet", try_new_peers=False)],
           ex_json(dict(OPEN, root_node_ips=roots("203.0.113.1"))),
           input_config=OPEN))

    A(case("merge_peers_no_dedupe_duplicate_removed_by_truncation_is_ok",
           "The dedupe:false refusal is validate()'s, AFTER truncation (§6.5 6-7). "
           "A duplicate that max_roots drops never reaches the file, so it must not "
           "fail the merge.",
           [op("merge_peers", ["203.0.113.1", "203.0.113.2", "203.0.113.2"],
               mode="replace", dedupe=False, max_roots=2)],
           ex_json(dict(OPEN, root_node_ips=roots("203.0.113.1", "203.0.113.2")),
                   pending=("truncated",)),
           input_config=OPEN))

    A(case("merge_peers_validates_the_whole_result",
           "A merge onto a config that already carries a bad reserved_peer_ips entry "
           "fails AT THE MERGE, where the caller can still act on it — not silently, "
           "and not later at save().",
           [op("merge_peers", ["203.0.113.1"], mode="replace")],
           {"error": err("Invalid", "reserved_peer_ips[0]", at_op=0)},
           input_config={"chain": "Mainnet", "root_node_ips": roots("192.0.2.10"),
                         "try_new_peers": True,
                         "reserved_peer_ips": ["198.51.100.300"]}))

    A(case("save_round_trips_the_live_minimal_form_byte_equivalently",
           "Conformance checklist item: the live minimal form survives load -> save.",
           [op("save", "$TMPDIR/override_gossip_config.json")],
           ex_value({"written": True, "changed": True}, MINIMAL,
                    file_after={"path": "$TMPDIR/override_gossip_config.json",
                                "json_text": render(MINIMAL)}),
           input_config=MINIMAL))

    return c


def main():
    for d in (HTTP, CASES):
        for f in os.listdir(d):
            if f.endswith(".json"):
                os.remove(os.path.join(d, f))

    http = http_fixtures()
    for fx in http:
        p = os.path.join(HTTP, fx["name"] + ".json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(fx, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    cases = gossip_cases()
    names = set()
    for cs in cases:
        assert cs["name"] not in names, "duplicate case name %s" % cs["name"]
        names.add(cs["name"])
        p = os.path.join(CASES, cs["name"] + ".json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cs, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    print("wrote %d http fixtures, %d gossip cases" % (len(http), len(cases)))


if __name__ == "__main__":
    sys.exit(main())

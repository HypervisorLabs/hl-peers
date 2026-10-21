"""CLIENT_SPEC.md §9 item 5, against ``fixtures/http``
replayed through an injected transport. No socket is opened by this module."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from conftest import REPO_ROOT, FixtureTransport, Recorder
from hl_peers import (
    BadRequest,
    Envelope,
    FilterUnavailable,
    GossipConfigResult,
    Invalid,
    NetworkMapGraph,
    NetworkMapSummary,
    NotFound,
    NotModified,
    Peer,
    PeerApiClient,
    PeerAuction,
    PeerMempool,
    PeerPage,
    PeerQuery,
    RateLimited,
    TransportError,
    Unavailable,
    mempool_servers,
)
from hl_peers.transport import HttpResponse

FIELDS = json.loads((REPO_ROOT / "spec" / "peer-api-v1.fields.json").read_text(encoding="utf-8"))
GOLDEN_FIXTURE = json.loads(
    (REPO_ROOT / "fixtures" / "http" / "peers_format_gossip_config.json").read_text(
        encoding="utf-8"
    )
)
GOLDEN = GOLDEN_FIXTURE["expect"]["gossip_config_json_text"]


def make_client(
    transport: Optional[FixtureTransport] = None,
    recorder: Optional[Recorder] = None,
    **kw: Any,
) -> PeerApiClient:
    recorder = recorder or Recorder()
    return PeerApiClient(
        base_url="https://peers.test.invalid",
        transport=transport if transport is not None else FixtureTransport(),
        sleep=recorder.sleep,
        random=recorder.random,
        env={},
        **kw,
    )


def field_names(cls: Any) -> set:
    return {f.name for f in dataclasses.fields(cls)}


# ---------------------------------------------------------------------------
# the decoded types cover exactly the spec's field sets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cls,key,local_only",
    [
        (Peer, "peer", {"extra"}),
        (PeerMempool, "peer_mempool", set()),
        (PeerAuction, "peer_auction", set()),
        (Envelope, "envelope", {"extra"}),
    ],
)
def test_models_cover_exactly_the_spec_field_sets(cls: Any, key: str, local_only: set) -> None:
    """``spec/peer-api-v1.fields.json`` is generated from the vendored public OpenAPI;
    a field added or dropped upstream fails here until the model follows."""
    assert field_names(cls) - local_only == set(FIELDS[key])


RESULT_META = {"rate_limit", "etag", "from_cache", "status", "api_version", "raw"}


def test_peer_page_and_facets_cover_their_spec_field_sets() -> None:
    from hl_peers import Facets

    envelope = set(FIELDS["envelope"])
    page = (field_names(PeerPage) - RESULT_META - {"envelope"}) | envelope
    assert page == set(FIELDS["peer_list"])
    facets = (field_names(Facets) - RESULT_META - {"envelope"}) | envelope
    assert facets == set(FIELDS["facets"])


@pytest.mark.parametrize(
    "cls,key",
    [(NetworkMapGraph, "network_map_graph"), (NetworkMapSummary, "network_map_summary")],
)
def test_network_map_types_cover_their_spec_field_sets(cls: Any, key: str) -> None:
    assert field_names(cls) - RESULT_META == set(FIELDS[key])


def test_query_builder_covers_every_list_peers_parameter() -> None:
    for name in FIELDS["list_peers_params"]:
        assert callable(getattr(PeerQuery, name, None)), name


def test_closed_enums_match_the_spec() -> None:
    from hl_peers.query import CLOSED_ENUMS

    for name, values in FIELDS["enums"].items():
        assert list(CLOSED_ENUMS[name]) == values, name


# ---------------------------------------------------------------------------
# mempool / auction (§3.2) and their fixtures (§9 item 5)
# ---------------------------------------------------------------------------
def test_owner_query_1_whole_mempool_servers_in_de() -> None:
    transport = FixtureTransport()
    page = make_client(transport).list_peers(mempool_servers().cc("DE"))
    assert isinstance(page, PeerPage)
    assert transport.requests[-1][1].endswith("/peers?cc=DE&serves_mempool=true")
    (peer,) = page.peers
    assert peer.mempool == PeerMempool(
        serves=True,
        last_checked="2026-09-21T02:30:00Z",
        verdict="serves",
        first_frame_ms=412,
        source="probe",
    )
    assert peer.auction == PeerAuction(
        status="holds_slot",
        slot=3,
        since="2026-09-20T11:00:00Z",
        last_bid_at="2026-09-20T10:59:12Z",
    )


def test_serves_mempool_null_selects_the_unknown() -> None:
    transport = FixtureTransport()
    page = make_client(transport).list_peers(PeerQuery().serves_mempool(None))
    assert isinstance(page, PeerPage)
    assert transport.requests[-1][1].endswith("/peers?serves_mempool=null")
    assert page.peers
    for peer in page.peers:
        # 🔴 all-null is UNKNOWN, never "does not serve"
        assert peer.mempool == PeerMempool() and peer.mempool.serves is None
        assert peer.mempool.is_unknown
        assert peer.auction == PeerAuction()


def test_auction_filter_holds_slot_or_bidding() -> None:
    transport = FixtureTransport()
    page = make_client(transport).list_peers(PeerQuery().auction("holds_slot", "bidding"))
    assert isinstance(page, PeerPage)
    assert transport.requests[-1][1].endswith("/peers?auction=holds_slot%2Cbidding")
    assert [(p.id, p.auction.status, p.auction.slot) for p in page.peers] == [
        ("192.0.2.10", "holds_slot", 3),
        ("198.51.100.7", "bidding", None),
    ]


def test_the_removed_mempool_filter_is_a_plain_bad_request() -> None:
    """D-O: a ``mempool=`` filter answers ``400 filter-removed`` —
    ``BadRequest``, not ``FilterUnavailable``."""
    client = make_client()
    with pytest.raises(BadRequest) as excinfo:
        client.list_peers(PeerQuery({"mempool": "serving"}))
    assert type(excinfo.value) is BadRequest
    assert excinfo.value.parameter == "mempool"
    assert excinfo.value.api_version == "1"


def test_serves_mempool_filter_unavailable_is_raised_not_swallowed() -> None:
    client = make_client(FixtureTransport(scenario="mempool-unavailable"))
    with pytest.raises(FilterUnavailable) as excinfo:
        client.list_peers(mempool_servers())
    assert excinfo.value.filter == "serves_mempool"


def test_a_peer_without_mempool_or_auction_decodes_to_unknown() -> None:
    peer = Peer.from_json({"id": "192.0.2.10", "tier": None, "legacy_field": 7})
    assert peer.mempool == PeerMempool() and peer.auction == PeerAuction()
    assert peer.tier is None  # never defaulted to 0
    assert peer.extra == {"legacy_field": 7}  # not modelled; kept, not dropped


def test_unknown_keys_inside_mempool_and_auction_are_ignored() -> None:
    peer = Peer.from_json(
        {
            "id": "192.0.2.10",
            "mempool": {"serves": False, "future": 1},
            "auction": {"status": "a_new_status", "future": 2},
            "stream_quality": "good",
        }
    )
    assert peer.mempool.serves is False  # False stays False, never folded into None
    assert peer.auction.status == "a_new_status"  # an open set, decoded as-is
    assert "future" not in peer.extra and peer.extra == {"stream_quality": "good"}


def test_geo_fields_decode(client: PeerApiClient) -> None:
    detail = client.get_peer("192.0.2.10")
    peer = detail.peer
    assert (peer.cc, peer.cc_confidence, peer.cc_source) == ("DE", "high", "db+rtt-confirmed")
    assert (peer.asn, peer.org, peer.uptime) == ("AS64500", "Example Hosting", 0.97)
    assert peer.feeders == ["192.0.2.200"]
    assert detail.envelope.attribution and "IPinfo" in detail.envelope.attribution


# ---------------------------------------------------------------------------
# API-Version (§1)
# ---------------------------------------------------------------------------
def test_api_version_is_exposed_on_every_result_and_error(client: PeerApiClient) -> None:
    page = client.list_peers()
    assert isinstance(page, PeerPage) and page.api_version == "1"
    cached = client.list_peers()
    assert isinstance(cached, PeerPage) and cached.from_cache and cached.api_version == "1"
    assert isinstance(page.etag, str)
    not_modified = client.list_peers(if_none_match=page.etag)
    assert isinstance(not_modified, NotModified) and not_modified.api_version == "1"
    assert client.get_peer("192.0.2.10").api_version == "1"
    assert client.facets().api_version == "1"
    assert client.network_map_graph(max_tier=2).api_version == "1"
    assert client.network_map_summary().api_version == "1"
    assert client.list_peers_as_gossip_config(PeerQuery().cc("DE")).api_version == "1"
    with pytest.raises(BadRequest) as excinfo:
        client.list_peers(PeerQuery().asn("AS24940"))
    assert excinfo.value.api_version == "1"


def test_a_different_api_version_is_informational_never_an_error() -> None:
    class Future:
        def request(self, method: str, url: str, headers: Any, timeout: float) -> HttpResponse:
            response = FixtureTransport().request(method, url, headers, timeout)
            return HttpResponse(
                response.status, dict(response.headers, **{"api-version": "7"}), response.body
            )

    client = PeerApiClient("https://peers.test.invalid", transport=Future(), env={})
    page = client.list_peers()
    assert isinstance(page, PeerPage) and page.api_version == "7"


def test_a_missing_api_version_is_none() -> None:
    class Bare:
        def request(self, method: str, url: str, headers: Any, timeout: float) -> HttpResponse:
            response = FixtureTransport().request(method, url, headers, timeout)
            kept = {k: v for k, v in response.headers.items() if k != "api-version"}
            return HttpResponse(response.status, kept, response.body)

    page = PeerApiClient("https://peers.test.invalid", transport=Bare(), env={}).list_peers()
    assert isinstance(page, PeerPage) and page.api_version is None


# ---------------------------------------------------------------------------
# retries off by default (§5.2, D-Q)
# ---------------------------------------------------------------------------
def test_retries_are_off_by_default_one_503_is_one_request() -> None:
    transport, recorder = FixtureTransport(fail_first_n=1), Recorder()
    client = make_client(transport, recorder)
    assert client.max_retries == 0
    with pytest.raises(Unavailable):
        client.list_peers()
    assert len(transport.requests) == 1 and recorder.sleeps == []


def test_retries_are_off_by_default_for_transport_failures() -> None:
    transport, recorder = FixtureTransport(raise_transport_first_n=1), Recorder()
    with pytest.raises(TransportError):
        make_client(transport, recorder).list_peers()
    assert len(transport.requests) == 1 and recorder.sleeps == []


def test_explicit_max_retries_still_retries() -> None:
    transport, recorder = FixtureTransport(fail_first_n=2), Recorder()
    page = make_client(transport, recorder, max_retries=3).list_peers()
    assert isinstance(page, PeerPage) and page.available
    assert len(transport.requests) == 3 and len(recorder.sleeps) == 2


# ---------------------------------------------------------------------------
# RateLimited.reset_at from the injected clock (§5.1)
# ---------------------------------------------------------------------------
NOW = datetime(2026, 9, 21, 2, 35, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("rate-limited", NOW + timedelta(seconds=42)),  # now + Retry-After
        ("rate-limited-long", NOW + timedelta(seconds=3600)),
        ("rate-limited-http-date", NOW + timedelta(seconds=60)),  # the date, resolved
        ("rate-limited-bad-retry-after", NOW + timedelta(seconds=42)),  # now + RateLimit-Reset
        ("rate-limited-flood", NOW + timedelta(seconds=1)),  # the per-address flood 429
    ],
)
def test_reset_at_from_the_injected_clock(scenario: str, expected: datetime) -> None:
    client = make_client(FixtureTransport(scenario=scenario), clock=lambda: NOW)
    with pytest.raises(RateLimited) as excinfo:
        client.list_peers()
    error = excinfo.value
    assert error.reset_at == expected
    assert error.reset_at is not None and error.reset_at.tzinfo is not None
    assert error.reset_at.utcoffset() == timedelta(0)
    assert error.signup_url == "https://hypervisor.gg/docs/peer-api/authentication"
    assert error.api_version == "1"


def test_reset_at_is_none_without_either_header() -> None:
    class Bare429:
        def request(self, method: str, url: str, headers: Any, timeout: float) -> HttpResponse:
            return HttpResponse(429, {"content-type": "text/plain"}, b"slow down")

    client = PeerApiClient(
        "https://peers.test.invalid", transport=Bare429(), env={}, clock=lambda: NOW
    )
    with pytest.raises(RateLimited) as excinfo:
        client.list_peers()
    assert excinfo.value.reset_at is None and excinfo.value.signup_url is None


# ---------------------------------------------------------------------------
# list_peers_as_gossip_config (§4.7) — the golden test
# ---------------------------------------------------------------------------
def test_gossip_config_golden_to_json_and_saved_bytes(tmp_path: Path) -> None:
    transport = FixtureTransport()
    result = make_client(transport).list_peers_as_gossip_config(PeerQuery().cc("DE"))
    assert isinstance(result, GossipConfigResult)
    assert transport.requests[-1][1].endswith("/peers?cc=DE&format=gossip_config")
    assert result.status == 200 and result.from_cache is False
    assert result.etag == GOLDEN_FIXTURE["response"]["headers"]["ETag"]
    assert result.rate_limit.limit == 20 and result.api_version == "1"

    assert result.config.to_json() == GOLDEN
    assert result.to_json() == GOLDEN
    path = tmp_path / "override_gossip_config.json"
    saved = result.config.save(str(path))
    assert saved.written is True
    assert path.read_bytes() == GOLDEN.encode("utf-8")
    # the shortcut is the same writer: a second save is a no-op
    assert result.save(str(path)).written is False


def test_gossip_config_overrides_the_callers_format() -> None:
    transport = FixtureTransport()
    result = make_client(transport).list_peers_as_gossip_config(PeerQuery().cc("DE").format("json"))
    assert result.to_json() == GOLDEN
    assert transport.requests[-1][1].endswith("format=gossip_config")


def test_gossip_config_sends_a_callers_try_new_peers() -> None:
    transport = FixtureTransport()
    with pytest.raises(NotFound):  # no fixture for this exact query; the URL is the point
        make_client(transport).list_peers_as_gossip_config(
            PeerQuery().cc("DE").try_new_peers(False)
        )
    assert transport.requests[-1][1].endswith("cc=DE&format=gossip_config&try_new_peers=false")


def test_gossip_config_revalidates_through_the_cache() -> None:
    client = make_client()
    client.list_peers_as_gossip_config(PeerQuery().cc("DE"))
    again = client.list_peers_as_gossip_config(PeerQuery().cc("DE"))
    assert again.from_cache is True and again.status == 304
    assert again.to_json() == GOLDEN


def test_list_peers_still_refuses_the_gossip_format_and_names_the_helper() -> None:
    transport = FixtureTransport()
    with pytest.raises(Invalid) as excinfo:
        make_client(transport).list_peers(PeerQuery().format("gossip_config"))
    assert excinfo.value.parameter == "format"
    assert "list_peers_as_gossip_config" in excinfo.value.reason
    assert transport.requests == []  # refused before the wire


# ---------------------------------------------------------------------------
# network map (§4.8)
# ---------------------------------------------------------------------------
def test_network_map_graph() -> None:
    transport = FixtureTransport()
    graph = make_client(transport).network_map_graph(max_tier=2)
    assert transport.requests[-1][1].endswith("/network-map/graph?max_tier=2")
    assert (graph.version, graph.available, graph.stale) == ("v1", True, False)
    assert graph.generated_at == "2026-09-21T02:35:00Z"
    assert graph.operator_nodes.count is None
    assert (graph.node_count, graph.edge_count, graph.tier_count, graph.unreached) == (4, 3, 3, 0)
    assert graph.truncated is False
    assert [(t.tier, t.count) for t in graph.tiers] == [(0, 1), (1, 2), (2, 1)]
    assert graph.ips() == ["192.0.2.200", "192.0.2.10", "198.51.100.7", "192.0.2.11"]
    # nodes decode with the Peer decoder, mempool/auction included
    assert all(isinstance(n, Peer) for n in graph.nodes)
    assert graph.nodes[1].auction.status == "holds_slot"
    assert graph.nodes[3].mempool.serves is False
    assert [(e.from_, e.to, e.span) for e in graph.edges] == [
        ("192.0.2.200", "192.0.2.10", 1),
        ("192.0.2.200", "198.51.100.7", 1),
        ("192.0.2.10", "192.0.2.11", 1),
    ]
    assert graph.etag and graph.rate_limit.limit == 20


def test_network_map_summary() -> None:
    transport = FixtureTransport()
    summary = make_client(transport).network_map_summary()
    assert transport.requests[-1][1] == "https://peers.test.invalid/network-map/summary"
    assert summary.connectable == {"yes": 3, "no": 1, "unknown": 0}
    assert summary.serves_mempool == {"true": 2, "false": 1, "null": 1}
    assert summary.auction == {"holds_slot": 1, "bidding": 1, "bid_before": 1, "null": 1}
    assert summary.node_count == 4 and summary.edge_count == 3
    assert not hasattr(summary, "nodes") and not hasattr(summary, "truncated")


def test_network_map_available_false_is_returned_as_is() -> None:
    class Dark:
        def request(self, method: str, url: str, headers: Any, timeout: float) -> HttpResponse:
            body = {"version": "v1", "available": False, "stale": False, "generated_at": None}
            return HttpResponse(200, {"api-version": "1"}, json.dumps(body).encode())

    client = PeerApiClient("https://peers.test.invalid", transport=Dark(), env={})
    graph = client.network_map_graph()
    assert graph.available is False and graph.nodes == [] and graph.edges == []


def test_network_map_query_serialization_and_validation() -> None:
    from hl_peers._http import network_map_query_string as qs

    assert qs() == ""
    assert (
        qs(
            max_tier=0,
            show_unreached=False,
            hide_unreachable=True,
            connectable=["yes", "unknown"],
            cc="de,NL",
            operator="Example Operator",
            label="sentry",
        )
        == "cc=de%2CNL&connectable=yes%2Cunknown&hide_unreachable=true&label=sentry&"
        "max_tier=0&operator=Example%20Operator&show_unreached=false"
    )
    with pytest.raises(Invalid) as excinfo:
        qs(connectable=["maybe"])
    assert excinfo.value.parameter == "connectable"
    with pytest.raises(Invalid) as excinfo:
        qs(max_tier=-1)
    assert excinfo.value.parameter == "max_tier"


# ---------------------------------------------------------------------------
# facets take every filter (§4.4) and decode the new names
# ---------------------------------------------------------------------------
def test_facets_accept_every_filter() -> None:
    query = (
        PeerQuery()
        .chain("Mainnet")
        .cc("DE")
        .country("JP")
        .region("aws:eu-central-1")
        .asn("AS64500")
        .tier(1)
        .max_tier(2)
        .role("sentry")
        .label("sentry")
        .connectable("yes")
        .accepting(True)
        .state("responsive")
        .serves_mempool(True)
        .auction("bidding")
        .max_blocks_behind(5)
        .min_uptime(0.9)
        .max_latency_ms(80)
        .vantage("eu")
        .seen_within("1h")
        .version("1.0")
    )
    assert set(query.params) == set(FIELDS["facets_params"])
    transport = FixtureTransport()
    with pytest.raises(NotFound):  # no fixture for that exact query; the refusal is the point
        make_client(transport).facets(query)
    assert len(transport.requests) == 1  # it was sent, not refused


@pytest.mark.parametrize(
    "build",
    [
        lambda q: q.sort("tier"),
        lambda q: q.limit(5),
        lambda q: q.cursor("abc"),
        lambda q: q.fields("id"),
        lambda q: q.format("json"),
        lambda q: q.try_new_peers(True),
    ],
)
def test_facets_refuse_every_non_filter(build: Any) -> None:
    transport = FixtureTransport()
    query = build(PeerQuery().cc("DE"))
    with pytest.raises(Invalid) as excinfo:
        make_client(transport).facets(query)
    assert excinfo.value.parameter in {
        "sort",
        "limit",
        "cursor",
        "fields",
        "format",
        "try_new_peers",
    }
    assert transport.requests == []


def test_facets_decode_mempool_auction_and_geo_coverage(client: PeerApiClient) -> None:
    facets = client.facets()
    assert facets.facets["serves_mempool"].counts() == {"true": 143, "false": 388, "null": 381}
    assert facets.facets["auction"].counts()["holds_slot"] == 9
    assert facets.facet_available("uptime") is True
    assert facets.geo_coverage is not None and facets.geo_coverage["source"] == "dbip-lite"
    assert facets.envelope.attribution and "geo_coverage" not in facets.envelope.extra


# ---------------------------------------------------------------------------
# the builder's new fields (§2.1, §2.2)
# ---------------------------------------------------------------------------
def test_new_builder_fields_serialize() -> None:
    assert PeerQuery().country("jp", "de").to_params() == {"country": "JP,DE"}
    assert PeerQuery().serves_mempool(True, None).to_params() == {"serves_mempool": "true,null"}
    assert PeerQuery().serves_mempool([False]).to_params() == {"serves_mempool": "false"}
    assert PeerQuery().serves_mempool("true,null").to_params() == {"serves_mempool": "true,null"}
    assert PeerQuery().auction("bid_before", None).to_params() == {"auction": "bid_before,null"}
    assert PeerQuery().auction("bidding,null").to_params() == {"auction": "bidding,null"}
    assert PeerQuery().try_new_peers(False).to_params() == {"try_new_peers": "false"}
    assert PeerQuery().state("responsive", "open").to_query_string() == "state=responsive%2Copen"
    assert PeerQuery().serves_mempool(True).unset("serves_mempool").to_params() == {}


@pytest.mark.parametrize(
    "build,parameter",
    [
        (lambda q: q.serves_mempool("yes"), "serves_mempool"),
        (lambda q: q.serves_mempool("TRUE"), "serves_mempool"),
        (lambda q: q.auction("holding"), "auction"),
        (lambda q: q.auction("HOLDS_SLOT"), "auction"),
        (lambda q: q.state("Responsive"), "state"),
    ],
)
def test_new_closed_enums_are_validated_locally(build: Any, parameter: str) -> None:
    with pytest.raises(Invalid) as excinfo:
        build(PeerQuery())
    assert excinfo.value.parameter == parameter


def test_mempool_servers_preset() -> None:
    assert mempool_servers().to_params() == {"serves_mempool": "true"}
    doc = mempool_servers.__doc__ or ""
    assert "unknown" in doc and "excluded" in doc and "not" in doc


# ---------------------------------------------------------------------------
# the async client, same behaviour
# ---------------------------------------------------------------------------
class AsyncFixtureTransport:
    def __init__(self, inner: FixtureTransport) -> None:
        self.inner = inner

    async def request(
        self, method: str, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        return self.inner.request(method, url, headers, timeout)


def test_async_client_parity(tmp_path: Path) -> None:
    pytest.importorskip("httpx", reason="the [httpx] extra is not installed")
    from hl_peers.aio import AsyncPeerApiClient

    sleeps: List[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def run() -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        inner = FixtureTransport()
        client = AsyncPeerApiClient(
            "https://peers.test.invalid",
            transport=AsyncFixtureTransport(inner),
            sleep=sleep,
            clock=lambda: NOW,
            env={},
        )
        assert client.max_retries == 0 and client.base_path == ""
        result = await client.list_peers_as_gossip_config(PeerQuery().cc("DE"))
        out["golden"] = result.to_json()
        path = tmp_path / "override_gossip_config.json"
        result.config.save(str(path))
        out["saved"] = path.read_text(encoding="utf-8")
        out["graph"] = await client.network_map_graph(max_tier=2)
        out["summary"] = await client.network_map_summary()
        with pytest.raises(Invalid):
            await client.list_peers(PeerQuery().format("gossip_config"))
        with pytest.raises(Invalid):
            await client.facets(PeerQuery().limit(3))
        page = await client.list_peers(mempool_servers().cc("DE"))
        out["page"] = page

        failing = FixtureTransport(fail_first_n=1)
        off = AsyncPeerApiClient(
            "https://peers.test.invalid", transport=AsyncFixtureTransport(failing), env={}
        )
        with pytest.raises(Unavailable):
            await off.list_peers()
        out["requests_with_retries_off"] = len(failing.requests)

        limited = AsyncPeerApiClient(
            "https://peers.test.invalid",
            transport=AsyncFixtureTransport(FixtureTransport(scenario="rate-limited")),
            sleep=sleep,
            clock=lambda: NOW,
            env={},
        )
        with pytest.raises(RateLimited) as excinfo:
            await limited.list_peers()
        out["reset_at"] = excinfo.value.reset_at
        return out

    out = asyncio.run(run())
    assert out["golden"] == GOLDEN and out["saved"] == GOLDEN
    assert out["graph"].node_count == 4 and out["graph"].nodes[0].id == "192.0.2.200"
    assert out["summary"].connectable["yes"] == 3
    assert out["page"].api_version == "1" and out["page"].peers[0].mempool.serves is True
    assert out["requests_with_retries_off"] == 1
    assert out["reset_at"] == NOW + timedelta(seconds=42)
    assert sleeps == []

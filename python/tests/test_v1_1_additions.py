"""CLIENT_SPEC.md §9 item 6, against ``fixtures/http``
replayed through an injected transport. No socket is opened by this module."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from conftest import REPO_ROOT, FixtureTransport, Recorder
from hl_peers import (
    CityCluster,
    Invalid,
    PeerApiClient,
    PeerPage,
    PeerQuery,
    RateLimited,
)
from hl_peers.transport import HttpResponse

FIELDS = json.loads((REPO_ROOT / "spec" / "peer-api-v1.fields.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 9, 21, 2, 35, tzinfo=timezone.utc)
WITH_CITIES = (
    "IP address data is powered by IPinfo (https://ipinfo.io), CC BY-SA 4.0; "
    "IP Geolocation by DB-IP (https://db-ip.com), CC BY 4.0"
)


def make_client(transport: Optional[FixtureTransport] = None, **kw: Any) -> PeerApiClient:
    recorder = Recorder()
    return PeerApiClient(
        base_url="https://peers.test.invalid",
        transport=transport if transport is not None else FixtureTransport(),
        sleep=recorder.sleep,
        random=recorder.random,
        env={},
        **kw,
    )


def test_city_cluster_covers_its_spec_field_set() -> None:
    assert {f.name for f in dataclasses.fields(CityCluster)} == set(FIELDS["network_map_city"])


def test_network_map_methods_take_every_spec_parameter() -> None:
    import inspect

    for method in (PeerApiClient.network_map_graph, PeerApiClient.network_map_summary):
        params = set(inspect.signature(method).parameters) - {"self"}
        assert params == set(FIELDS["network_map_params"]), method.__name__


# --- listening ---------------------------------------------------------------
def test_listening_state_is_selectable_and_decoded() -> None:
    transport = FixtureTransport()
    page = make_client(transport).list_peers(PeerQuery().state("listening"))
    assert transport.requests[-1][1].endswith("/peers?state=listening")
    assert isinstance(page, PeerPage)
    (peer,) = page.peers
    assert peer.state == "listening"
    assert peer.tier is None  # never placed: NOT tier 0
    assert peer.latest_height is None and peer.blocks_behind is None
    assert peer.connectable == "unknown" and peer.city is None


def test_state_filter_is_still_closed() -> None:
    with pytest.raises(Invalid) as excinfo:
        PeerQuery().state("LISTENING")
    assert excinfo.value.parameter == "state"
    assert PeerQuery().state(["listening", "unreachable"]).to_query_string() == (
        "state=listening%2Cunreachable"
    )


# --- city / cities / attribution -------------------------------------------------
def test_every_peer_row_carries_city() -> None:
    page = make_client().list_peers()
    assert isinstance(page, PeerPage)
    assert [p.city for p in page.peers] == [None] * len(page.peers)
    assert all("city" not in p.extra for p in page.peers)


def test_graph_decodes_cities_and_attribution() -> None:
    graph = make_client().network_map_graph(max_tier=2)
    assert [n.city for n in graph.nodes] == [
        None,
        "DE-frankfurt-am-main",
        None,
        "DE-frankfurt-am-main",
    ]
    assert graph.cities == [
        CityCluster(
            id="DE-frankfurt-am-main",
            name="Frankfurt am Main",
            cc="DE",
            lat=50.1,
            lon=8.7,
            peers=2,
            tiers={"1": 1, "2": 1},
        )
    ]
    assert graph.attribution == WITH_CITIES


def test_summary_decodes_cities_and_attribution_d_s() -> None:
    summary = make_client().network_map_summary()
    assert summary.cities is not None and [c.id for c in summary.cities] == ["DE-frankfurt-am-main"]
    assert summary.attribution == WITH_CITIES


def test_absent_cities_stay_none_distinct_from_empty() -> None:
    graph = make_client().network_map_graph(serves_mempool=True, auction="holds_slot")
    assert graph.cities is None and graph.attribution is None

    class Empty:
        def request(self, method: str, url: str, headers: Any, timeout: float) -> HttpResponse:
            body = {"version": "v1", "available": True, "stale": False, "cities": []}
            return HttpResponse(200, {"api-version": "1"}, json.dumps(body).encode())

    client = PeerApiClient("https://peers.test.invalid", transport=Empty(), env={})
    assert client.network_map_graph().cities == []
    assert client.network_map_summary().cities == []


# --- the network-map mempool / auction filters --------------------------------------
def test_network_map_mempool_auction_filters_on_the_wire() -> None:
    transport = FixtureTransport()
    graph = make_client(transport).network_map_graph(serves_mempool=[True], auction=["holds_slot"])
    assert transport.requests[-1][1].endswith(
        "/network-map/graph?auction=holds_slot&serves_mempool=true"
    )
    assert graph.ips() == ["192.0.2.10"] and graph.edges == []
    assert graph.nodes[0].mempool.serves is True


def test_network_map_filter_serialization() -> None:
    from hl_peers._http import network_map_query_string as qs

    assert qs(serves_mempool=[True, None]) == "serves_mempool=true%2Cnull"
    assert qs(serves_mempool="false,null") == "serves_mempool=false%2Cnull"
    assert qs(auction=["bid_before", None]) == "auction=bid_before%2Cnull"
    assert qs(auction="bidding,null") == "auction=bidding%2Cnull"


@pytest.mark.parametrize(
    "kwargs,parameter",
    [
        ({"serves_mempool": "yes"}, "serves_mempool"),
        ({"serves_mempool": ["maybe"]}, "serves_mempool"),
        ({"auction": "holding"}, "auction"),
        ({"auction": ["HOLDS_SLOT"]}, "auction"),
    ],
)
@pytest.mark.parametrize("op", ["network_map_graph", "network_map_summary"])
def test_network_map_filters_refused_locally(op: str, kwargs: Any, parameter: str) -> None:
    transport = FixtureTransport()
    with pytest.raises(Invalid) as excinfo:
        getattr(make_client(transport), op)(**kwargs)
    assert excinfo.value.parameter == parameter
    assert transport.requests == []  # refused before any request


# --- the flood 429 -----------------------------------------------------------------
def test_flood_429_is_rate_limited_with_its_own_window() -> None:
    client = make_client(FixtureTransport(scenario="rate-limited-flood"), clock=lambda: NOW)
    with pytest.raises(RateLimited) as excinfo:
        client.list_peers()
    error = excinfo.value
    assert error.retry_after == 1
    assert error.reset_at == NOW + timedelta(seconds=1)
    assert (error.rate_limit.limit, error.rate_limit.remaining, error.rate_limit.reset) == (
        100,
        0,
        1,
    )
    assert error.rate_limit.policy == "100;w=1"
    assert error.problem is not None and error.problem["title"] == "Too many requests"

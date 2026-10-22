"""Testnet (beta): the ``network`` option (CLIENT_SPEC §1, §2.1, §4.3, §4.4, §4.8,
§9 item 7, §10 D-U). No network access: every request is replayed from
``fixtures/http`` or answered by a local stub."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Tuple

import pytest

from conftest import FixtureTransport, Recorder
from hl_peers import (
    BadRequest,
    Invalid,
    NetworkMapGraph,
    NetworkMapSummary,
    PeerApiClient,
    PeerPage,
    PeerQuery,
    cli,
)
from hl_peers.transport import HttpResponse

BASE = "https://peers.test.invalid"


def make_client(transport: Any = None, **kw: Any) -> PeerApiClient:
    recorder = Recorder()
    return PeerApiClient(
        base_url=BASE,
        transport=transport if transport is not None else FixtureTransport(),
        sleep=recorder.sleep,
        random=recorder.random,
        env={},
        **kw,
    )


# ---------------------------------------------------------------------------
# a stub that answers every route with a minimal body and records the URLs
# ---------------------------------------------------------------------------
class StubTransport:
    def __init__(self) -> None:
        self.urls: List[str] = []

    def request(
        self, method: str, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        self.urls.append(url)
        path = url.split("?", 1)[0][len(BASE) :]
        body: Dict[str, Any] = {"version": "v1", "available": True, "stale": False}
        if path == "/peers":
            body.update(peers=[], count=0, total=0, next_cursor=None)
        elif path == "/peers/facets":
            body.update(facets={}, total=0)
        elif path.startswith("/peers/"):
            body.update(peer={"id": path.rsplit("/", 1)[-1]})
        return HttpResponse(200, {"content-type": "application/json"}, json.dumps(body).encode())


ROUTES: List[Tuple[str, Callable[..., Any], str]] = [
    ("list_peers", lambda c, **kw: c.list_peers(PeerQuery().cc("DE"), **kw), "/peers?cc=DE"),
    (
        "list_peers_as_gossip_config",
        lambda c, **kw: c.list_peers_as_gossip_config(PeerQuery().cc("DE"), **kw),
        "/peers?cc=DE&format=gossip_config",
    ),
    ("facets", lambda c, **kw: c.facets(PeerQuery().cc("DE"), **kw), "/peers/facets?cc=DE"),
    ("get_peer", lambda c, **kw: c.get_peer("192.0.2.10", **kw), "/peers/192.0.2.10"),
    (
        "network_map_graph",
        lambda c, **kw: c.network_map_graph(max_tier=2, **kw),
        "/network-map/graph?max_tier=2",
    ),
    (
        "network_map_summary",
        lambda c, **kw: c.network_map_summary(max_tier=2, **kw),
        "/network-map/summary?max_tier=2",
    ),
]
ROUTE_IDS = [r[0] for r in ROUTES]


def _call(client: PeerApiClient, name: str, call: Callable[..., Any]) -> None:
    try:
        call(client)
    except Exception:  # the gossip_config route cannot decode the stub body; only the URL matters
        if name != "list_peers_as_gossip_config":
            raise


@pytest.mark.parametrize("name,call,url", ROUTES, ids=ROUTE_IDS)
def test_mainnet_golden_url_is_unchanged_without_network(
    name: str, call: Callable[..., Any], url: str
) -> None:
    """🔴 Unset ⇒ no ``network`` key at all: byte-identical to a 1.1 request."""
    stub = StubTransport()
    _call(make_client(stub), name, call)
    assert stub.urls == [BASE + url]


@pytest.mark.parametrize("name,call,url", ROUTES, ids=ROUTE_IDS)
def test_client_default_network_reaches_every_route(
    name: str, call: Callable[..., Any], url: str
) -> None:
    stub = StubTransport()
    _call(make_client(stub, network="testnet"), name, call)
    (sent,) = stub.urls
    assert "network=testnet" in sent
    # lexicographic order holds: the param sits where its name sorts
    query = sent.split("?", 1)[1].split("&")
    assert query == sorted(query)


def _with_network(name: str, call: Callable[..., Any], network: str) -> Callable[..., Any]:
    """The per-call form of ``network`` on each route."""
    if name in ("get_peer", "network_map_graph", "network_map_summary"):
        return lambda c: call(c, network=network)
    builders = {
        "list_peers": lambda c: c.list_peers(PeerQuery().cc("DE").network(network)),
        "list_peers_as_gossip_config": lambda c: c.list_peers_as_gossip_config(
            PeerQuery().cc("DE").network(network)
        ),
        "facets": lambda c: c.facets(PeerQuery().cc("DE").network(network)),
    }
    return builders[name]


@pytest.mark.parametrize("name,call,url", ROUTES, ids=ROUTE_IDS)
def test_per_call_network_wins_over_the_client_default(
    name: str, call: Callable[..., Any], url: str
) -> None:
    stub = StubTransport()
    client = make_client(stub, network="testnet")
    _call(client, name, _with_network(name, call, "mainnet"))
    (sent,) = stub.urls
    assert "network=mainnet" in sent and "testnet" not in sent


@pytest.mark.parametrize("name,call,url", ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize("bad", ["Testnet", "TESTNET", "devnet", ""])
def test_out_of_enum_network_is_invalid_before_any_request(
    name: str, call: Callable[..., Any], url: str, bad: str
) -> None:
    stub = StubTransport()
    with pytest.raises(Invalid) as excinfo:
        _with_network(name, call, bad)(make_client(stub))
    assert excinfo.value.parameter == "network"
    assert stub.urls == []


@pytest.mark.parametrize("bad", ["Testnet", "devnet"])
def test_out_of_enum_client_default_is_invalid_at_construction(bad: str) -> None:
    with pytest.raises(Invalid) as excinfo:
        make_client(StubTransport(), network=bad)
    assert excinfo.value.parameter == "network"


def test_network_and_chain_are_different_enums() -> None:
    with pytest.raises(Invalid):
        PeerQuery().network("Testnet")  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        PeerQuery().chain("testnet")  # type: ignore[arg-type]
    assert (
        PeerQuery().chain("Testnet").network("testnet").to_query_string()
        == "chain=Testnet&network=testnet"
    )
    assert PeerQuery().network("testnet").network(None).to_query_string() == ""


def test_iter_peers_carries_the_client_default_on_every_page() -> None:
    stub = StubTransport()
    list(make_client(stub, network="testnet").iter_peers(PeerQuery().limit(2)))
    assert stub.urls == [BASE + "/peers?limit=2&network=testnet"]


# ---------------------------------------------------------------------------
# the testnet fixtures, decoded (§9 item 7)
# ---------------------------------------------------------------------------
def test_peers_testnet_fixture() -> None:
    transport = FixtureTransport()
    page = make_client(transport).list_peers(PeerQuery().network("testnet"))
    assert transport.requests[-1][1] == BASE + "/peers?network=testnet"
    assert isinstance(page, PeerPage)
    assert page.network == "testnet" and page.envelope.network == "testnet"
    assert page.chain == "Testnet"
    assert [p.version for p in page.peers] == ["1550", "1550", "1549", None]
    first = page.peers[0]
    assert (first.cc, first.asn, first.org, first.region) == (
        "JP",
        "AS64502",
        "Example Cloud",
        "aws:ap-northeast-1",
    )
    assert "network" not in page.envelope.extra


def test_peers_testnet_fixture_via_the_client_default() -> None:
    transport = FixtureTransport()
    page = make_client(transport, network="testnet").list_peers()
    assert transport.requests[-1][1] == BASE + "/peers?network=testnet"
    assert isinstance(page, PeerPage) and page.network == "testnet"


def test_peer_found_testnet_fixture() -> None:
    transport = FixtureTransport()
    detail = make_client(transport).get_peer("192.0.2.50", network="testnet")
    assert transport.requests[-1][1] == BASE + "/peers/192.0.2.50?network=testnet"
    assert detail.network == "testnet" and detail.envelope.chain == "Testnet"
    assert detail.peer.version == "1550"
    assert (detail.peer.cc, detail.peer.asn, detail.peer.org) == ("JP", "AS64502", "Example Cloud")
    assert detail.peer.region == "aws:ap-northeast-1"


def test_facets_testnet_fixture() -> None:
    transport = FixtureTransport()
    facets = make_client(transport).facets(PeerQuery().network("testnet"))
    assert transport.requests[-1][1] == BASE + "/peers/facets?network=testnet"
    assert facets.network == "testnet" and facets.envelope.chain == "Testnet"
    assert facets.facets["version"].counts() == {"1550": 2, "1549": 1, "null": 1}


def test_network_map_graph_testnet_fixture() -> None:
    transport = FixtureTransport()
    graph = make_client(transport).network_map_graph(network="testnet")
    assert transport.requests[-1][1] == BASE + "/network-map/graph?network=testnet"
    assert isinstance(graph, NetworkMapGraph)
    assert graph.network == "testnet"
    assert [n.version for n in graph.nodes] == ["1550", "1550", "1549", None]
    assert graph.nodes[0].cc == "JP" and graph.nodes[0].asn == "AS64502"


def test_network_map_summary_testnet_fixture() -> None:
    transport = FixtureTransport()
    summary = make_client(transport, network="testnet").network_map_summary()
    assert transport.requests[-1][1] == BASE + "/network-map/summary?network=testnet"
    assert isinstance(summary, NetworkMapSummary)
    assert summary.network == "testnet"


def test_mainnet_bodies_decode_network_as_none() -> None:
    client = make_client()
    page = client.list_peers(PeerQuery().cc("DE"))
    assert isinstance(page, PeerPage) and page.network is None
    assert client.network_map_graph(max_tier=2).network is None
    assert client.network_map_summary().network is None
    assert all(p.version is None for p in page.peers)


def test_network_not_served_is_bad_request_with_parameter_network() -> None:
    transport = FixtureTransport(scenario="network-not-served")
    with pytest.raises(BadRequest) as excinfo:
        make_client(transport).list_peers(PeerQuery().network("testnet"))
    error = excinfo.value
    assert type(error) is BadRequest  # no new error variant (§10 D-U)
    assert error.parameter == "network"
    assert error.allowed == ["mainnet", "testnet"]


# ---------------------------------------------------------------------------
# the async twin, with a fixture-backed async transport (no httpx, no socket)
# ---------------------------------------------------------------------------
class AsyncFixtureTransport:
    def __init__(self) -> None:
        self.inner = FixtureTransport()

    async def request(
        self, method: str, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        return self.inner.request(method, url, headers, timeout)

    async def aclose(self) -> None:
        return None


def test_async_client_takes_network_everywhere() -> None:
    pytest.importorskip("httpx")
    from hl_peers.aio import AsyncPeerApiClient

    async def run() -> List[str]:
        transport = AsyncFixtureTransport()
        client = AsyncPeerApiClient(BASE, transport=transport, env={}, network="testnet")
        page = await client.list_peers()
        assert isinstance(page, PeerPage) and page.network == "testnet"
        detail = await client.get_peer("192.0.2.50")
        assert detail.peer.version == "1550"
        assert (await client.facets()).network == "testnet"
        assert (await client.network_map_graph()).network == "testnet"
        assert (await client.network_map_summary()).network == "testnet"
        with pytest.raises(Invalid):
            await client.get_peer("192.0.2.50", network="Testnet")  # type: ignore[arg-type]
        return [r[1] for r in transport.inner.requests]

    urls = asyncio.run(run())
    assert urls == [
        BASE + "/peers?network=testnet",
        BASE + "/peers/192.0.2.50?network=testnet",
        BASE + "/peers/facets?network=testnet",
        BASE + "/network-map/graph?network=testnet",
        BASE + "/network-map/summary?network=testnet",
    ]


# ---------------------------------------------------------------------------
# CLI --network, in process against a fixture-backed client
# ---------------------------------------------------------------------------
def _cli(monkeypatch: pytest.MonkeyPatch, args: List[str]) -> Tuple[int, str, str, List[str]]:
    transport = FixtureTransport()

    def fake_make_client(ns: Any) -> PeerApiClient:
        return make_client(transport)

    monkeypatch.setattr(cli, "make_client", fake_make_client)
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(args, out, err)
    return code, out.getvalue(), err.getvalue(), [r[1] for r in transport.requests]


def test_cli_query_network(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, _, urls = _cli(monkeypatch, ["query", "--network", "testnet", "--output", "ips"])
    assert code == 0
    assert urls == [BASE + "/peers?network=testnet"]
    assert out.split() == ["192.0.2.50", "198.51.100.60", "203.0.113.70", "203.0.113.71"]


def test_cli_facets_and_peer_network(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, _, urls = _cli(monkeypatch, ["facets", "--network", "testnet"])
    assert code == 0 and urls == [BASE + "/peers/facets?network=testnet"]
    assert json.loads(out)["network"] == "testnet"
    code, out, _, urls = _cli(monkeypatch, ["peer", "192.0.2.50", "--network", "testnet"])
    assert code == 0 and urls == [BASE + "/peers/192.0.2.50?network=testnet"]
    assert json.loads(out)["peer"]["version"] == "1550"


def test_cli_gossip_update_network(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, urls = _cli(
        monkeypatch, ["gossip", "update", "--network", "testnet", "--stdout"]
    )
    assert code == 0, err
    assert urls == [BASE + "/peers?network=testnet"]
    assert json.loads(out)["chain"] == "Testnet"


def test_cli_without_network_sends_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, urls = _cli(monkeypatch, ["query", "--cc", "DE", "--output", "ips"])
    assert urls == [BASE + "/peers?cc=DE"]


def test_cli_rejects_out_of_enum_network(monkeypatch: pytest.MonkeyPatch) -> None:
    code, _, err, urls = _cli(monkeypatch, ["query", "--network", "Testnet"])
    assert code == cli.EXIT_CLIENT_ERROR and urls == []
    assert "network" in err


def test_readme_documents_testnet() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "Testnet (beta)" in readme
    assert 'network="testnet"' in readme

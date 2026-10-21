"""The query builder's validation and serialization, and ``update_gossip_config``."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FixtureTransport, Recorder
from hl_peers import (
    GossipConfig,
    Invalid,
    PeerApiClient,
    PeerQuery,
    Unavailable,
    update_gossip_config,
)


# ---------------------------------------------------------------------------
# query builder
# ---------------------------------------------------------------------------
def test_setters_are_immutable() -> None:
    base = PeerQuery().cc("DE")
    other = base.cc("NL")
    assert base.to_params() == {"cc": "DE"}
    assert other.to_params() == {"cc": "NL"}


def test_csv_is_joined_for_you_and_a_pre_joined_string_is_accepted() -> None:
    assert PeerQuery().cc("DE", "NL").to_params()["cc"] == "DE,NL"
    assert PeerQuery().cc(["DE", "NL"]).to_params()["cc"] == "DE,NL"
    assert PeerQuery().cc("de,nl").to_params()["cc"] == "DE,NL"


def test_query_string_is_lexicographic_and_percent_encodes_the_comma() -> None:
    query = PeerQuery().tier(1).cc("DE", "NL").sort("random")
    assert query.to_query_string() == "cc=DE%2CNL&sort=random&tier=1"


def test_unset_and_empty_list_parameters_are_omitted() -> None:
    assert PeerQuery().cc("DE").cc(None).to_query_string() == ""
    assert PeerQuery().cc([]).to_query_string() == ""


def test_tier_null_is_a_selectable_member() -> None:
    assert PeerQuery().tier(1, None).to_params()["tier"] == "1,null"


def test_tier_and_max_tier_are_different_questions() -> None:
    query = PeerQuery().tier(1).max_tier(2)
    assert query.to_params() == {"max_tier": "2", "tier": "1"}


@pytest.mark.parametrize(
    "build",
    [
        lambda q: q.seen_within("1 hour"),
        lambda q: q.min_uptime(1.5),
        lambda q: q.max_tier(-1),
        lambda q: q.max_blocks_behind(-1),
        lambda q: q.max_latency_ms(-1),
        lambda q: q.limit(0),
        lambda q: q.chain("mainnet"),
        lambda q: q.sort("tier_desc"),
        lambda q: q.state("packed"),
        lambda q: q.connectable("true"),
        lambda q: q.format("yaml"),
    ],
)
def test_the_thin_client_side_validation(build) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(Invalid):
        build(PeerQuery())


def test_max_latency_ms_requires_a_vantage() -> None:
    with pytest.raises(Invalid) as excinfo:
        PeerQuery().max_latency_ms(80).to_params()
    assert excinfo.value.parameter == "vantage"
    assert PeerQuery().max_latency_ms(80).vantage("eu").to_params()["vantage"] == "eu"


def test_open_sets_are_not_validated_locally() -> None:
    # cc/region/asn/role/label/version/fields may grow additively within v1: send
    # them and let the server name the problem.
    params = (
        PeerQuery()
        .cc("ZZ")
        .role("future-role")
        .label("future:label")
        .fields("id,tier,whatever")
        .to_params()
    )
    assert params["cc"] == "ZZ" and params["role"] == "future-role"


def test_limit_is_not_clamped_client_side() -> None:
    assert PeerQuery().limit(100_000).to_params()["limit"] == "100000"


def test_no_sort_is_sent_unless_asked() -> None:
    assert "sort" not in PeerQuery().cc("DE").to_params()


# ---------------------------------------------------------------------------
# update_gossip_config
# ---------------------------------------------------------------------------
def make_client(**kw):  # type: ignore[no-untyped-def]
    recorder = Recorder()
    return PeerApiClient(
        base_url="https://peers.test.invalid",
        transport=FixtureTransport(**kw),
        sleep=recorder.sleep,
        random=recorder.random,
        env={},
    )


def test_update_without_a_path_renders_to_stdout_style_json() -> None:

    result = update_gossip_config(make_client(), PeerQuery().cc("DE"))
    assert result.written_path is None
    assert result.json.startswith('{\n  "chain": "Mainnet"')
    assert result.config.root_ips() == ["192.0.2.10", "192.0.2.11"]
    assert result.envelope is not None and result.envelope.chain == "Mainnet"


def test_update_writes_and_is_idempotent(tmp_path: Path) -> None:

    path = str(tmp_path / "override_gossip_config.json")
    first = update_gossip_config(make_client(), PeerQuery().cc("DE"), path)
    assert first.written_path == path and first.changed is True
    second = update_gossip_config(make_client(), PeerQuery().cc("DE"), path)
    assert second.written_path is None and second.changed is False


def test_update_pins_keep_existing_and_honours_exclude(tmp_path: Path) -> None:

    path = str(tmp_path / "override_gossip_config.json")
    GossipConfig.new("Mainnet", ["203.0.113.9"]).set_try_new_peers(True).save(path)
    result = update_gossip_config(
        make_client(),
        PeerQuery().cc("DE"),
        path,
        keep_existing=["203.0.113.9"],
        exclude=["192.0.2.11"],
    )
    assert result.config.root_ips() == ["203.0.113.9", "192.0.2.10"]


def test_update_refuses_an_unavailable_graph_before_touching_a_file(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    client = make_client(scenario="unavailable")
    with pytest.raises(Unavailable):
        update_gossip_config(client, PeerQuery(), str(path))
    assert not path.exists()


def test_update_refuses_to_guess_a_chain() -> None:
    with pytest.raises(Invalid) as excinfo:
        update_gossip_config(peers=["192.0.2.10"])
    assert excinfo.value.parameter == "chain"


def test_update_works_offline_from_an_explicit_peer_list(tmp_path: Path) -> None:
    path = str(tmp_path / "override_gossip_config.json")
    result = update_gossip_config(
        peers=["192.0.2.10", "192.0.2.11"], path=path, chain="Mainnet", try_new_peers=False
    )
    assert result.changed and result.written_path == path
    assert "truncated" not in result.warning_codes


def test_update_dry_run_writes_nothing(tmp_path: Path) -> None:

    path = str(tmp_path / "override_gossip_config.json")
    GossipConfig.new("Mainnet", ["203.0.113.9"]).save(path)
    before = Path(path).read_text(encoding="utf-8")
    result = update_gossip_config(make_client(), PeerQuery().cc("DE"), path, dry_run=True)
    assert result.written_path is None
    assert "192.0.2.10" in result.diff
    assert Path(path).read_text(encoding="utf-8") == before


def test_overfetch_bounds_the_walk() -> None:
    client = make_client()
    update_gossip_config(client, PeerQuery().limit(2).sort("tier"), max_roots=2, overfetch=1)
    # max_items = max_roots * overfetch = 2, so one page is enough
    assert len(client.transport.requests) == 1  # type: ignore[attr-defined]


def test_update_takes_file_mode_not_mode(tmp_path: Path) -> None:
    import stat as stat_module

    path = str(tmp_path / "override_gossip_config.json")
    result = update_gossip_config(
        peers=["192.0.2.10"], path=path, chain="Mainnet", mode="replace", file_mode=0o640
    )
    assert result.written_path == path
    assert stat_module.S_IMODE(Path(path).stat().st_mode) == 0o640

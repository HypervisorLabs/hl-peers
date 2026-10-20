"""``hl-peers`` end to end, as a subprocess against the spawned mock server."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from hl_peers import GossipConfig

pytest_plugins: List[str] = []

HL_PEERS = shutil.which("hl-peers")
BASE_CMD = [HL_PEERS] if HL_PEERS else [sys.executable, "-m", "hl_peers.cli"]


def run(args: List[str], base_url: str = "") -> subprocess.CompletedProcess:
    cmd = list(BASE_CMD) + args
    if base_url:
        cmd += ["--base-url", base_url]
    env = dict(os.environ)
    env.pop("HL_PEER_API_BASE_URL", None)
    env.pop("HL_PEER_API_KEY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)


# the mock_server fixture lives in test_integration_mock.py; import it here
from test_integration_mock import mock_server  # noqa: E402,F401


def test_query_output_ips(mock_server: str) -> None:
    result = run(["query", "--cc", "DE", "--output", "ips"], mock_server)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["192.0.2.10", "192.0.2.11"]


def test_query_output_table_and_json(mock_server: str) -> None:
    table = run(["query", "--tier", "1", "--output", "table"], mock_server)
    assert table.returncode == 0
    assert table.stdout.splitlines()[0].split() == [
        "ip",
        "tier",
        "cc",
        "connect",
        "name",
    ]

    as_json = run(["query", "--label", "sentry", "--output", "json"], mock_server)
    body = json.loads(as_json.stdout)
    assert body["peers"][0]["id"] == "192.0.2.200"
    assert body["available"] is True


def test_query_all_walks_every_page(mock_server: str) -> None:
    result = run(
        ["query", "--limit", "2", "--sort", "tier", "--all", "--output", "ips"], mock_server
    )
    assert result.returncode == 0
    assert len(result.stdout.split()) == 5


def test_peer_and_facets(mock_server: str) -> None:
    peer = run(["peer", "192.0.2.10"], mock_server)
    assert json.loads(peer.stdout)["peer"]["cc"] == "DE"
    facets = run(["facets"], mock_server)
    assert json.loads(facets.stdout)["facets"]["asn"]["available"] is False


def test_gossip_update_dry_run_writes_nothing(mock_server: str, tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    GossipConfig.new("Mainnet", ["203.0.113.9"]).set_try_new_peers(True).save(str(path))
    before = path.read_text(encoding="utf-8")
    result = run(
        [
            "gossip",
            "update",
            "--path",
            str(path),
            "--cc",
            "DE",
            "--max-roots",
            "8",
            "--dry-run",
        ],
        mock_server,
    )
    assert result.returncode == 0
    assert "192.0.2.10" in result.stdout  # the unified diff
    assert path.read_text(encoding="utf-8") == before


def test_gossip_update_writes_and_says_it_did_not_restart_anything(
    mock_server: str, tmp_path: Path
) -> None:
    path = tmp_path / "override_gossip_config.json"
    result = run(
        [
            "gossip",
            "update",
            "--path",
            str(path),
            "--cc",
            "DE",
            "--file-mode",
            "0640",
        ],
        mock_server,
    )
    assert result.returncode == 0, result.stderr
    assert GossipConfig.load(str(path)).root_ips() == ["192.0.2.10", "192.0.2.11"]
    assert "The node reads this file when it starts" in result.stderr
    assert "--restart" not in result.stderr
    assert oct(os.stat(path).st_mode & 0o777) == "0o640"  # --file-mode, not --mode


def test_gossip_update_stdout(mock_server: str) -> None:
    result = run(["gossip", "update", "--stdout", "--cc", "DE"], mock_server)
    assert result.returncode == 0
    assert json.loads(result.stdout)["root_node_ips"] == [
        {"Ip": "192.0.2.10"},
        {"Ip": "192.0.2.11"},
    ]


def test_gossip_update_keep_and_exclude(mock_server: str, tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    GossipConfig.new("Mainnet", ["203.0.113.9"]).save(str(path))
    result = run(
        [
            "gossip",
            "update",
            "--path",
            str(path),
            "--cc",
            "DE",
            "--keep",
            "203.0.113.9",
            "--exclude",
            "192.0.2.11",
        ],
        mock_server,
    )
    assert result.returncode == 0, result.stderr
    assert GossipConfig.load(str(path)).root_ips() == ["203.0.113.9", "192.0.2.10"]


def test_gossip_show_validate_add_remove(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    GossipConfig.new("Mainnet", ["192.0.2.10"]).set_try_new_peers(False).save(str(path))

    show = run(["gossip", "show", "--path", str(path)])
    assert show.returncode == 0 and '"Ip": "192.0.2.10"' in show.stdout

    validate = run(["gossip", "validate", "--path", str(path)])
    assert validate.returncode == 0 and "ok:" in validate.stdout

    add = run(["gossip", "add-root", "203.0.113.4", "--path", str(path)])
    assert add.returncode == 0
    assert GossipConfig.load(str(path)).root_ips() == ["192.0.2.10", "203.0.113.4"]
    assert "The node reads this file when it starts" in add.stderr

    remove = run(["gossip", "remove-root", "203.0.113.4", "--path", str(path)])
    assert remove.returncode == 0
    assert GossipConfig.load(str(path)).root_ips() == ["192.0.2.10"]


def test_validate_warnings_go_to_stderr(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    config = GossipConfig.new(None, ["192.0.2.10"])
    path.write_text(config.to_json(), encoding="utf-8")
    result = run(["gossip", "validate", "--path", str(path)])
    assert result.returncode == 0
    assert "chain_absent" in result.stderr


def test_loud_warnings_survive_quiet(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    roots = ["203.0.113.%d" % i for i in range(1, 26)]
    GossipConfig.new("Mainnet", roots).set_try_new_peers(True).save(str(path))
    result = run(["gossip", "validate", "--path", str(path), "--quiet"])
    assert "many_roots_gt_24" in result.stderr


@pytest.mark.parametrize(
    "args,code",
    [
        (["gossip", "show", "--path", "/nonexistent/override_gossip_config.json"], 5),
        (["query", "--seen-within", "1 hour"], 1),
        ([], 2),
    ],
)
def test_exit_codes(args: List[str], code: int, mock_server: str) -> None:
    result = run(args, mock_server if args else "")
    assert result.returncode == code, result.stderr


def test_filter_unavailable_exits_1(mock_server: str) -> None:
    result = run(["query", "--asn", "AS24940"], mock_server)
    assert result.returncode == 1
    assert "FilterUnavailable" in result.stderr


def test_json_errors(mock_server: str) -> None:
    result = run(["query", "--asn", "AS24940", "--json-errors"], mock_server)
    payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert payload["kind"] == "FilterUnavailable"
    assert payload["problem"]["parameter"] == "asn"


def test_query_accepts_chain_and_reaches_chain_not_served(mock_server: str) -> None:
    result = run(["query", "--chain", "Testnet"], mock_server)
    assert result.returncode == 1
    assert "ChainNotServed" in result.stderr


def test_add_root_takes_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    GossipConfig.new("Mainnet", ["192.0.2.10"]).set_try_new_peers(True).save(str(path))
    result = run(["gossip", "add-root", "203.0.113.4", "--path", str(path), "--file-mode", "0640"])
    assert result.returncode == 0, result.stderr
    assert oct(os.stat(path).st_mode & 0o777) == "0o640"


def test_there_is_no_restart_flag() -> None:
    help_text = run(["gossip", "update", "--help"]).stdout
    assert "--restart" not in help_text and "--apply" not in help_text


# ---------------------------------------------------------------------------
# in-process, for the paths a subprocess hides from coverage
# ---------------------------------------------------------------------------
import io  # noqa: E402

from hl_peers import cli  # noqa: E402


def call(args: List[str]) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(args, out, err)
    return code, out.getvalue(), err.getvalue()


def test_in_process_query_outputs(mock_server: str) -> None:
    code, out, err = call(["query", "--base-url", mock_server, "--output", "ips"])
    assert code == 0 and len(out.split()) == 5
    code, out, _ = call(["query", "--base-url", mock_server, "--output", "table"])
    assert code == 0 and "192.0.2.200" in out
    code, out, _ = call(["query", "--base-url", mock_server, "--cc", "DE"])
    assert json.loads(out)["count"] == 2


def test_in_process_available_false_and_stale_warn(mock_server: str) -> None:
    # the scenario fixtures need a header the CLI never sends, so this only
    # exercises the plain path; the warning text lives next to it
    code, out, err = call(
        [
            "query",
            "--base-url",
            mock_server,
            "--limit",
            "2",
            "--sort",
            "tier",
            "--all",
            "--max-items",
            "3",
            "--output",
            "ips",
        ]
    )
    assert code == 0
    assert "--max-items" in err


def test_in_process_gossip_paths(mock_server: str, tmp_path: Path) -> None:
    path = str(tmp_path / "override_gossip_config.json")
    code, out, err = call(
        [
            "gossip",
            "update",
            "--base-url",
            mock_server,
            "--path",
            path,
            "--cc",
            "DE",
        ]
    )
    assert code == 0 and "when it starts" in err
    code, out, err = call(
        [
            "gossip",
            "update",
            "--base-url",
            mock_server,
            "--path",
            path,
            "--cc",
            "DE",
        ]
    )
    assert code == 0 and "no change" in err
    code, out, err = call(["gossip", "add-root", "203.0.113.7", "--path", path, "--stdout"])
    assert code == 0 and "203.0.113.7" in out
    code, out, err = call(["gossip", "add-root", "203.0.113.7", "--path", path, "--dry-run"])
    assert code == 0 and '+      "Ip": "203.0.113.7"' in out
    code, out, err = call(["gossip", "remove-root", "203.0.113.99", "--path", path])
    assert code == 0 and "no change" in err


def test_in_process_exit_codes(mock_server: str) -> None:
    assert call(["gossip"])[0] == cli.EXIT_USAGE
    assert call([])[0] == cli.EXIT_USAGE
    code, _, err = call(["peer", "198.51.100.254", "--base-url", mock_server])
    assert code == cli.EXIT_NOT_FOUND
    code, _, err = call(["facets", "--base-url", mock_server])
    assert code == 0
    # 🪤 CLIENT_SPEC §8 lists --chain for `facets` and `gossip update` but not for
    # `query`, so ChainNotServed is not reachable from `hl-peers query`; it is
    # covered at the client level instead.
    code, _, err = call(["query", "--base-url", mock_server, "--asn", "AS24940"])
    assert code == cli.EXIT_CLIENT_ERROR and "FilterUnavailable" in err
    with pytest.raises(SystemExit) as excinfo:  # argparse's own usage exit
        call(["query", "--nonsense"])
    assert excinfo.value.code == cli.EXIT_USAGE


def test_in_process_mempool_auction_and_country_flags(mock_server: str) -> None:
    code, out, err = call(
        ["query", "--base-url", mock_server, "--serves-mempool", "true", "--cc", "DE"]
    )
    assert code == 0, err
    body = json.loads(out)
    assert [p["id"] for p in body["peers"]] == ["192.0.2.10"]
    assert body["peers"][0]["mempool"]["serves"] is True
    code, out, err = call(
        ["query", "--base-url", mock_server, "--auction", "holds_slot,bidding", "--output", "ips"]
    )
    assert code == 0 and out.split() == ["192.0.2.10", "198.51.100.7"]
    code, out, err = call(
        ["query", "--base-url", mock_server, "--serves-mempool", "null", "--output", "ips"]
    )
    assert code == 0 and out.split() == ["198.51.100.20", "192.0.2.200"]
    # closed enums are refused locally, exit 1
    code, _, err = call(["query", "--base-url", mock_server, "--serves-mempool", "yes"])
    assert code == cli.EXIT_CLIENT_ERROR and "serves_mempool" in err
    code, _, err = call(["query", "--base-url", mock_server, "--auction", "HOLDS_SLOT"])
    assert code == cli.EXIT_CLIENT_ERROR and "auction" in err
    # --country parses and reaches the wire (no fixture for it: the mock answers 404)
    code, _, err = call(["query", "--base-url", mock_server, "--country", "jp"])
    assert code == cli.EXIT_NOT_FOUND and "country=JP" in err


def test_in_process_format_gossip_config_prints_the_canonical_file(mock_server: str) -> None:
    code, out, err = call(
        ["query", "--base-url", mock_server, "--cc", "DE", "--format", "gossip-config"]
    )
    assert code == 0, err
    golden = json.loads(
        (
            Path(__file__).resolve().parents[2] / "fixtures/http/peers_format_gossip_config.json"
        ).read_text(encoding="utf-8")
    )["expect"]["gossip_config_json_text"]
    assert out == golden


def test_in_process_facets_take_filters(mock_server: str) -> None:
    code, _, err = call(["facets", "--base-url", mock_server, "--serves-mempool", "true"])
    # no fixture for a filtered facets call; reaching the wire (404) is the point
    assert code == cli.EXIT_NOT_FOUND and "serves_mempool=true" in err


def test_max_retries_defaults_to_zero_and_is_opt_in() -> None:
    args = cli.build_parser().parse_args(["query"])
    assert cli.make_client(args).max_retries == 0
    args = cli.build_parser().parse_args(["query", "--max-retries", "3"])
    assert cli.make_client(args).max_retries == 3

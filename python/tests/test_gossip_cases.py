"""ONE generic runner over every case in ``fixtures/gossip/cases``.

Not one test per case: one runner, driven by the format in
``fixtures/README.md``. All 76 cases are executed and all 76 must pass.

``expect.warnings`` is asserted by calling ``validate()`` after the last
operation and comparing the **set** of codes — §6.4's state-derived warnings
unioned with any pending merge warnings (§6.5), de-duplicated by code.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from conftest import GOSSIP_CASES
from hl_peers.errors import ERROR_KINDS, HlPeersError
from hl_peers.gossip import ConfigWarning, GossipConfig, SaveResult, render_config

CASE_FILES = sorted(GOSSIP_CASES.glob("*.json"))


def _substitute(value: Any, tmpdir: Path) -> Any:
    if isinstance(value, str):
        return value.replace("$TMPDIR", str(tmpdir))
    if isinstance(value, list):
        return [_substitute(v, tmpdir) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, tmpdir) for k, v in value.items()}
    return value


def _warnings_of(subject: GossipConfig) -> List[str]:
    """What `fixtures/README.md` asserts: validate() after the last operation."""
    return [w.code for w in subject.validate()]


def _value_of(result: Any, op: str) -> Any:
    if isinstance(result, SaveResult):
        return {k: v for k, v in dataclasses.asdict(result).items() if k != "warnings"}
    if op == "validate" and isinstance(result, list):
        return "ok"  # validate() returned its warnings without raising
    if isinstance(result, list) and all(isinstance(w, ConfigWarning) for w in result):
        return [w.code for w in result]  # merge_peers returns its own warnings
    return result


def _apply(subject: Optional[GossipConfig], operation: Dict[str, Any]) -> Any:
    name = operation["op"]
    args = operation.get("args", [])
    opts = operation.get("opts", {})
    if name == "new":
        return GossipConfig.new(*args, **opts)
    if name == "from_peers":
        return GossipConfig.from_peers(*args, **opts)
    assert subject is not None, "operation %r needs a subject" % name
    return getattr(subject, name)(*args, **opts)


def run_case(case: Dict[str, Any], tmpdir: Path) -> None:
    case = _substitute(case, tmpdir)
    expect = case["expect"]
    expected_error = expect.get("error")

    subject: Optional[GossipConfig] = None
    result: Any = None
    raised: Optional[HlPeersError] = None
    raised_at: Optional[int] = None

    try:
        if "input_json_text" in case:
            subject = GossipConfig.parse(case["input_json_text"])
        elif case.get("input_config") is not None:
            subject = GossipConfig.parse(render_config(case["input_config"]))
    except HlPeersError as exc:
        raised, raised_at = exc, -1

    if raised is None:
        for index, operation in enumerate(case["operations"]):
            try:
                result = _apply(subject, operation)
            except HlPeersError as exc:
                raised, raised_at = exc, index
                break
            if isinstance(result, GossipConfig):
                subject = result

    # -- error -------------------------------------------------------------
    if expected_error:
        assert raised is not None, "expected %s, nothing raised" % expected_error["kind"]
        expected_type = ERROR_KINDS[expected_error["kind"]]
        assert isinstance(raised, expected_type), "expected %s, got %r" % (
            expected_error["kind"],
            raised,
        )
        if "parameter" in expected_error:
            assert getattr(raised, "parameter", None) == expected_error["parameter"]
        if "at_op" in expected_error:
            assert raised_at == expected_error["at_op"]
        if "reason_contains" in expected_error:
            needle = expected_error["reason_contains"].lower()
            assert needle in str(raised).lower(), "%r not in %r" % (needle, str(raised))
    else:
        assert raised is None, "unexpected %r" % (raised,)

    # -- json_text ---------------------------------------------------------
    if "json_text" in expect:
        assert subject is not None
        assert subject.to_json() == expect["json_text"]

    # -- value -------------------------------------------------------------
    if "value" in expect:
        expected_value = expect["value"]
        actual = _value_of(result, case["operations"][-1]["op"])
        if isinstance(expected_value, dict):
            assert isinstance(actual, dict)
            for key, value in expected_value.items():
                assert actual.get(key) == value, "%s: %r != %r" % (key, actual.get(key), value)
        else:
            assert actual == expected_value

    # -- warnings ----------------------------------------------------------
    if "warnings" in expect:
        assert subject is not None
        codes = _warnings_of(subject)
        assert len(codes) == len(set(codes)), "a warning code appeared twice: %r" % (codes,)
        assert sorted(codes) == sorted(expect["warnings"]), "%r != %r" % (
            codes,
            expect["warnings"],
        )
    elif not expected_error:
        raise AssertionError("every non-error case must assert expect.warnings")

    # -- diff --------------------------------------------------------------
    for needle in expect.get("diff_contains", []):
        diff = getattr(result, "diff", "")
        assert needle in diff, "%r not in diff %r" % (needle, diff)

    # -- files -------------------------------------------------------------
    if "file_after" in expect:
        path = Path(expect["file_after"]["path"])
        assert path.exists(), "%s does not exist" % path
        assert path.read_text(encoding="utf-8") == expect["file_after"]["json_text"]
    if "file_absent" in expect:
        assert not Path(expect["file_absent"]).exists()


@pytest.mark.parametrize("case_file", CASE_FILES, ids=lambda p: p.stem)
def test_gossip_conformance(case_file: Path, tmp_path: Path) -> None:
    run_case(case_file_json(case_file), tmp_path)


def case_file_json(case_file: Path) -> Dict[str, Any]:
    return json.loads(case_file.read_text(encoding="utf-8"))


def test_every_case_file_is_covered() -> None:
    assert len(CASE_FILES) == 76, "expected 76 conformance cases, found %d" % len(CASE_FILES)


def test_serializer_matches_the_reference_implementation(repo_root: Path) -> None:
    """Compare against ``tools/gen_fixtures.py``'s reference serializer.

    Loaded by path on purpose: the package must stand alone and never import it.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_gen_fixtures", repo_root / "tools" / "gen_fixtures.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    samples = [
        {"chain": "Mainnet", "root_node_ips": [{"Ip": "192.0.2.10"}], "try_new_peers": False},
        {
            "chain": "Testnet",
            "root_node_ips": [{"Ip": "192.0.2.10"}, {"Ip": "198.51.100.7"}],
            "try_new_peers": True,
            "reserved_peer_ips": ["198.51.100.20"],
            "n_gossip_peers": 20,
            "split_client_blocks": True,
            "zzz_future": {"b": 2, "a": 1},
            "aaa_future": [3, 1, 2],
            "note": "Betreiber — Köln ✓",
        },
        {"root_node_ips": [], "reserved_peer_ips": []},
    ]
    for sample in samples:
        assert render_config(sample) == module.render(sample)

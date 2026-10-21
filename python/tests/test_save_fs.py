"""Filesystem behaviour of ``GossipConfig.save`` — the half of this library
whose failure mode is a crash-looping node."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hl_peers import GossipConfig, Invalid, NotFound, default_config_path

MINIMAL = {"chain": "Mainnet", "root_node_ips": [{"Ip": "192.0.2.10"}], "try_new_peers": False}


def config() -> GossipConfig:
    return GossipConfig.parse(
        '{"chain": "Mainnet", "root_node_ips": [{"Ip": "192.0.2.10"}], "try_new_peers": false}'
    )


def test_new_file_is_private_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    result = config().save(str(path))
    assert result.written and result.changed
    assert path.read_text(encoding="utf-8").endswith("}\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]  # no temp file left behind


def test_the_temp_file_is_in_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "override_gossip_config.json"
    seen = []
    real_replace = os.replace

    def spy(src, dst):  # type: ignore[no-untyped-def]
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    config().save(str(path))
    assert seen and os.path.dirname(seen[0][0]) == str(tmp_path)


def test_an_existing_mode_is_preserved_and_never_widened(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    path.write_text("{}\n", encoding="utf-8")
    os.chmod(path, 0o640)
    config().save(str(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_save_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    first = config().save(str(path))
    mtime = path.stat().st_mtime_ns
    second = config().save(str(path))
    assert first.written is True
    assert second.written is False and second.changed is False
    assert path.stat().st_mtime_ns == mtime  # mtime untouched


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    config().save(str(path))
    before = path.read_text(encoding="utf-8")
    changed = config().add_root_ip("192.0.2.99")
    result = changed.save(str(path), dry_run=True)
    assert result.written is False and result.changed is True
    assert '+      "Ip": "192.0.2.99"' in result.diff
    assert result.diff.startswith("--- %s" % path)
    assert path.read_text(encoding="utf-8") == before


def test_dry_run_against_a_missing_file_diffs_against_empty(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    result = config().save(str(path), dry_run=True)
    assert result.changed is True and not path.exists()
    assert "+{" in result.diff


def test_backup_is_written_with_an_injected_clock(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    config().save(str(path))
    frozen = datetime(2026, 9, 21, 2, 35, 0, tzinfo=timezone.utc)
    result = config().add_root_ip("192.0.2.99").save(str(path), backup=True, now=lambda: frozen)
    assert result.backup_path == str(path) + ".20260921T023500Z.bak"
    assert Path(result.backup_path).read_text(encoding="utf-8").count("192.0.2.99") == 0


def test_no_backup_on_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    config().save(str(path))
    result = config().save(str(path), backup=True)
    assert result.written is False and result.backup_path is None
    assert list(tmp_path.iterdir()) == [path]


def test_zero_roots_refusal_leaves_the_file_untouched(tmp_path: Path) -> None:
    path = tmp_path / "override_gossip_config.json"
    config().save(str(path))
    before = path.read_text(encoding="utf-8")
    empty = config().set_root_ips([])
    with pytest.raises(Invalid) as excinfo:
        empty.save(str(path))
    assert excinfo.value.parameter == "root_node_ips"
    assert "crash-loop" in excinfo.value.reason
    assert path.read_text(encoding="utf-8") == before


def test_load_refuses_to_invent_a_config_for_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(NotFound):
        GossipConfig.load(str(tmp_path / "nope.json"))
    fresh = GossipConfig.load_or_new(str(tmp_path / "nope.json"), "Mainnet")
    assert fresh.root_ips() == []  # the explicit opt-in


def test_default_config_path_is_home_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/operator")
    assert default_config_path() == "/home/operator/override_gossip_config.json"


def test_the_save_path_is_never_discovered_from_a_process() -> None:
    """A guard against a future 'helpful' auto-detect: no /proc, no systemd."""
    source = (Path(__file__).resolve().parents[1] / "src/hl_peers/gossip.py").read_text()
    for needle in ("/proc/", "systemctl", "psutil", "import subprocess"):
        assert needle not in source


def test_nothing_in_the_library_restarts_a_node() -> None:
    src = Path(__file__).resolve().parents[1] / "src/hl_peers"
    for path in src.glob("*.py"):
        text = path.read_text()
        for needle in ("systemctl", "kill(", "SIGHUP", "restart("):
            assert needle not in text, "%s mentions %s" % (path.name, needle)


def test_file_mode_sets_the_permission_bits(tmp_path: Path) -> None:
    """🪤 `file_mode` is permissions; `mode` is the merge strategy (§6.6)."""
    path = tmp_path / "override_gossip_config.json"
    config().save(str(path), file_mode=0o640)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_merge_warnings_are_pending_and_survive_save(tmp_path: Path) -> None:
    from hl_peers import GossipConfig

    path = str(tmp_path / "override_gossip_config.json")
    cfg = GossipConfig.new("Mainnet", ["192.0.2.10"]).set_try_new_peers(True)
    returned = cfg.merge_peers(["203.0.113.%d" % i for i in range(1, 13)], mode="replace")
    assert [w.code for w in returned] == ["truncated"]  # returned from the call
    assert [w.code for w in cfg.pending_warnings] == ["truncated"]  # and retained
    result = cfg.save(path)
    assert result.warning_codes == ["truncated"]
    assert [w.code for w in cfg.validate()] == ["truncated"]  # a save does not clear
    assert [w.code for w in cfg.clear_warnings().validate()] == []


# ---------------------------------------------------------------------------
# 🔴 symlinks — CLIENT_SPEC §6.6
# ---------------------------------------------------------------------------
def test_save_follows_a_symlink_and_replaces_its_target(tmp_path: Path) -> None:
    """Renaming over the link would swap the operator's arrangement for a plain
    file, after which the node and their tooling read two different files."""
    real = tmp_path / "managed" / "gossip.json"
    real.parent.mkdir()
    real.write_text('{"chain": "Mainnet", "root_node_ips": []}\n', encoding="utf-8")
    link = tmp_path / "override_gossip_config.json"
    link.symlink_to(real)

    result = config().save(str(link))

    assert link.is_symlink(), "the link itself must survive the write"
    assert os.path.realpath(str(link)) == str(real)
    assert real.read_text(encoding="utf-8") == config().to_json()
    assert result.path == str(link)
    assert result.resolved_path == str(real)


def test_save_preserves_the_targets_mode_through_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "gossip.json"
    real.write_text("{}\n", encoding="utf-8")
    os.chmod(real, 0o640)
    link = tmp_path / "override_gossip_config.json"
    link.symlink_to(real)

    config().save(str(link))

    assert stat.S_IMODE(real.stat().st_mode) == 0o640


def test_save_writes_through_a_dangling_symlink(tmp_path: Path) -> None:
    real = tmp_path / "not-yet-there.json"
    link = tmp_path / "override_gossip_config.json"
    link.symlink_to(real)

    config().save(str(link))

    assert real.read_text(encoding="utf-8") == config().to_json()
    assert link.is_symlink()


def test_save_refuses_a_symlink_loop(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.symlink_to(b)
    b.symlink_to(a)

    with pytest.raises(Invalid) as excinfo:
        config().save(str(a))
    assert excinfo.value.parameter == "path"


def test_save_refuses_a_symlink_into_a_missing_directory(tmp_path: Path) -> None:
    link = tmp_path / "override_gossip_config.json"
    link.symlink_to(tmp_path / "nowhere" / "gossip.json")

    with pytest.raises(Invalid) as excinfo:
        config().save(str(link))
    assert excinfo.value.parameter == "path"

defmodule HlPeers.GossipConfigFSTest do
  @moduledoc """
  Filesystem behaviour of `save/3` (`CLIENT_SPEC.md` §6.6) — the parts the
  language-neutral conformance cases cannot see: atomicity, mode preservation,
  the timestamped `.bak`, and the fact that a refusal leaves the operator's file
  exactly as it was.
  """

  use ExUnit.Case, async: true

  alias HlPeers.GossipConfig

  setup do
    dir = Path.join(System.tmp_dir!(), "hl_peers_fs_#{:erlang.unique_integer([:positive])}")
    File.mkdir_p!(dir)
    on_exit(fn -> File.rm_rf!(dir) end)
    %{dir: dir, path: Path.join(dir, "override_gossip_config.json")}
  end

  defp config, do: GossipConfig.new("Mainnet", ["192.0.2.10"])

  test "the temp file lives in the target's directory, never /tmp", %{dir: dir, path: path} do
    # A rename across filesystems is not atomic, so the temp file must be a
    # sibling of the target. We catch it mid-flight by watching the directory.
    parent = self()

    task =
      Task.async(fn ->
        send(parent, :watching)

        Enum.find_value(1..2000, fn _ ->
          case Path.wildcard(Path.join(dir, ".*.tmp")) do
            [] -> nil
            [found] -> found
          end
        end)
      end)

    assert_receive :watching
    {:ok, _} = GossipConfig.save(config(), path)
    Task.shutdown(task, :brutal_kill)

    assert File.read!(path) == GossipConfig.to_json(config())
    assert Path.wildcard(Path.join(dir, ".*.tmp")) == [], "no temp file is left behind"
  end

  test "a new file is 0600 — this names infrastructure you peer with", %{path: path} do
    {:ok, result} = GossipConfig.save(config(), path)

    assert result.written
    assert mode(path) == 0o600
  end

  test "an existing file's mode is preserved and never widened", %{path: path} do
    File.write!(path, "{}\n")
    File.chmod!(path, 0o640)

    {:ok, _} = GossipConfig.save(config(), path)

    assert mode(path) == 0o640
  end

  test "an explicit file_mode applies to a new file only", %{path: path} do
    {:ok, _} = GossipConfig.save(config(), path, file_mode: 0o644)

    assert mode(path) == 0o644
  end

  test "backup: true copies the target to a timestamped .bak, preserving mode", %{
    dir: dir,
    path: path
  } do
    File.write!(path, "{}\n")
    File.chmod!(path, 0o640)

    {:ok, result} =
      GossipConfig.save(config(), path, backup: true, now: ~U[2026-09-21 02:35:00Z])

    assert result.backup_path == path <> ".20260921T023500Z.bak"
    assert File.read!(result.backup_path) == "{}\n"
    assert mode(result.backup_path) == 0o640
    assert length(Path.wildcard(Path.join(dir, "*.bak"))) == 1
  end

  test "a no-op save writes nothing, creates no .bak and does not touch mtime", %{
    dir: dir,
    path: path
  } do
    {:ok, first} = GossipConfig.save(config(), path)
    assert first.written

    mtime = File.stat!(path, time: :posix).mtime

    {:ok, second} = GossipConfig.save(config(), path, backup: true)

    refute second.written
    refute second.changed
    assert File.stat!(path, time: :posix).mtime == mtime
    assert Path.wildcard(Path.join(dir, "*.bak")) == []
  end

  test "dry_run writes nothing and returns a unified diff", %{dir: dir, path: path} do
    {:ok, _} = GossipConfig.save(config(), path)
    before = File.read!(path)

    {:ok, result} =
      config() |> GossipConfig.add_root_ip("192.0.2.99") |> GossipConfig.save(path, dry_run: true)

    refute result.written
    assert result.changed
    assert result.diff =~ "--- #{path}"
    assert result.diff =~ "+++ #{path}"
    assert result.diff =~ "@@"
    assert result.diff =~ "+      \"Ip\": \"192.0.2.99\""
    assert File.read!(path) == before
    assert Path.wildcard(Path.join(dir, ".*.tmp")) == []
  end

  test "dry_run against a missing file diffs against empty", %{path: path} do
    {:ok, result} = GossipConfig.save(config(), path, dry_run: true)

    assert result.changed
    assert result.diff =~ "+  \"chain\": \"Mainnet\""
    refute File.exists?(path)
  end

  test "🔴 the zero-roots refusal leaves the file untouched", %{dir: dir, path: path} do
    {:ok, _} = GossipConfig.save(config(), path)
    before = File.read!(path)

    {:error, error} =
      config() |> GossipConfig.set_root_ips([]) |> GossipConfig.save(path)

    assert error.kind == :invalid
    assert error.parameter == "root_node_ips"
    assert error.reason =~ "crash-loop"
    assert File.read!(path) == before
    assert Path.wildcard(Path.join(dir, ".*.tmp")) == []
  end

  test "a refusal never creates the file in the first place", %{path: path} do
    {:error, _} = GossipConfig.new("Mainnet", []) |> GossipConfig.save(path)

    refute File.exists?(path)
  end

  test "load refuses to invent an empty config for a missing file", %{path: path} do
    assert {:error, %{kind: :not_found}} = GossipConfig.load(path)
    assert {:ok, config} = GossipConfig.load_or_new(path, "Mainnet")
    assert GossipConfig.root_ips(config) == []
    # …and that opt-in config still cannot be saved, because it is the crash-loop shape.
    assert {:error, %{parameter: "root_node_ips"}} = GossipConfig.save(config, path)
  end

  test "the round trip of the live minimal form is byte-identical", %{path: path} do
    live =
      ~s({\n  "chain": "Mainnet",\n  "root_node_ips": [\n    {\n      "Ip": "192.0.2.10"\n    }\n  ],\n  "try_new_peers": false\n}\n)

    File.write!(path, live)

    {:ok, loaded} = GossipConfig.load(path)
    {:ok, result} = GossipConfig.save(loaded, path)

    refute result.written, "identical bytes must not be rewritten"
    assert File.read!(path) == live
  end

  test "default_config_path is $HOME-relative and never discovered" do
    assert GossipConfig.default_config_path() ==
             Path.join(System.get_env("HOME"), "override_gossip_config.json")
  end

  defp mode(path), do: Bitwise.band(File.stat!(path).mode, 0o777)

  # ------------------------------------------------------------------
  # 🔴 symlinks — CLIENT_SPEC §6.6
  # ------------------------------------------------------------------

  test "🔴 save/3 follows a symlink and replaces its target, not the link", %{
    dir: dir,
    path: path
  } do
    # Renaming over the link would swap the operator's arrangement for a plain
    # file, after which the node and their tooling read two different files.
    managed = Path.join(dir, "managed")
    File.mkdir_p!(managed)
    real = Path.join(managed, "gossip.json")
    File.write!(real, ~s({"chain": "Mainnet", "root_node_ips": []}\n))
    File.ln_s!(real, path)

    config = config()
    assert {:ok, result} = GossipConfig.save(config, path)

    assert {:ok, _} = File.read_link(path), "the link itself must survive the write"
    assert File.read!(real) == GossipConfig.to_json(config)
    assert result.path == path
    assert result.resolved_path == real
  end

  test "save/3 preserves the target's mode through a symlink", %{dir: dir, path: path} do
    real = Path.join(dir, "gossip.json")
    File.write!(real, "{}\n")
    File.chmod!(real, 0o640)
    File.ln_s!(real, path)

    assert {:ok, _} = GossipConfig.save(config(), path)
    assert {:ok, %File.Stat{mode: mode}} = File.stat(real)
    assert Bitwise.band(mode, 0o777) == 0o640
  end

  test "save/3 writes through a dangling symlink and leaves the link alone", %{
    dir: dir,
    path: path
  } do
    real = Path.join(dir, "not-yet-there.json")
    File.ln_s!(real, path)

    config = config()
    assert {:ok, _} = GossipConfig.save(config, path)
    assert File.read!(real) == GossipConfig.to_json(config)
    assert {:ok, _} = File.read_link(path)
  end

  test "save/3 refuses a symlink loop", %{dir: dir} do
    a = Path.join(dir, "a.json")
    b = Path.join(dir, "b.json")
    File.ln_s!(b, a)
    File.ln_s!(a, b)

    assert {:error, %HlPeers.Error{kind: :invalid, parameter: "path"}} =
             GossipConfig.save(config(), a)
  end

  test "save/3 refuses a symlink into a directory that does not exist", %{dir: dir, path: path} do
    File.ln_s!(Path.join([dir, "nowhere", "gossip.json"]), path)

    assert {:error, %HlPeers.Error{kind: :invalid, parameter: "path"}} =
             GossipConfig.save(config(), path)
  end
end

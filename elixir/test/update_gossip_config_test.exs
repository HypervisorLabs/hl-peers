defmodule HlPeers.UpdateGossipConfigTest do
  @moduledoc "The one helper that joins the two namespaces (`CLIENT_SPEC.md` §7)."

  use ExUnit.Case, async: true

  alias HlPeers.{Client, GossipConfig, Query}
  alias HlPeers.Test.FixtureAdapter

  setup do
    {:ok, recorder} = FixtureAdapter.start_link()

    client =
      Client.new(
        base_url: "http://fixtures.invalid",
        adapter: FixtureAdapter,
        adapter_opts: [recorder: recorder],
        sleep_fun: fn _ -> :ok end
      )

    dir = Path.join(System.tmp_dir!(), "hl_peers_upd_#{:erlang.unique_integer([:positive])}")
    File.mkdir_p!(dir)
    on_exit(fn -> File.rm_rf!(dir) end)

    %{client: client, recorder: recorder, path: Path.join(dir, "override_gossip_config.json")}
  end

  defp paged, do: Query.new(limit: 2, sort: "tier")

  test "builds a config from a query without writing anything when no path is given", ctx do
    {:ok, result} =
      HlPeers.update_gossip_config(ctx.client, Query.new(cc: ["DE"]))

    assert result.json =~ "\"chain\": \"Mainnet\""
    assert result.written_path == nil
    assert GossipConfig.root_ips(result.config) == ["192.0.2.10", "192.0.2.11"]
    # the chain came from the response envelope, not from a guess
    assert GossipConfig.chain(result.config) == "Mainnet"
  end

  test "writes the file when a path is given, and is idempotent", ctx do
    {:ok, first} = HlPeers.update_gossip_config(ctx.client, paged(), ctx.path)

    assert first.written_path == ctx.path
    assert first.changed
    assert File.read!(ctx.path) == first.json

    {:ok, second} = HlPeers.update_gossip_config(ctx.client, paged(), ctx.path)

    refute second.changed
    assert second.written_path == nil
  end

  test "merges into an existing file, pinning keep_existing first", ctx do
    File.write!(ctx.path, GossipConfig.to_json(GossipConfig.new("Mainnet", ["203.0.113.250"])))

    {:ok, result} =
      HlPeers.update_gossip_config(ctx.client, paged(), ctx.path,
        mode: :replace,
        keep_existing: ["203.0.113.250"]
      )

    assert List.first(GossipConfig.root_ips(result.config)) == "203.0.113.250"
  end

  test "dry_run shows the diff and writes nothing", ctx do
    {:ok, _} = HlPeers.update_gossip_config(ctx.client, paged(), ctx.path)
    before = File.read!(ctx.path)

    {:ok, result} =
      HlPeers.update_gossip_config(ctx.client, Query.new(cc: ["DE"]), ctx.path, dry_run: true)

    assert result.diff =~ "@@"
    assert File.read!(ctx.path) == before
  end

  test "🔴 available: false raises Unavailable before touching any file", ctx do
    {:error, error} =
      HlPeers.update_gossip_config(ctx.client, %Query{}, ctx.path,
        headers: [{"X-Mock-Scenario", "unavailable"}],
        chain: "Mainnet"
      )

    assert error.kind == :unavailable
    assert error.detail =~ "crash-loop"
    refute File.exists?(ctx.path)
  end

  test "a peer list may be passed instead of a query, skipping the network", ctx do
    {:ok, result} =
      HlPeers.update_gossip_config(ctx.client, ["192.0.2.10", "192.0.2.11"], nil,
        chain: "Testnet"
      )

    assert GossipConfig.chain(result.config) == "Testnet"
    assert FixtureAdapter.request_count(ctx.recorder) == 0
  end

  test "🔴 the chain is never defaulted to Mainnet", ctx do
    {:error, error} =
      HlPeers.update_gossip_config(ctx.client, ["192.0.2.10"], nil, [])

    assert error.kind == :invalid
    assert error.parameter == "chain"
  end

  test "warnings ride on the result and are not swallowed", ctx do
    {:ok, result} =
      HlPeers.update_gossip_config(ctx.client, ["192.0.2.10", "192.0.2.11"], nil,
        chain: "Mainnet",
        try_new_peers: false,
        max_roots: 1
      )

    codes = HlPeers.Warning.codes(result.warnings)
    assert :truncated in codes
  end

  test "no_write renders but leaves the file alone", ctx do
    {:ok, result} =
      HlPeers.update_gossip_config(ctx.client, paged(), ctx.path, no_write: true)

    assert result.json != ""
    refute File.exists?(ctx.path)
  end
end

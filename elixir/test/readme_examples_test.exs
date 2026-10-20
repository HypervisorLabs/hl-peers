defmodule HlPeers.ReadmeExamplesTest do
  @moduledoc """
  Every code sample in `elixir/README.md` and `docs/elixir.md` that is not
  already a doctest, executed here against the offline fixtures. A documented
  example that drifts from the code fails the build rather than misleading an
  operator.
  """

  use ExUnit.Case, async: true

  alias HlPeers.{Client, Error, GossipConfig, Peer, Poller, Query}
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

    dir =
      Path.join(System.tmp_dir!(), "hl_peers_readme_#{:erlang.unique_integer([:positive])}")

    File.mkdir_p!(dir)
    on_exit(fn -> File.rm_rf!(dir) end)

    %{client: client, recorder: recorder, path: Path.join(dir, "override_gossip_config.json")}
  end

  test "quickstart", %{client: client} do
    {:ok, page} = HlPeers.list_peers(client, Query.tier1())

    assert Enum.map(page.peers, & &1.id) == ["198.51.100.20"]
  end

  test "the owner's four example queries", %{client: client} do
    assert {:ok, %{count: 1}} =
             HlPeers.list_peers(client, Query.mempool_servers() |> Query.cc(["DE"]))

    assert {:ok, _} = HlPeers.list_peers(client, Query.new(cc: ["DE"]))
    assert {:ok, _} = HlPeers.not_full(client, cc: ["SG"])
    assert {:ok, _} = HlPeers.tier1(client)
    assert {:ok, _} = HlPeers.sentries(client)
  end

  test "a composed query serializes as documented" do
    query =
      Query.new()
      |> Query.cc(["DE", "NL"])
      |> Query.state(["full"])
      |> Query.max_blocks_behind(5)
      |> Query.seen_within("1h")
      |> Query.limit(50)

    assert Query.to_query_string(query) ==
             "cc=DE%2CNL&limit=50&max_blocks_behind=5&seen_within=1h&state=full"
  end

  test "streaming with a filter", %{client: client} do
    serving =
      client
      |> HlPeers.stream_peers(Query.new(limit: 2, sort: "tier"))
      |> Stream.filter(&Peer.connectable?/1)
      |> Enum.take(20)

    assert is_list(serving)
  end

  test "build a config from a query and print it", %{client: client} do
    {:ok, result} = HlPeers.update_gossip_config(client, Query.new(cc: ["DE"]))

    assert result.json =~ "root_node_ips"
    assert result.written_path == nil
  end

  test "update a file in place, with keep_existing and exclude", %{client: client, path: path} do
    {:ok, result} =
      HlPeers.update_gossip_config(client, Query.new(cc: ["DE"]), path,
        max_roots: 8,
        mode: :replace,
        backup: true,
        keep_existing: ["192.0.2.10"],
        exclude: ["198.51.100.9"]
      )

    assert result.changed
    assert Enum.all?(result.warnings, &is_binary(&1.message))
    assert List.first(GossipConfig.root_ips(result.config)) == "192.0.2.10"
  end

  test "dry run prints a diff and writes nothing", %{client: client, path: path} do
    {:ok, _} = HlPeers.update_gossip_config(client, Query.new(cc: ["DE"]), path)
    before = File.read!(path)

    {:ok, result} =
      HlPeers.update_gossip_config(client, ["203.0.113.7"], path,
        chain: "Mainnet",
        mode: :append,
        dry_run: true
      )

    assert is_binary(result.diff)
    assert File.read!(path) == before
  end

  test "offline editing chain", %{path: path} do
    File.write!(path, GossipConfig.to_json(GossipConfig.new("Mainnet", ["192.0.2.10"])))

    {:ok, config} = GossipConfig.load(path)

    {:ok, result} =
      config
      |> GossipConfig.add_root_ip("192.0.2.11")
      |> GossipConfig.remove_root_ip("198.51.100.9")
      |> GossipConfig.set_try_new_peers(true)
      |> GossipConfig.save(path, backup: true)

    assert result.written
    assert GossipConfig.load!(path) |> GossipConfig.root_ips() == ["192.0.2.10", "192.0.2.11"]
  end

  test "🔴 never zero roots", %{path: path} do
    assert {:error, %Error{kind: :invalid, parameter: "root_node_ips"}} =
             GossipConfig.new("Mainnet", []) |> GossipConfig.save(path)
  end

  test "the rate-limit accessors", %{client: client} do
    {:ok, page} = HlPeers.list_peers(client)

    assert page.rate_limit.remaining == 17
    assert page.rate_limit.reset == 42
  end

  test "the error-handling example", %{client: client} do
    result =
      case HlPeers.list_peers(client, Query.new(region: ["aws:eu-central-1"])) do
        {:ok, page} ->
          page.peers

        {:error, %Error{kind: :filter_unavailable} = e} ->
          {:error, e.filter}

        {:error, %Error{kind: :rate_limited} = e} ->
          {:retry_at, e.reset_at}

        {:error, %Error{} = e} ->
          if Error.bad_request?(e), do: {:bad_query, e}, else: {:error, e}
      end

    assert result == {:error, "region"}
  end

  test "the poller example", %{client: client, path: path} do
    {:ok, pid} =
      Poller.start_link(
        client: client,
        query: Query.new(cc: ["DE"]),
        interval: 300_000,
        gossip_path: path
      )

    assert is_pid(pid)
  end

  test "mempool and auction filters", %{client: client} do
    {:ok, unknown} = HlPeers.list_peers(client, Query.new(serves_mempool: [nil]))
    assert Enum.all?(unknown.peers, &is_nil(&1.mempool.serves))

    {:ok, page} = HlPeers.list_peers(client, Query.new(auction: ["holds_slot", "bidding"]))

    rows =
      for peer <- page.peers do
        {peer.id, peer.mempool.serves, peer.mempool.source, peer.auction.status,
         peer.auction.slot}
      end

    assert rows == [
             {"192.0.2.10", true, "probe", "holds_slot", 3},
             {"198.51.100.7", true, "relay", "bidding", nil}
           ]
  end

  test "facets example", %{client: client} do
    {:ok, facets} = HlPeers.facets(client)
    assert HlPeers.Facets.available?(facets, "serves_mempool")

    assert {:ok, [%{value: "holds_slot", count: 9} | _]} =
             HlPeers.Facets.values(facets, "auction")
  end

  test "the network map", %{client: client} do
    {:ok, graph} = HlPeers.network_map_graph(client, max_tier: 2)
    assert [%Peer{} | _] = graph.nodes
    assert length(Enum.map(graph.edges, &{&1.from, &1.to, &1.span})) == 3
    assert hd(graph.tiers) == %{tier: 0, count: 1}

    {:ok, summary} = HlPeers.network_map_summary(client)
    assert summary.connectable == %{"yes" => 3, "no" => 1, "unknown" => 0}
  end

  test "let the server render the gossip config, then save", %{client: client, path: path} do
    {:ok, result} = HlPeers.list_peers_as_gossip_config(client, Query.new(cc: ["DE"]))

    json = GossipConfig.to_json(result.config)
    {:ok, _} = GossipConfig.save(result.config, path, backup: true)
    assert File.read!(path) == json
  end

  test "the rate-limited example matches on reset_at", %{client: client} do
    result =
      case HlPeers.list_peers(client, %Query{}, headers: [{"X-Mock-Scenario", "rate-limited"}]) do
        {:error, %Error{kind: :rate_limited, reset_at: %DateTime{} = at}} ->
          {:would_sleep_until, at}

        other ->
          other
      end

    assert {:would_sleep_until, %DateTime{}} = result
  end

  test "defaults: the real host and no retries" do
    client = Client.new()
    # HL_PEER_API_BASE_URL wins when set; the default run needs neither.
    expected = System.get_env("HL_PEER_API_BASE_URL") || "https://api.hypervisor.gg"
    assert client.base_url == String.trim_trailing(expected, "/")
    assert client.max_retries == 0
  end
end

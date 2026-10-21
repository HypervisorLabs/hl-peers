defmodule HlPeers.V1AdditionsTest do
  @moduledoc """
  Replayed offline from
  `fixtures/http`: the typed `mempool` / `auction` objects, the filter 400s, both
  `/network-map/*` endpoints, `API-Version`, and the §4.7 golden gossip-config
  test. Plus the model-coverage test against `spec/peer-api-v1.fields.json`.
  """

  use ExUnit.Case, async: true

  alias HlPeers.{
    Client,
    Envelope,
    Error,
    GossipConfig,
    GossipConfigResult,
    NetworkMap,
    Peer,
    Query
  }

  alias HlPeers.Peer.{Auction, Mempool}
  alias HlPeers.Test.{FixtureAdapter, Fixtures}

  setup do
    {:ok, recorder} = FixtureAdapter.start_link()

    client =
      Client.new(
        base_url: "http://fixtures.invalid",
        adapter: FixtureAdapter,
        adapter_opts: [recorder: recorder],
        sleep_fun: fn _ms -> :ok end
      )

    %{client: client, recorder: recorder}
  end

  defp url(recorder), do: recorder |> FixtureAdapter.requests() |> List.last() |> Map.get(:url)

  defp signals(peers) do
    Enum.map(peers, fn p ->
      [p.id, p.mempool.serves, p.mempool.source, p.auction.status, p.auction.slot]
    end)
  end

  describe "mempool / auction fixtures" do
    test "peers_owner_q1_mempool_cc_de — owner query 1 whole", ctx do
      {:ok, page} = HlPeers.list_peers(ctx.client, Query.mempool_servers() |> Query.cc(["DE"]))

      assert url(ctx.recorder) == "http://fixtures.invalid/peers?cc=DE&serves_mempool=true"
      assert signals(page.peers) == [["192.0.2.10", true, "probe", "holds_slot", 3]]

      [peer] = page.peers

      assert %Mempool{
               serves: true,
               verdict: "serves",
               first_frame_ms: 412,
               last_checked: ~U[2026-09-21 02:30:00Z]
             } = peer.mempool

      assert %Auction{
               since: ~U[2026-09-20 11:00:00Z],
               last_bid_at: ~U[2026-09-20 10:59:12Z]
             } = peer.auction

      assert Peer.serves_mempool?(peer)
    end

    test "mempool_servers/2 is the same request", ctx do
      # Only the URL matters: this exact query has no un-gated fixture.
      _ = HlPeers.mempool_servers(ctx.client)
      assert url(ctx.recorder) == "http://fixtures.invalid/peers?serves_mempool=true"
    end

    test "peers_serves_mempool_unknown — nil is 'unknown', not false", ctx do
      {:ok, page} = HlPeers.list_peers(ctx.client, Query.new(serves_mempool: [nil]))

      assert url(ctx.recorder) == "http://fixtures.invalid/peers?serves_mempool=null"
      assert length(page.peers) == 2

      for peer <- page.peers do
        assert peer.mempool == %Mempool{}
        assert peer.auction == %Auction{}
        refute Peer.serves_mempool?(peer)
        assert peer.mempool.serves == nil
      end
    end

    test "peers_auction_holds_slot_or_bidding", ctx do
      {:ok, page} =
        HlPeers.list_peers(ctx.client, Query.new(auction: ["holds_slot", "bidding"]))

      assert url(ctx.recorder) ==
               "http://fixtures.invalid/peers?auction=holds_slot%2Cbidding"

      assert signals(page.peers) == [
               ["192.0.2.10", true, "probe", "holds_slot", 3],
               ["198.51.100.7", true, "relay", "bidding", nil]
             ]
    end

    test "a peer from an older server without the keys decodes to all-nil structs" do
      peer = Peer.from_map(%{"id" => "192.0.2.1"})

      assert peer.mempool == %Mempool{}
      assert peer.auction == %Auction{}
      assert peer.extra == %{}
    end

    test "unknown keys inside mempool/auction are ignored, not carried" do
      peer =
        Peer.from_map(%{
          "id" => "192.0.2.1",
          "mempool" => %{"serves" => false, "future" => 1},
          "auction" => %{"status" => "a_new_status"}
        })

      assert peer.mempool.serves == false
      assert peer.auction.status == "a_new_status"
      assert peer.extra == %{}
    end

    test "error_400_filter_removed — the old mempool= filter is a plain BadRequest", ctx do
      # The builder has no `mempool` field (it left the contract), so the server's
      # answer is reached through the transport directly.
      {:error, error} = Client.get(ctx.client, "/peers", [{"mempool", "serving"}])

      assert error.kind == :bad_request
      assert error.parameter == "mempool"
      assert error.detail =~ "serves_mempool"
      assert Error.bad_request?(error)
      assert error.api_version == "1"
    end

    test "error_400_filter_unavailable_mempool — never swallowed into an empty list", ctx do
      {:error, error} =
        HlPeers.list_peers(ctx.client, Query.new(serves_mempool: "true"),
          headers: [{"X-Mock-Scenario", "mempool-unavailable"}]
        )

      assert error.kind == :filter_unavailable
      assert error.filter == "serves_mempool"
      assert Error.bad_request?(error)
    end
  end

  describe "network map" do
    test "network_map_graph decodes nodes with the Peer decoder", ctx do
      {:ok, %NetworkMap.Graph{} = graph} = HlPeers.network_map_graph(ctx.client, max_tier: 2)

      assert url(ctx.recorder) == "http://fixtures.invalid/network-map/graph?max_tier=2"
      assert {graph.version, graph.available, graph.stale} == {"v1", true, false}
      assert graph.generated_at == "2026-09-21T02:35:00Z"
      assert graph.operator_nodes_count == nil
      assert {graph.node_count, graph.edge_count, graph.tier_count} == {4, 3, 3}
      assert graph.unreached == 0
      assert graph.truncated == false
      assert graph.tiers == [%{tier: 0, count: 1}, %{tier: 1, count: 2}, %{tier: 2, count: 1}]

      assert Enum.map(graph.nodes, & &1.id) ==
               ["192.0.2.200", "192.0.2.10", "198.51.100.7", "192.0.2.11"]

      assert Enum.all?(graph.nodes, &match?(%Peer{mempool: %Mempool{}}, &1))
      assert Enum.at(graph.nodes, 1).auction.status == "holds_slot"
      assert length(graph.edges) == 3
      assert Enum.all?(graph.edges, &match?(%NetworkMap.Edge{}, &1))
      assert graph.api_version == "1"
      assert graph.rate_limit.limit == 20
      assert graph.etag == "\"sha256-6cd7c782082eee66\""
      refute graph.from_cache
    end

    test "network_map_summary carries connectable buckets, no nodes/edges", ctx do
      {:ok, %NetworkMap.Summary{} = summary} = HlPeers.network_map_summary(ctx.client)

      assert url(ctx.recorder) == "http://fixtures.invalid/network-map/summary"
      assert summary.connectable == %{"yes" => 3, "no" => 1, "unknown" => 0}
      assert summary.node_count == 4
      refute Map.has_key?(summary, :nodes)
      refute Map.has_key?(summary, :edges)
      refute Map.has_key?(summary, :truncated)
      assert summary.api_version == "1"
    end

    test "the ETag cache applies (a repeat is a free 304)", ctx do
      {:ok, _} = HlPeers.network_map_summary(ctx.client)
      {:ok, again} = HlPeers.network_map_summary(ctx.client)

      assert again.status == 304
      assert again.from_cache
      assert again.connectable["yes"] == 3
    end

    test "filters are validated and serialized canonically", ctx do
      assert {:ok,
              [
                {"cc", "DE"},
                {"connectable", "yes,unknown"},
                {"hide_unreachable", "true"},
                {"label", "sentry"},
                {"operator", "Example Operator"},
                {"show_unreached", "false"}
              ]} =
               NetworkMap.to_params(
                 show_unreached: false,
                 hide_unreachable: true,
                 connectable: ["yes", "unknown"],
                 cc: "DE",
                 operator: "Example Operator",
                 label: ["sentry"]
               )

      for {bad, param} <- [
            {[connectable: ["maybe"]], "connectable"},
            {[max_tier: -1], "max_tier"},
            {[show_unreached: "yes"], "show_unreached"},
            {[tier: [1]], "tier"}
          ] do
        assert {:error, %Error{kind: :invalid, parameter: ^param}} =
                 HlPeers.network_map_graph(ctx.client, bad)
      end

      assert FixtureAdapter.request_count(ctx.recorder) == 0
    end

    test "available: false is data, not an error" do
      body = %{"version" => "v1", "available" => false, "stale" => false, "nodes" => []}

      {:ok, recorder} =
        FixtureAdapter.start_link(
          fixtures: [
            %{
              "request" => %{"method" => "GET", "path" => "/network-map/graph", "query" => %{}},
              "response" => %{"status" => 200, "headers" => %{}, "body" => body}
            }
          ]
        )

      client =
        Client.new(
          base_url: "http://fixtures.invalid",
          adapter: FixtureAdapter,
          adapter_opts: [recorder: recorder]
        )

      assert {:ok, %NetworkMap.Graph{available: false, nodes: []}} =
               HlPeers.network_map_graph(client)
    end
  end

  describe "API-Version" do
    test "is exposed on every result type", ctx do
      {:ok, page} = HlPeers.list_peers(ctx.client)
      {:ok, peer} = HlPeers.get_peer(ctx.client, "192.0.2.10")
      {:ok, facets} = HlPeers.facets(ctx.client)
      {:ok, config} = HlPeers.list_peers_as_gossip_config(ctx.client, Query.new(cc: ["DE"]))

      assert Enum.map([page, peer, facets, config], & &1.api_version) == ["1", "1", "1", "1"]
    end

    test "is exposed on every server error", ctx do
      errors = [
        HlPeers.list_peers(ctx.client, Query.new(asn: ["AS24940"])),
        HlPeers.get_peer(ctx.client, "198.51.100.254"),
        HlPeers.list_peers(ctx.client, %Query{}, headers: [{"X-Mock-Scenario", "rate-limited"}]),
        HlPeers.list_peers(ctx.client, %Query{}, headers: [{"X-Mock-Scenario", "server-error"}]),
        HlPeers.list_peers(ctx.client, %Query{},
          headers: [{"X-Mock-Scenario", "producer-unavailable"}]
        )
      ]

      assert Enum.map(errors, fn {:error, e} -> e.api_version end) == List.duplicate("1", 5)
    end

    test "a different value is informational, never a failure" do
      {:ok, recorder} =
        FixtureAdapter.start_link(
          fixtures: [
            %{
              "request" => %{"method" => "GET", "path" => "/peers", "query" => %{}},
              "response" => %{
                "status" => 200,
                "headers" => %{"API-Version" => "7"},
                "body" => %{"version" => "v1", "available" => true, "peers" => []}
              }
            }
          ]
        )

      client =
        Client.new(
          base_url: "http://fixtures.invalid",
          adapter: FixtureAdapter,
          adapter_opts: [recorder: recorder]
        )

      assert {:ok, %{api_version: "7"}} = HlPeers.list_peers(client)
    end

    test "a locally raised error has none", ctx do
      assert {:error, %Error{kind: :invalid, api_version: nil}} =
               HlPeers.get_peer(ctx.client, "not-an-ip")
    end
  end

  describe "🔴 §4.7 list_peers_as_gossip_config — the golden test" do
    setup do
      dir =
        Path.join(System.tmp_dir!(), "hl_peers_golden_#{:erlang.unique_integer([:positive])}")

      File.mkdir_p!(dir)
      on_exit(fn -> File.rm_rf!(dir) end)
      %{path: Path.join(dir, "override_gossip_config.json")}
    end

    test "to_json/1 and the saved bytes equal expect.gossip_config_json_text", ctx do
      golden = Fixtures.http("peers_format_gossip_config")["expect"]["gossip_config_json_text"]

      {:ok, %GossipConfigResult{} = result} =
        HlPeers.list_peers_as_gossip_config(ctx.client, Query.new(cc: ["DE"]))

      assert url(ctx.recorder) == "http://fixtures.invalid/peers?cc=DE&format=gossip_config"
      assert GossipConfig.to_json(result.config) == golden

      assert {:ok, saved} = GossipConfig.save(result.config, ctx.path)
      assert saved.written
      assert File.read!(ctx.path) == golden

      assert {result.status, result.api_version, result.from_cache} == {200, "1", false}
      assert result.etag == "\"sha256-4fa9d9042adc20d8\""
      assert result.rate_limit.remaining == 17
    end

    test "the caller's format is overridden; try_new_peers is sent", ctx do
      _ =
        HlPeers.list_peers_as_gossip_config(
          ctx.client,
          Query.new(cc: ["DE"], format: "json", try_new_peers: false)
        )

      assert url(ctx.recorder) ==
               "http://fixtures.invalid/peers?cc=DE&format=gossip_config&try_new_peers=false"
    end

    test "list_peers/3 still refuses the format and names the helper", ctx do
      assert {:error, %Error{kind: :invalid, parameter: "format", reason: reason}} =
               HlPeers.list_peers(ctx.client, Query.new(format: "gossip_config"))

      assert reason =~ "list_peers_as_gossip_config"
    end
  end

  describe "the models cover exactly spec/peer-api-v1.fields.json" do
    setup do
      path = Path.join(Fixtures.root(), "spec/peer-api-v1.fields.json")
      %{fields: path |> File.read!() |> Jason.decode!()}
    end

    defp struct_keys(struct, drop \\ []) do
      struct
      |> Map.from_struct()
      |> Map.keys()
      |> Kernel.--(drop)
      |> Enum.map(&Atom.to_string/1)
      |> Enum.sort()
    end

    test "Peer", %{fields: fields} do
      assert Enum.sort(Peer.known_fields()) == fields["peer"]
      assert struct_keys(%Peer{}, [:extra]) == fields["peer"]
    end

    test "Peer.Mempool and Peer.Auction", %{fields: fields} do
      assert Enum.sort(Mempool.known_fields()) == fields["peer_mempool"]
      assert struct_keys(%Mempool{}) == fields["peer_mempool"]
      assert Enum.sort(Auction.known_fields()) == fields["peer_auction"]
      assert struct_keys(%Auction{}) == fields["peer_auction"]
    end

    test "Envelope", %{fields: fields} do
      assert Enum.sort(Envelope.known_fields()) == fields["envelope"]
      assert struct_keys(%Envelope{}) == fields["envelope"]
    end

    test "the query parameters", %{fields: fields} do
      assert Query.fields() |> Enum.map(&Atom.to_string/1) |> Enum.sort() ==
               fields["list_peers_params"]

      facet_ok =
        Enum.filter(Query.fields(), fn field ->
          Query.validate_for_facets(Query.new(sample(field))) == :ok
        end)

      assert facet_ok |> Enum.map(&Atom.to_string/1) |> Enum.sort() == fields["facets_params"]

      assert NetworkMap.filters() |> Enum.map(&Atom.to_string/1) |> Enum.sort() ==
               fields["network_map_params"]
    end

    test "the network-map results", %{fields: fields} do
      meta = [:rate_limit, :api_version, :etag, :from_cache, :status, :body]

      graph =
        %NetworkMap.Graph{}
        |> struct_keys(meta)
        |> Enum.map(&String.replace(&1, "operator_nodes_count", "operator_nodes"))
        |> Enum.sort()

      summary =
        %NetworkMap.Summary{}
        |> struct_keys(meta)
        |> Enum.map(&String.replace(&1, "operator_nodes_count", "operator_nodes"))
        |> Enum.sort()

      assert graph == fields["network_map_graph"]
      assert summary == fields["network_map_summary"]
      assert NetworkMap.Edge.known_fields() == fields["network_map_edge"]
      assert struct_keys(%NetworkMap.Edge{}) == fields["network_map_edge"]
      assert NetworkMap.CityCluster.known_fields() == fields["network_map_city"]
      assert struct_keys(%NetworkMap.CityCluster{}) == fields["network_map_city"]
    end

    test "the closed enums match", %{fields: fields} do
      for {name, allowed} <- fields["enums"], name in ~w(state serves_mempool auction) do
        assert :ok = Query.validate(Query.new([{String.to_atom(name), allowed}]))
      end
    end
  end

  defp sample(:max_latency_ms), do: [max_latency_ms: 1, vantage: "eu"]
  defp sample(field), do: [{field, sample_value(field)}]

  defp sample_value(:chain), do: "Mainnet"
  defp sample_value(:sort), do: "tier"
  defp sample_value(:format), do: "json"
  defp sample_value(:connectable), do: ["yes"]
  defp sample_value(:state), do: ["open"]
  defp sample_value(:serves_mempool), do: [true]
  defp sample_value(:auction), do: ["bidding"]
  defp sample_value(:seen_within), do: "1h"
  defp sample_value(:min_uptime), do: 0.5
  defp sample_value(field) when field in [:max_tier, :max_blocks_behind, :limit], do: 1
  defp sample_value(field) when field in [:accepting, :try_new_peers], do: true
  defp sample_value(_field), do: ["x"]
end

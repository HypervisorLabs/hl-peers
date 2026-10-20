defmodule HlPeers.V11AdditionsTest do
  @moduledoc """
  Replayed offline from
  `fixtures/http`: the `listening` state, `Peer.city`, the network map's
  `cities` / `attribution` and filters, the summary's mempool / auction
  counts, and the per-address flood `429`.
  """

  use ExUnit.Case, async: true

  alias HlPeers.{Client, Error, NetworkMap, Query}
  alias HlPeers.Test.FixtureAdapter

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

  describe "the listening state" do
    test "is selectable and decodes as-is (peers_state_listening)", ctx do
      {:ok, page} = HlPeers.list_peers(ctx.client, Query.new(state: ["listening"]))

      assert url(ctx.recorder) == "http://fixtures.invalid/peers?state=listening"
      [peer] = page.peers

      assert {peer.id, peer.state, peer.tier, peer.city} ==
               {"203.0.113.40", "listening", nil, nil}

      assert peer.connectable == "unknown"
      assert peer.latest_height == nil
    end

    test "another case is still refused client-side" do
      assert {:error, %Error{kind: :invalid, parameter: "state"}} =
               Query.validate(Query.new(state: ["LISTENING"]))
    end
  end

  describe "Peer.city" do
    test "is nil on a /peers row and never lands in extra", ctx do
      {:ok, page} = HlPeers.list_peers(ctx.client)
      assert Enum.all?(page.peers, &(&1.city == nil))
      refute Enum.any?(page.peers, &Map.has_key?(&1.extra, "city"))
    end
  end

  describe "network map cities" do
    test "the graph carries cities, attribution and per-node city ids", ctx do
      {:ok, graph} = HlPeers.network_map_graph(ctx.client, max_tier: 2)

      assert [%NetworkMap.CityCluster{} = city] = graph.cities

      assert {city.id, city.name, city.cc, city.lat, city.lon, city.peers} ==
               {"DE-frankfurt-am-main", "Frankfurt am Main", "DE", 50.1, 8.7, 2}

      assert city.tiers == %{"1" => 1, "2" => 1}
      assert graph.attribution =~ "IPinfo"
      assert graph.attribution =~ "DB-IP"

      assert Enum.map(graph.nodes, & &1.city) ==
               [nil, "DE-frankfurt-am-main", nil, "DE-frankfurt-am-main"]
    end

    test "the summary carries mempool / auction counts plus cities", ctx do
      {:ok, summary} = HlPeers.network_map_summary(ctx.client)

      assert summary.serves_mempool == %{"true" => 2, "false" => 1, "null" => 1}

      assert summary.auction == %{
               "holds_slot" => 1,
               "bidding" => 1,
               "bid_before" => 1,
               "null" => 1
             }

      assert [%NetworkMap.CityCluster{id: "DE-frankfurt-am-main"}] = summary.cities
      assert summary.attribution =~ "DB-IP"
    end

    test "absent cities stay nil, not []", ctx do
      {:ok, graph} =
        HlPeers.network_map_graph(ctx.client, serves_mempool: [true], auction: ["holds_slot"])

      assert graph.cities == nil
      assert graph.attribution == nil
      assert Enum.all?(graph.nodes, &(&1.city == nil))
    end
  end

  describe "network map mempool / auction filters" do
    test "serialize like the /peers builder (network_map_graph_mempool_auction)", ctx do
      {:ok, graph} =
        HlPeers.network_map_graph(ctx.client, serves_mempool: [true], auction: ["holds_slot"])

      assert url(ctx.recorder) ==
               "http://fixtures.invalid/network-map/graph?auction=holds_slot&serves_mempool=true"

      assert Enum.map(graph.nodes, & &1.id) == ["192.0.2.10"]
      assert graph.edges == []
    end

    test "accept pre-joined CSV and nil members" do
      assert {:ok, [{"auction", "bidding,null"}, {"serves_mempool", "true,null"}]} =
               NetworkMap.to_params(serves_mempool: [true, nil], auction: "bidding,null")
    end

    test "an out-of-enum member is Invalid before any request", ctx do
      for {bad, param} <- [
            {[serves_mempool: ["yes"]], "serves_mempool"},
            {[serves_mempool: "yes"], "serves_mempool"},
            {[auction: ["holding"]], "auction"},
            {[auction: "HOLDS_SLOT"], "auction"}
          ] do
        assert {:error, %Error{kind: :invalid, parameter: ^param}} =
                 HlPeers.network_map_graph(ctx.client, bad)

        assert {:error, %Error{kind: :invalid, parameter: ^param}} =
                 HlPeers.network_map_summary(ctx.client, bad)
      end

      assert FixtureAdapter.request_count(ctx.recorder) == 0
    end
  end

  describe "the per-address flood 429 (error_429_ip_flood)" do
    test "maps to rate_limited like the tier 429", ctx do
      {:error, error} =
        HlPeers.list_peers(ctx.client, %Query{},
          headers: [{"X-Mock-Scenario", "rate-limited-flood"}]
        )

      assert error.kind == :rate_limited
      assert error.retry_after == 1
      assert error.rate_limit.limit == 100
      assert error.rate_limit.remaining == 0
      assert error.rate_limit.policy == "100;w=1"
      assert error.signup_url == "https://hypervisor.gg/docs/peer-api/authentication"
      assert %DateTime{} = error.reset_at
    end
  end
end

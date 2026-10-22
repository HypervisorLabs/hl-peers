defmodule HlPeers.TestnetTest do
  @moduledoc """
  `network` — Testnet (beta), `CLIENT_SPEC.md` §1, §2.1, §4.3, §4.4, §4.8,
  §9 item 7 and §10 D-U. Replayed offline from `fixtures/http/*testnet*.json`
  and `error_400_network_not_served.json`.
  """

  use ExUnit.Case, async: true

  alias HlPeers.{Client, Error, NetworkMap, Query}
  alias HlPeers.Test.FixtureAdapter

  @base "http://fixtures.invalid"

  setup do
    {:ok, recorder} = FixtureAdapter.start_link()
    %{client: client(recorder), recorder: recorder}
  end

  defp client(recorder, opts \\ []) do
    Client.new(
      [
        base_url: @base,
        adapter: FixtureAdapter,
        adapter_opts: [recorder: recorder],
        sleep_fun: fn _ms -> :ok end
      ] ++ opts
    )
  end

  defp url(recorder), do: recorder |> FixtureAdapter.requests() |> List.last() |> Map.get(:url)

  defp rows(peers), do: Enum.map(peers, &[&1.id, &1.version, &1.cc, &1.asn, &1.org, &1.region])

  @testnet_rows [
    ["192.0.2.50", "1550", "JP", "AS64502", "Example Cloud", "aws:ap-northeast-1"],
    ["198.51.100.60", "1550", "US", "AS64503", "Example Hosting US", nil],
    ["203.0.113.70", "1549", "FI", "AS64500", "Example Hosting", nil],
    ["203.0.113.71", nil, nil, nil, nil, nil]
  ]

  describe "the request carries network=testnet, and the testnet body decodes" do
    test "list_peers (peers_testnet)", ctx do
      {:ok, page} = HlPeers.list_peers(ctx.client, Query.new(network: "testnet"))

      assert url(ctx.recorder) == @base <> "/peers?network=testnet"
      assert page.envelope.network == "testnet"
      assert page.envelope.chain == "Testnet"
      assert page.envelope.available == true
      assert rows(page.peers) == @testnet_rows
    end

    test "the atom is accepted and spelled the same on the wire", ctx do
      {:ok, page} = HlPeers.list_peers(ctx.client, network: :testnet)

      assert url(ctx.recorder) == @base <> "/peers?network=testnet"
      assert page.envelope.network == "testnet"
    end

    test "iter_peers walks the testnet graph", ctx do
      ids = ctx.client |> HlPeers.iter_peers(network: "testnet") |> Enum.map(& &1.id)

      assert ids == Enum.map(@testnet_rows, &hd/1)
      assert url(ctx.recorder) == @base <> "/peers?network=testnet"
    end

    test "list_peers_as_gossip_config sends network", ctx do
      _ = HlPeers.list_peers_as_gossip_config(ctx.client, network: "testnet")

      assert url(ctx.recorder) == @base <> "/peers?format=gossip_config&network=testnet"
    end

    test "get_peer (peer_found_testnet)", ctx do
      {:ok, result} = HlPeers.get_peer(ctx.client, "192.0.2.50", network: "testnet")

      assert url(ctx.recorder) == @base <> "/peers/192.0.2.50?network=testnet"
      assert result.envelope.network == "testnet"
      assert result.envelope.chain == "Testnet"
      assert rows([result.peer]) == [hd(@testnet_rows)]
    end

    test "facets (facets_testnet)", ctx do
      {:ok, facets} = HlPeers.facets(ctx.client, Query.new(network: "testnet"))

      assert url(ctx.recorder) == @base <> "/peers/facets?network=testnet"
      assert facets.envelope.network == "testnet"
      assert facets.envelope.chain == "Testnet"
      assert is_integer(facets.total)
    end

    test "network_map_graph (network_map_graph_testnet)", ctx do
      {:ok, graph} = HlPeers.network_map_graph(ctx.client, network: "testnet")

      assert url(ctx.recorder) == @base <> "/network-map/graph?network=testnet"
      assert graph.network == "testnet"
      assert rows(graph.nodes) == @testnet_rows
    end

    test "network_map_summary (network_map_summary_testnet)", ctx do
      {:ok, summary} = HlPeers.network_map_summary(ctx.client, network: "testnet")

      assert url(ctx.recorder) == @base <> "/network-map/summary?network=testnet"
      assert summary.network == "testnet"
      assert summary.available == true
    end
  end

  describe "a mainnet body" do
    test "decodes network as nil and version as nil", ctx do
      {:ok, page} = HlPeers.list_peers(ctx.client)
      assert page.envelope.network == nil
      assert Enum.all?(page.peers, &is_nil(&1.version))

      {:ok, graph} = HlPeers.network_map_graph(ctx.client, max_tier: 2)
      assert graph.network == nil
    end
  end

  describe "network-not-served" do
    test "is a BadRequest with parameter network, not a new error kind", ctx do
      assert {:error, %Error{kind: :bad_request, parameter: "network"} = error} =
               HlPeers.list_peers(ctx.client, Query.new(network: "testnet"),
                 headers: [{"X-Mock-Scenario", "network-not-served"}]
               )

      assert error.allowed == ["mainnet", "testnet"]
      assert Error.bad_request?(error)
    end
  end

  describe "an out-of-enum network is Invalid before any request" do
    for bad <- ["Testnet", "TESTNET", "devnet", :devnet] do
      test "#{inspect(bad)} on every route", ctx do
        bad = unquote(bad)

        for result <- [
              HlPeers.list_peers(ctx.client, network: bad),
              HlPeers.list_peers_as_gossip_config(ctx.client, network: bad),
              HlPeers.facets(ctx.client, network: bad),
              HlPeers.get_peer(ctx.client, "192.0.2.50", network: bad),
              HlPeers.network_map_graph(ctx.client, network: bad),
              HlPeers.network_map_summary(ctx.client, network: bad)
            ] do
          assert {:error, %Error{kind: :invalid, parameter: "network"}} = result
        end

        assert_raise Error, fn ->
          ctx.client |> HlPeers.iter_peers(network: bad) |> Enum.to_list()
        end

        assert FixtureAdapter.request_count(ctx.recorder) == 0
      end
    end

    test "a bad client default is refused the same way", ctx do
      client = client(ctx.recorder, network: "Testnet")

      assert {:error, %Error{kind: :invalid, parameter: "network"}} = HlPeers.list_peers(client)

      assert {:error, %Error{kind: :invalid, parameter: "network"}} =
               HlPeers.get_peer(client, "192.0.2.50")

      assert {:error, %Error{kind: :invalid, parameter: "network"}} =
               HlPeers.network_map_summary(client)

      assert FixtureAdapter.request_count(ctx.recorder) == 0
    end

    test "Query.validate and NetworkMap.to_params" do
      assert {:error, %Error{kind: :invalid, parameter: "network"}} =
               Query.validate(Query.new(network: "Testnet"))

      assert {:error, %Error{kind: :invalid, parameter: "network"}} =
               NetworkMap.to_params(network: "devnet")

      assert {:ok, [{"network", "testnet"}]} = NetworkMap.to_params(network: "testnet")
    end
  end

  describe "the client-level default" do
    setup ctx, do: %{testnet: client(ctx.recorder, network: "testnet")}

    test "applies to every route when the call sets none", ctx do
      {:ok, page} = HlPeers.list_peers(ctx.testnet)
      assert url(ctx.recorder) == @base <> "/peers?network=testnet"
      assert page.envelope.network == "testnet"

      {:ok, _} = HlPeers.get_peer(ctx.testnet, "192.0.2.50")
      assert url(ctx.recorder) == @base <> "/peers/192.0.2.50?network=testnet"

      {:ok, _} = HlPeers.facets(ctx.testnet)
      assert url(ctx.recorder) == @base <> "/peers/facets?network=testnet"

      {:ok, graph} = HlPeers.network_map_graph(ctx.testnet)
      assert url(ctx.recorder) == @base <> "/network-map/graph?network=testnet"
      assert graph.network == "testnet"

      {:ok, _} = HlPeers.network_map_summary(ctx.testnet)
      assert url(ctx.recorder) == @base <> "/network-map/summary?network=testnet"
    end

    test "a per-call value wins", ctx do
      _ = HlPeers.list_peers(ctx.testnet, network: "mainnet")
      assert url(ctx.recorder) == @base <> "/peers?network=mainnet"

      _ = HlPeers.get_peer(ctx.testnet, "192.0.2.10", network: "mainnet")
      assert url(ctx.recorder) == @base <> "/peers/192.0.2.10?network=mainnet"

      _ = HlPeers.facets(ctx.testnet, network: :mainnet)
      assert url(ctx.recorder) == @base <> "/peers/facets?network=mainnet"

      _ = HlPeers.network_map_summary(ctx.testnet, network: "mainnet")
      assert url(ctx.recorder) == @base <> "/network-map/summary?network=mainnet"
    end
  end

  describe "the mainnet golden: network unset sends no network key" do
    test "on every route", ctx do
      {:ok, _} = HlPeers.list_peers(ctx.client)
      assert url(ctx.recorder) == @base <> "/peers"

      {:ok, _} = HlPeers.list_peers(ctx.client, Query.new(cc: ["DE"]))
      assert url(ctx.recorder) == @base <> "/peers?cc=DE"

      _ = ctx.client |> HlPeers.iter_peers(Query.tier1()) |> Enum.to_list()
      assert url(ctx.recorder) == @base <> "/peers?tier=1"

      {:ok, _} = HlPeers.list_peers_as_gossip_config(ctx.client, Query.new(cc: ["DE"]))
      assert url(ctx.recorder) == @base <> "/peers?cc=DE&format=gossip_config"

      {:ok, _} = HlPeers.get_peer(ctx.client, "192.0.2.10")
      assert url(ctx.recorder) == @base <> "/peers/192.0.2.10"

      {:ok, _} = HlPeers.facets(ctx.client)
      assert url(ctx.recorder) == @base <> "/peers/facets"

      {:ok, _} = HlPeers.network_map_graph(ctx.client, max_tier: 2)
      assert url(ctx.recorder) == @base <> "/network-map/graph?max_tier=2"

      {:ok, _} = HlPeers.network_map_summary(ctx.client)
      assert url(ctx.recorder) == @base <> "/network-map/summary"

      refute Enum.any?(FixtureAdapter.requests(ctx.recorder), &(&1.url =~ "network="))
    end

    test "the canonical query string is unchanged" do
      assert Query.to_query_string(Query.new()) == ""
      assert Query.to_query_string(Query.tier1()) == "tier=1"
      assert NetworkMap.to_params([]) == {:ok, []}
    end
  end
end

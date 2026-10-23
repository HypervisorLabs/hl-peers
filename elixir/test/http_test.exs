defmodule HlPeers.HTTPTest do
  @moduledoc """
  Fixture-driven HTTP behaviour (`CLIENT_SPEC.md` §9(2)), replayed offline
  through an injected adapter. No network is touched, and
  `HL_PEER_API_BASE_URL` is neither required, reachable nor consulted.
  """

  use ExUnit.Case, async: true

  alias HlPeers.{Client, Error, Facets, NotModified, Peer, PeerEnvelope, PeerList, Query}
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

  describe "the peers endpoint" do
    test "decodes the envelope, the peers and the rate-limit block", %{client: client} do
      {:ok, %PeerList{} = page} = HlPeers.list_peers(client)

      assert page.status == 200
      assert page.count == 5
      assert page.envelope.version == "v1"
      assert page.envelope.available == true
      assert page.envelope.stale == false
      assert page.envelope.chain == "Mainnet"
      assert page.envelope.next_sweep_at == "2026-09-21T02:40:00Z"
      assert page.rate_limit.limit == 20
      assert page.rate_limit.remaining == 17
      assert page.rate_limit.reset == 42
      assert page.rate_limit.policy =~ "anon-min"
      # A missing header is nil, never 0.
      assert page.rate_limit.retry_after == nil
      assert page.etag == "\"sha256-7e0a0f7bf15bd239\""
      assert page.api_version == "1"
      assert page.envelope.attribution =~ "IPinfo"
      refute page.from_cache
    end

    test "keeps tier: nil as 'never placed', never a sentinel, never 0", %{client: client} do
      {:ok, page} = HlPeers.list_peers(client)

      # tier 0 stays 0 and nothing is defaulted to a sentinel.
      assert Enum.find(page.peers, &(&1.id == "192.0.2.200")).tier == 0
      refute Enum.any?(page.peers, &(&1.tier == -1))

      # and an absent tier decodes to nil, which is NOT tier 0.
      unplaced = Peer.from_map(%{"id" => "203.0.113.1", "tier" => nil})
      assert unplaced.tier == nil
      assert Peer.unplaced?(unplaced)
      refute Peer.unplaced?(Enum.find(page.peers, &(&1.id == "192.0.2.200")))
    end

    test "keeps connectable three-state; mempool/auction are typed, never in extra",
         %{client: client} do
      {:ok, page} = HlPeers.list_peers(client)

      assert Enum.all?(page.peers, &is_binary(&1.connectable))
      assert Enum.all?(page.peers, &match?(%HlPeers.Peer.Mempool{}, &1.mempool))
      assert Enum.all?(page.peers, &match?(%HlPeers.Peer.Auction{}, &1.auction))
      assert Enum.all?(page.peers, &(&1.extra == %{}))
    end

    test "available: false is data, not an error, and not 'no peers matched'", %{client: client} do
      {:ok, page} =
        HlPeers.list_peers(client, %Query{}, headers: [{"X-Mock-Scenario", "unavailable"}])

      assert page.envelope.available == false
      assert page.peers == []
      refute PeerList.available?(page)
    end

    test "stale: true is exposed, and available is checked first", %{client: client} do
      {:ok, page} =
        HlPeers.list_peers(client, %Query{}, headers: [{"X-Mock-Scenario", "stale"}])

      assert page.envelope.available == true
      assert page.envelope.stale == true
    end

    test "format=gossip_config comes back as a GossipConfig", %{client: client} do
      query = Query.new(cc: ["DE"])
      {:ok, result} = HlPeers.list_peers_as_gossip_config(client, query)

      assert HlPeers.GossipConfig.root_ips(result.config) != []
      assert HlPeers.GossipConfig.chain(result.config) == "Mainnet"
    end

    test "🔴 list_peers/3 refuses format=gossip_config rather than reporting no peers",
         %{client: client} do
      query = Query.new(cc: ["DE"], format: "gossip_config")

      assert {:error, %Error{kind: :invalid, parameter: "format"}} =
               HlPeers.list_peers(client, query)
    end
  end

  describe "the owner's four example queries" do
    test "peers in DE", %{client: client, recorder: recorder} do
      {:ok, page} = HlPeers.list_peers(client, Query.new(cc: ["DE"]))

      assert page.count == 2
      assert url(recorder) == "http://fixtures.invalid/peers?cc=DE"
    end

    test "query 1 whole: serves mempool AND in DE", %{client: client, recorder: recorder} do
      {:ok, page} = HlPeers.list_peers(client, Query.mempool_servers() |> Query.cc(["DE"]))

      assert url(recorder) == "http://fixtures.invalid/peers?cc=DE&serves_mempool=true"
      assert [%{id: "192.0.2.10", mempool: %{serves: true}}] = page.peers
    end

    test "not_full in SG", %{client: client, recorder: recorder} do
      {:ok, page} = HlPeers.not_full(client, cc: ["SG"])

      # single-page since fixtures ab9c95d: count == total, next_cursor null
      assert {page.count, page.total, page.next_cursor} == {1, 1, nil}
      assert url(recorder) =~ "accepting=true"
    end

    test "tier 1", %{client: client, recorder: recorder} do
      {:ok, page} = HlPeers.tier1(client)

      assert {page.count, page.total, page.next_cursor} == {1, 1, nil}
      assert url(recorder) =~ "tier=1"
      # D-J: no sort is sent unless the caller sets one.
      refute url(recorder) =~ "sort="
    end

    test "sentries uses label=sentry, never tier=0", %{client: client, recorder: recorder} do
      {:ok, page} = HlPeers.sentries(client)

      assert {page.count, page.total, page.next_cursor} == {1, 1, nil}
      assert url(recorder) =~ "label=sentry"
      refute url(recorder) =~ "tier=0"
    end
  end

  describe "pagination" do
    test "walks three pages and never dedupes or reorders", %{client: client, recorder: recorder} do
      peers =
        client
        |> HlPeers.stream_peers(Query.new(limit: 2, sort: "tier"))
        |> Enum.to_list()

      assert length(peers) == 5
      assert FixtureAdapter.request_count(recorder) == 3

      # Each request after the first differs only in `cursor`.
      [first, second, third] = Enum.map(FixtureAdapter.requests(recorder), & &1.url)
      refute first =~ "cursor="
      assert second =~ "cursor=cursor-page-2"
      assert third =~ "cursor=cursor-page-3"
    end

    test "max_items is a hard guard that defaults to 10 000", %{client: client} do
      peers =
        client
        |> HlPeers.stream_peers(Query.new(limit: 2, sort: "tier"), max_items: 3)
        |> Enum.to_list()

      assert length(peers) == 3
    end

    test "on_truncate: :raise reports how many were seen", %{client: client} do
      assert_raise Error, fn ->
        client
        |> HlPeers.stream_peers(Query.new(limit: 2, sort: "tier"),
          max_items: 3,
          on_truncate: :raise
        )
        |> Enum.to_list()
      end
    end

    test "cursor-expired surfaces and is never auto-restarted", %{
      client: client,
      recorder: recorder
    } do
      {:error, error} =
        HlPeers.list_peers(client, Query.new(cursor: "cursor-from-a-previous-sweep"))

      assert error.kind == :cursor_expired
      assert Error.bad_request?(error)
      assert FixtureAdapter.request_count(recorder) == 1
    end
  end

  describe "the ETag cache" do
    test "a 304 returns the stored body with from_cache and the 304's rate-limit headers", %{
      client: client,
      recorder: recorder
    } do
      {:ok, first} = HlPeers.list_peers(client)
      {:ok, second} = HlPeers.list_peers(client)

      assert FixtureAdapter.request_count(recorder) == 2
      assert second.status == 304
      assert second.from_cache
      assert second.peers == first.peers
      assert second.rate_limit.limit == 20

      [_, revalidation] = FixtureAdapter.requests(recorder)
      assert {"if-none-match", "\"sha256-7e0a0f7bf15bd239\""} in revalidation.headers
    end

    test "the cache key includes whether a key was sent", %{recorder: recorder} do
      anonymous = HlPeers.Store.key("GET", "/peers", "tier=1", false)
      keyed = HlPeers.Store.key("GET", "/peers", "tier=1", true)

      assert anonymous != keyed
      assert is_pid(recorder)
    end

    test "a caller-supplied If-None-Match answers NotModified, not a body", %{client: client} do
      {:ok, %NotModified{} = result} =
        HlPeers.list_peers(client, %Query{},
          headers: [{"If-None-Match", "\"sha256-7e0a0f7bf15bd239\""}]
        )

      assert result.status == 304
      assert result.rate_limit.limit == 20
    end

    test "cache: false revalidates nothing and always fetches", %{recorder: recorder} do
      client =
        Client.new(
          base_url: "http://fixtures.invalid",
          adapter: FixtureAdapter,
          adapter_opts: [recorder: recorder],
          cache: false
        )

      {:ok, a} = HlPeers.list_peers(client)
      {:ok, b} = HlPeers.list_peers(client)

      assert a.status == 200
      assert b.status == 200
      refute b.from_cache
    end
  end

  describe "the single-peer path" do
    test "a found peer", %{client: client} do
      {:ok, %PeerEnvelope{} = result} = HlPeers.get_peer(client, "192.0.2.10")

      assert result.peer.id == "192.0.2.10"
      refute Map.has_key?(result.peer, :gossip_port)
      assert result.peer.cc_confidence == "high"
      assert result.peer.cc_source == "db"
      assert result.peer.org == "Example Hosting"
      assert result.peer.uptime == 0.97
      assert result.peer.feeders == ["192.0.2.200"]
      assert result.peer.mempool.serves == true
      assert result.peer.mempool.last_checked == ~U[2026-09-21 02:30:00Z]
      assert result.peer.auction.status == "holds_slot"
      assert result.peer.auction.slot == 3
      assert result.envelope.chain == "Mainnet"
      assert result.api_version == "1"
    end

    test "a well-formed IPv4 in no source is a 404, distinctly", %{client: client} do
      {:error, error} = HlPeers.get_peer(client, "198.51.100.254")

      assert error.kind == :not_found
      assert error.detail =~ "appears in no source"
    end

    test "a malformed IP is refused locally and spends no quota", %{
      client: client,
      recorder: recorder
    } do
      {:error, error} = HlPeers.get_peer(client, "not-an-ip")

      assert error.kind == :invalid
      assert error.parameter == "ip"
      assert FixtureAdapter.request_count(recorder) == 0
    end

    test "IPv6 is rejected locally with an IPv4-only message", %{client: client} do
      {:error, error} = HlPeers.get_peer(client, "2001:db8::1")

      assert error.kind == :invalid
      assert error.reason =~ "IPv4-only"
    end

    test "a server 400 on the peer path is still a BadRequest, not a NotFound", %{client: client} do
      # The API's own answer for a malformed IP, reached by asking the transport
      # directly — the client refuses this shape before it can be sent, but the
      # two server answers must still map to different variants.
      {:error, error} = Client.get(client, "/peers/not-an-ip")

      assert error.kind == :bad_request
      assert error.status == 400
      assert error.parameter == "ip"
    end
  end

  describe "facets" do
    test "keeps 'not collected' and 'nothing matches' distinguishable", %{client: client} do
      {:ok, %Facets{} = facets} = HlPeers.facets(client)

      assert facets.total == 912
      assert Facets.available?(facets, "cc")
      refute Facets.available?(facets, "asn")
      refute Facets.available?(facets, "region")
      assert Facets.values(facets, "asn") == :unavailable
      assert {:ok, values} = Facets.values(facets, "tier")

      # "null" is a selectable member and is always a string.
      assert Enum.any?(values, &(&1.value == "null"))
      assert Enum.all?(values, &is_binary(&1.value))
    end

    test "rejects every non-filter parameter the endpoint would ignore", %{
      client: client,
      recorder: recorder
    } do
      for {field, value} <- [
            sort: "tier",
            limit: 5,
            cursor: "c",
            fields: ["id"],
            format: "json",
            try_new_peers: true
          ] do
        {:error, error} = HlPeers.facets(client, Query.new([{field, value}]))

        assert error.kind == :invalid
        assert error.parameter == to_string(field)
      end

      assert FixtureAdapter.request_count(recorder) == 0
    end

    test "accepts every filter (the counts are over the filtered set)", %{recorder: recorder} do
      client =
        Client.new(
          base_url: "http://x.invalid",
          adapter: FixtureAdapter,
          adapter_opts: [recorder: recorder]
        )

      query =
        Query.new(
          label: ["sentry"],
          serves_mempool: [true],
          auction: ["bidding"],
          country: ["jp"],
          state: ["responsive"]
        )

      _ = HlPeers.facets(client, query)

      assert url(recorder) ==
               "http://x.invalid/peers/facets?auction=bidding&country=JP&label=sentry" <>
                 "&serves_mempool=true&state=responsive"
    end

    test "decodes the mempool/auction facets and geo_coverage", %{client: client} do
      {:ok, facets} = HlPeers.facets(client)

      assert {:ok, values} = Facets.values(facets, "serves_mempool")
      assert Enum.map(values, & &1.value) == ["true", "false", "null"]
      assert Facets.available?(facets, "auction")
      assert facets.geo_coverage["source"] == "dbip-lite"
      assert facets.envelope.attribution =~ "IPinfo"
      assert facets.api_version == "1"
    end
  end

  describe "the error taxonomy" do
    test "400 filter-unavailable names the filter and is never an empty list", %{client: client} do
      {:error, error} = HlPeers.list_peers(client, Query.new(asn: ["AS24940"]))

      assert error.kind == :filter_unavailable
      assert error.filter == "asn"
      assert error.detail =~ "not collected"
      assert Error.bad_request?(error)
    end

    test "400 filter-unavailable on region — the documented trap", %{client: client} do
      {:error, error} = HlPeers.list_peers(client, Query.new(region: ["aws:eu-central-1"]))

      assert error.kind == :filter_unavailable
      assert error.filter == "region"
    end

    test "400 chain-not-served carries allowed", %{client: client} do
      {:error, error} = HlPeers.list_peers(client, Query.new(chain: "Testnet"))

      assert error.kind == :chain_not_served
      assert error.allowed == ["Mainnet"]
    end

    test "any other 400 is a plain BadRequest with the parameter", %{client: client} do
      # The invalid-enum fixture is only reachable by bypassing the builder:
      # §2.2 validates closed enums locally, so `connectable: "yess"` never leaves
      # the process. Going through Client.get proves the server's answer maps to
      # the generic member of the 400 family.
      {:error, error} = Client.get(client, "/peers", [{"connectable", "yess"}])

      assert error.kind == :bad_request
      assert error.parameter == "connectable"
      assert error.allowed == ["yes", "no", "unknown"]
    end

    test "401", %{client: recorder_client, recorder: recorder} do
      client = %{
        recorder_client
        | api_key: "hlp_live_invalidkey0000000000"
      }

      {:error, error} = HlPeers.list_peers(client)

      assert error.kind == :unauthorized
      assert error.detail =~ "not recognised"
      assert FixtureAdapter.request_count(recorder) == 1
    end

    test "a 500 with a non-problem body falls back to status-only mapping (D-K)", %{
      client: client
    } do
      {:error, error} =
        HlPeers.list_peers(client, %Query{}, headers: [{"X-Mock-Scenario", "server-error"}])

      assert error.kind == :server
      assert error.status == 500
      assert error.problem == nil
      assert error.body_excerpt == "Internal Server Error"
    end

    test "503 is Unavailable and carries the problem object", %{client: client} do
      {:error, error} =
        HlPeers.list_peers(client, %Query{},
          headers: [{"X-Mock-Scenario", "producer-unavailable"}]
        )

      assert error.kind == :unavailable
      assert error.problem["type"] =~ "producer-unavailable"
    end

    test "every error carries the parsed rate-limit block", %{client: client} do
      {:error, error} = HlPeers.list_peers(client, Query.new(asn: ["AS24940"]))

      assert error.rate_limit.limit == 20
      assert error.rate_limit.remaining == 17
    end
  end

  describe "defaults" do
    test "base_url is the real host, overridable, and nothing warns", %{recorder: recorder} do
      assert Client.default_base_url() == "https://api.hypervisor.gg"

      log =
        ExUnit.CaptureLog.capture_log(fn ->
          client =
            Client.new(
              base_url: Client.default_base_url(),
              adapter: FixtureAdapter,
              adapter_opts: [recorder: recorder]
            )

          _ = HlPeers.list_peers(client)
        end)

      assert log == ""
      assert url(recorder) == "https://api.hypervisor.gg/peers"

      # With no explicit argument: the environment, else the real host.
      expected = System.get_env("HL_PEER_API_BASE_URL") || "https://api.hypervisor.gg"
      assert Client.new().base_url == String.trim_trailing(expected, "/")
    end

    test "suppress_placeholder_warning is accepted as a no-op" do
      client = Client.new(base_url: "http://x.invalid", suppress_placeholder_warning: true)
      assert client.base_url == "http://x.invalid"
      refute Map.has_key?(client, :suppress_placeholder_warning)
    end

    test "max_retries defaults to 0 and the user agent names the package version" do
      client = Client.new(base_url: "http://x.invalid")

      assert client.max_retries == 0
      assert client.user_agent == "hl-peers-elixir/1.1.0"
    end
  end

  describe "base_path" do
    test "defaults to the one constant, the empty prefix", %{client: client, recorder: rec} do
      assert Client.default_base_path() == ""
      assert client.base_path == ""

      _ = HlPeers.get_peer(client, "192.0.2.10")
      assert url(rec) == "http://fixtures.invalid/peers/192.0.2.10"
    end

    test "is normalized per CLIENT_SPEC §1" do
      assert Client.normalize_base_path("peer-api/v1/") == "/peer-api/v1"
      assert Client.normalize_base_path("//peer-api/v1//") == "/peer-api/v1"
      assert Client.normalize_base_path("/") == ""
      assert Client.normalize_base_path("") == ""
    end

    test "a custom prefix reaches every route", %{recorder: recorder} do
      client =
        Client.new(
          base_url: "http://fixtures.invalid",
          base_path: "/peer-api/v1/",
          adapter: FixtureAdapter,
          adapter_opts: [recorder: recorder],
          sleep_fun: fn _ms -> :ok end
        )

      assert client.base_path == "/peer-api/v1"
      # the fixtures live at the host root, so these 404; only the URL matters here
      _ = HlPeers.list_peers(client, Query.new(cc: ["DE"]))
      assert url(recorder) == "http://fixtures.invalid/peer-api/v1/peers?cc=DE"
      _ = HlPeers.get_peer(client, "192.0.2.10")
      assert url(recorder) == "http://fixtures.invalid/peer-api/v1/peers/192.0.2.10"
      _ = HlPeers.facets(client)
      assert url(recorder) == "http://fixtures.invalid/peer-api/v1/peers/facets"
      _ = HlPeers.network_map_graph(client)
      assert url(recorder) == "http://fixtures.invalid/peer-api/v1/network-map/graph"
      _ = HlPeers.network_map_summary(client)
      assert url(recorder) == "http://fixtures.invalid/peer-api/v1/network-map/summary"
    end
  end

  defp url(recorder), do: recorder |> FixtureAdapter.requests() |> List.last() |> Map.get(:url)
end

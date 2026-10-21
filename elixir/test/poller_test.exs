defmodule HlPeers.PollerTest do
  @moduledoc "The optional poller (`CLIENT_SPEC.md` §4.6)."

  use ExUnit.Case, async: true

  alias HlPeers.{Client, GossipConfig, Poller, Query}
  alias HlPeers.Test.FixtureAdapter

  setup do
    {:ok, recorder} = FixtureAdapter.start_link()

    client =
      Client.new(
        base_url: "http://fixtures.invalid",
        adapter: FixtureAdapter,
        adapter_opts: [recorder: recorder]
      )

    %{client: client, recorder: recorder}
  end

  test "refuses an interval below min_poll_interval", ctx do
    assert {:error, error} =
             Poller.start_link(client: ctx.client, query: Query.tier1(), interval: 5_000)

    assert error.kind == :invalid
    assert error.parameter == "interval"
    assert error.reason =~ "polling faster"
  end

  test "calls the callback on the first poll and uses the ETag cache afterwards", ctx do
    parent = self()

    {:ok, poller} =
      Poller.start_link(
        client: ctx.client,
        query: Query.new(cc: ["DE"]),
        interval: 600_000,
        on_change: fn page -> send(parent, {:changed, page.etag}) end
      )

    assert_receive {:changed, etag}, 2_000
    assert is_binary(etag)

    # A second poll revalidates and gets a 304, which costs no quota unit and
    # fires no change callback.
    :ok = Poller.poll_now(poller)
    refute_receive {:changed, _}, 200
    assert FixtureAdapter.request_count(ctx.recorder) == 2
  end

  test "updates a gossip config file on change, and never restarts anything", ctx do
    dir = Path.join(System.tmp_dir!(), "hl_peers_poll_#{:erlang.unique_integer([:positive])}")
    File.mkdir_p!(dir)
    on_exit(fn -> File.rm_rf!(dir) end)
    path = Path.join(dir, "override_gossip_config.json")

    {:ok, _poller} =
      Poller.start_link(
        client: ctx.client,
        query: Query.new(cc: ["DE"]),
        interval: 600_000,
        gossip_path: path
      )

    wait_for(fn -> File.exists?(path) end)

    {:ok, config} = GossipConfig.load(path)
    assert GossipConfig.root_ips(config) == ["192.0.2.10", "192.0.2.11"]
    assert GossipConfig.chain(config) == "Mainnet"
  end

  defp wait_for(fun, tries \\ 100) do
    cond do
      fun.() -> :ok
      tries == 0 -> flunk("condition never became true")
      true -> Process.sleep(20) && wait_for(fun, tries - 1)
    end
  end
end

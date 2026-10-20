defmodule HlPeers.IntegrationTest do
  @moduledoc """
  The one test that speaks real HTTP, against `tools/mock_server.py` on a free
  port — the default adapter (`Req`), real sockets, real headers.

  It is tagged `:integration` and **excluded by default**, so `mix test` needs
  no network, no Python and no listening port. Run it with:

      mix test --include integration
  """

  use ExUnit.Case, async: false

  @moduletag :integration

  alias HlPeers.{Client, GossipConfig, Query}
  alias HlPeers.Test.Fixtures

  setup_all do
    port = free_port()
    script = Path.join(Fixtures.root(), "tools/mock_server.py")
    python = System.find_executable("python3") || flunk("python3 is not on PATH")

    server =
      Port.open({:spawn_executable, python}, [
        :binary,
        :exit_status,
        args: [script, "--port", Integer.to_string(port)]
      ])

    {:os_pid, os_pid} = Port.info(server, :os_pid)

    on_exit(fn ->
      System.cmd("kill", ["-9", Integer.to_string(os_pid)], stderr_to_stdout: true)
    end)

    wait_until_listening(port)

    %{base_url: "http://127.0.0.1:#{port}"}
  end

  setup %{base_url: base_url} do
    %{client: Client.new(base_url: base_url)}
  end

  test "lists peers over real HTTP", %{client: client} do
    {:ok, page} = HlPeers.list_peers(client)

    assert page.count == 5
    assert page.rate_limit.limit == 20
    assert page.etag != nil
  end

  test "the owner's four example queries", %{client: client} do
    assert {:ok, %{count: 2, next_cursor: nil}} =
             HlPeers.list_peers(client, Query.new(cc: ["DE"]))

    assert {:ok, %{count: 1, total: 1, next_cursor: nil}} = HlPeers.not_full(client, cc: ["SG"])
    assert {:ok, %{count: 1, total: 1, next_cursor: nil}} = HlPeers.tier1(client)
    assert {:ok, %{count: 1, total: 1, next_cursor: nil}} = HlPeers.sentries(client)
  end

  test "the ETag cache turns a repeat poll into a free 304", %{client: client} do
    {:ok, first} = HlPeers.list_peers(client)
    {:ok, second} = HlPeers.list_peers(client)

    assert second.status == 304
    assert second.from_cache
    assert second.peers == first.peers
  end

  test "pagination walks three real pages", %{client: client} do
    peers =
      client |> HlPeers.stream_peers(Query.new(limit: 2, sort: "tier")) |> Enum.to_list()

    assert length(peers) == 5
  end

  test "a 400 filter-unavailable is not swallowed", %{client: client} do
    {:error, error} = HlPeers.list_peers(client, Query.new(asn: ["AS24940"]))

    assert error.kind == :filter_unavailable
    assert error.filter == "asn"
  end

  test "end to end: a query becomes a gossip config on disk", %{client: client} do
    dir = Path.join(System.tmp_dir!(), "hl_peers_int_#{:erlang.unique_integer([:positive])}")
    File.mkdir_p!(dir)
    on_exit(fn -> File.rm_rf!(dir) end)
    path = Path.join(dir, "override_gossip_config.json")

    {:ok, result} =
      HlPeers.update_gossip_config(client, Query.new(cc: ["DE"]), path)

    assert result.changed
    assert File.read!(path) == result.json
    assert {:ok, config} = GossipConfig.load(path)
    assert GossipConfig.root_ips(config) == ["192.0.2.10", "192.0.2.11"]
  end

  defp free_port do
    {:ok, socket} = :gen_tcp.listen(0, [:binary, active: false, reuseaddr: true])
    {:ok, port} = :inet.port(socket)
    :ok = :gen_tcp.close(socket)
    port
  end

  defp wait_until_listening(port, tries \\ 200) do
    case :gen_tcp.connect(~c"127.0.0.1", port, [:binary, active: false], 100) do
      {:ok, socket} ->
        :gen_tcp.close(socket)
        :ok

      {:error, _} when tries > 0 ->
        Process.sleep(25)
        wait_until_listening(port, tries - 1)

      {:error, reason} ->
        flunk("mock_server.py never came up on #{port}: #{inspect(reason)}")
    end
  end
end

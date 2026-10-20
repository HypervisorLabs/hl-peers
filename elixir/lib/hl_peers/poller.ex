defmodule HlPeers.Poller do
  @moduledoc """
  An **optional** supervised poller (`CLIENT_SPEC.md` §4.6). Nothing else in this
  library needs a process; basic use is plain function calls.

  It re-runs one query on a schedule, and on a **change** either calls your
  callback or rewrites a gossip config file in place.

  > 🔴 It never restarts, signals or reloads your node. hl-node reads
  > `override_gossip_config.json` **when the node starts**, so a file
  > this poller rewrites takes effect at the node's next start — which is yours
  > to schedule, at your moment. Restarting a node *to apply a config* while it
  > is behind can make a lagging box worse.

  * an `:interval` below the client's `:min_poll_interval` (default 60 s) is
    refused with `Invalid{parameter: "interval"}`, because polling faster than the data refreshes returns identical bytes and spends quota;
  * every poll goes through the ETag cache, so unchanged data costs **no
    quota unit** — it is a `304`;
  * when the envelope carries `next_sweep_at`, the poller sleeps until then plus
    a small jitter (5–15 s, randomised once per poller) instead of guessing with
    `:interval`.

  ## Options

  | option | meaning |
  |---|---|
  | `:client` | required, a `HlPeers.Client` |
  | `:query` | required, a `HlPeers.Query` or keyword list |
  | `:interval` | milliseconds, `>= client.min_poll_interval` |
  | `:on_change` | `fun(HlPeers.PeerList.t()) -> any`, called only when the ETag changed |
  | `:on_error` | `fun(HlPeers.Error.t()) -> any`; the poller logs and keeps going |
  | `:gossip_path` | when set, `HlPeers.update_gossip_config/4` is run against it on change |
  | `:gossip_opts` | options forwarded to `update_gossip_config/4` |

      {:ok, _pid} =
        HlPeers.Poller.start_link(
          client: client,
          query: HlPeers.Query.new(cc: ["DE"]),
          interval: 300_000,
          gossip_path: HlPeers.GossipConfig.default_config_path()
        )
  """

  use GenServer

  require Logger

  alias HlPeers.{Client, Error, PeerList, Query}

  @type option ::
          {:client, Client.t()}
          | {:query, Query.t() | keyword()}
          | {:interval, pos_integer()}
          | {:on_change, (PeerList.t() -> any())}
          | {:on_error, (Error.t() -> any())}
          | {:gossip_path, Path.t()}
          | {:gossip_opts, keyword()}

  @doc "Starts the poller. Returns `{:error, %HlPeers.Error{}}` for too fast an interval."
  @spec start_link([option()]) :: GenServer.on_start() | {:error, Error.t()}
  def start_link(opts) do
    client = Keyword.fetch!(opts, :client)
    interval = Keyword.get(opts, :interval, client.min_poll_interval)

    if interval < client.min_poll_interval do
      {:error,
       Error.invalid(
         "interval",
         "#{interval} ms is below min_poll_interval (#{client.min_poll_interval} ms). The " <>
           "data refreshes on the server's schedule: polling faster returns identical bytes and spends quota."
       )}
    else
      GenServer.start_link(__MODULE__, Keyword.put(opts, :interval, interval), name: opts[:name])
    end
  end

  @doc "Polls once, synchronously, ignoring the schedule."
  @spec poll_now(GenServer.server()) :: :ok
  def poll_now(server), do: GenServer.call(server, :poll_now, 30_000)

  @impl true
  def init(opts) do
    state = %{
      client: Keyword.fetch!(opts, :client),
      query: Keyword.fetch!(opts, :query),
      interval: Keyword.fetch!(opts, :interval),
      on_change: Keyword.get(opts, :on_change),
      on_error: Keyword.get(opts, :on_error),
      gossip_path: Keyword.get(opts, :gossip_path),
      gossip_opts: Keyword.get(opts, :gossip_opts, []),
      # 5–15 s, randomised once per poller instance, so a fleet of them does not
      # stampede the refresh boundary together.
      jitter_ms: 5_000 + :rand.uniform(10_000),
      last_etag: nil,
      next_sweep_at: nil
    }

    {:ok, state, {:continue, :poll}}
  end

  @impl true
  def handle_continue(:poll, state), do: {:noreply, schedule(poll(state))}

  @impl true
  def handle_info(:poll, state), do: {:noreply, schedule(poll(state))}
  def handle_info(_message, state), do: {:noreply, state}

  @impl true
  def handle_call(:poll_now, _from, state), do: {:reply, :ok, poll(state)}

  defp poll(state) do
    case HlPeers.list_peers(state.client, state.query) do
      {:ok, %PeerList{} = page} ->
        handle_page(state, page)

      {:ok, _not_modified} ->
        state

      {:error, error} ->
        if state.on_error,
          do: state.on_error.(error),
          else: Logger.warning(Exception.message(error))

        state
    end
  end

  defp handle_page(state, page) do
    changed? = is_nil(state.last_etag) or page.etag != state.last_etag

    if changed? do
      if state.on_change, do: state.on_change.(page)
      maybe_update_gossip(state, page)
    end

    %{state | last_etag: page.etag, next_sweep_at: page.envelope.next_sweep_at}
  end

  defp maybe_update_gossip(%{gossip_path: nil}, _page), do: :ok

  defp maybe_update_gossip(state, page) do
    # available: false never reaches a file — update_gossip_config refuses it.
    case HlPeers.update_gossip_config(
           state.client,
           page.peers,
           state.gossip_path,
           Keyword.put_new(state.gossip_opts, :chain, page.envelope.chain)
         ) do
      {:ok, result} ->
        if result.changed do
          Logger.info(
            "wrote #{state.gossip_path}. The node reads this file when it starts — the " <>
              "change takes effect on the node's next start, which is yours to schedule."
          )
        end

        Enum.each(result.warnings, &Logger.warning(&1.message))

      {:error, error} ->
        if state.on_error,
          do: state.on_error.(error),
          else: Logger.warning(Exception.message(error))
    end
  end

  defp schedule(state) do
    Process.send_after(self(), :poll, delay(state))
    state
  end

  # Prefer the data's own clock when it is in the future.
  defp delay(%{next_sweep_at: next_sweep_at, jitter_ms: jitter} = state)
       when is_binary(next_sweep_at) do
    case DateTime.from_iso8601(next_sweep_at) do
      {:ok, at, _offset} ->
        ms = DateTime.diff(at, DateTime.utc_now(), :millisecond) + jitter
        if ms > 0, do: max(ms, state.interval), else: state.interval

      _ ->
        state.interval
    end
  end

  defp delay(state), do: state.interval
end

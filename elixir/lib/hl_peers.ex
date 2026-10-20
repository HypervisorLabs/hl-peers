defmodule HlPeers do
  @moduledoc """
  A client for the Hyperliquid **Peer API**, and a pure offline editor for
  hl-node's `override_gossip_config.json`.

  The default host is `https://api.hypervisor.gg` (override with `:base_url` or
  `HL_PEER_API_BASE_URL`); routes are plain paths at the host root. The test
  suite never touches it — it runs against `fixtures/` offline.

  Two namespaces, always separate:

  | namespace | nature | needs network? |
  |---|---|---|
  | `HlPeers` / `HlPeers.Client` / `HlPeers.Query` | the HTTP query surface | yes |
  | `HlPeers.GossipConfig` | pure, offline, deterministic file algebra | **never** |

  `HlPeers.GossipConfig` does not depend on the HTTP layer: an operator can
  edit a config file on a box with no API access at all. Only
  `update_gossip_config/4` joins the two.

  ## Quickstart

      client = HlPeers.Client.new()

      {:ok, page} = HlPeers.list_peers(client, HlPeers.Query.tier1())
      page.peers |> Enum.map(& &1.id)

      # every page, lazily, with a hard guard at max_items (default 10 000)
      client |> HlPeers.stream_peers(HlPeers.Query.sentries()) |> Enum.take(5)

  Everything answers `{:ok, term}` or `{:error, %HlPeers.Error{}}`; the `!`
  variants raise the same struct.

  > 🔴 The node reads `override_gossip_config.json` **when it starts**. Writing it does not steer a running node, and this library will
  > never restart, signal or otherwise disturb your node — that is the
  > operator's decision, at the operator's moment.
  """

  alias HlPeers.{
    Client,
    Envelope,
    Error,
    Facet,
    Facets,
    GossipConfig,
    GossipConfigResult,
    NetworkMap,
    NotModified,
    Peer,
    PeerEnvelope,
    PeerList,
    Query,
    UpdateResult
  }

  # Route suffixes under the client's `base_path` (default
  # `HlPeers.Client.default_base_path/0`).
  @peers_path "/peers"
  @facets_path "/peers/facets"
  @graph_path "/network-map/graph"
  @summary_path "/network-map/summary"
  @default_max_items 10_000

  # ------------------------------------------------------------------
  # queries
  # ------------------------------------------------------------------

  @doc """
  One page of peers.

  Returns a `HlPeers.PeerList` carrying the peers, the envelope, the
  rate-limit block, `api_version`, the ETag, `from_cache` and the HTTP status.

  `format: "gossip_config"` is refused with `Invalid{parameter: "format"}` —
  that body has no `peers` array; use `list_peers_as_gossip_config/3`.

  🔴 `page.envelope.available == false` is returned **as-is**: it means the
  producer could not build a graph, which is not an error and is emphatically
  not "no peers matched".

  A caller-supplied `If-None-Match` (via `headers:`) answers
  `{:ok, %HlPeers.NotModified{}}` on a 304, because you asked the conditional
  question yourself. The client's own cache instead returns the stored body with
  `from_cache: true`.
  """
  @spec list_peers(Client.t(), Query.t() | keyword(), keyword()) ::
          {:ok, PeerList.t() | NotModified.t()} | {:error, Error.t()}
  def list_peers(client, query \\ %Query{}, opts \\ []) do
    query = to_query(query)

    with :ok <- Query.validate(query),
         :ok <- refuse_gossip_config_format(query),
         {:ok, response} <-
           Client.get(client, client.base_path <> @peers_path, Query.to_params(query), opts) do
      decode(response, &peer_list/2)
    end
  end

  # 🔴 §4.1: `format=gossip_config` has no `peers` array. Decoding it as a page
  # would hand the caller an empty peer list that is not a finding about the
  # network — the one mistake §3.1 exists to prevent — so the request is refused
  # before it is sent rather than after.
  defp refuse_gossip_config_format(%Query{format: :gossip_config}) do
    {:error,
     Error.invalid(
       "format",
       "format=gossip_config does not return a peer list, so list_peers/3 cannot decode " <>
         "it; use HlPeers.list_peers_as_gossip_config/3, which returns a GossipConfig"
     )}
  end

  defp refuse_gossip_config_format(%Query{format: "gossip_config"}) do
    refuse_gossip_config_format(%Query{format: :gossip_config})
  end

  defp refuse_gossip_config_format(%Query{}), do: :ok

  @doc """
  `format=gossip_config`: the server's own rendering of the filtered result, as
  a `HlPeers.GossipConfig` (`CLIENT_SPEC.md` §4.7).

  The query is sent with `format=gossip_config` (a caller-set `format` is
  overridden; a caller-set `try_new_peers` is sent). The body is parsed with
  `HlPeers.GossipConfig.parse/1` and returned in a
  `HlPeers.GossipConfigResult` with `rate_limit`, `api_version`, `etag`,
  `from_cache` and `status`.

  🔴 Nothing is written. Write with the one safe writer,
  `HlPeers.GossipConfig.save/3` — it validates, renames atomically, creates
  the file `0600` and can keep a `.bak`:

      {:ok, result} =
        HlPeers.list_peers_as_gossip_config(client, Query.new(cc: ["DE"], try_new_peers: true))

      {:ok, _} = HlPeers.GossipConfig.save(result.config, "override_gossip_config.json")

  The server never emits an empty root list — it answers `400 empty-root-list`,
  which surfaces as `:bad_request`.

  > 🔴 The node reads `override_gossip_config.json` **when it starts**. Writing it does not steer a running node; the change takes effect
  > at the node's next start, which is yours to schedule.
  """
  @spec list_peers_as_gossip_config(Client.t(), Query.t() | keyword(), keyword()) ::
          {:ok, GossipConfigResult.t() | NotModified.t()} | {:error, Error.t()}
  def list_peers_as_gossip_config(client, query \\ %Query{}, opts \\ []) do
    query = query |> to_query() |> Query.put(:format, "gossip_config")

    with :ok <- Query.validate(query),
         {:ok, response} <-
           Client.get(client, client.base_path <> @peers_path, Query.to_params(query), opts) do
      gossip_config_result(response)
    end
  end

  defp gossip_config_result(%{not_modified: true} = response), do: decode(response, nil)

  defp gossip_config_result(response) do
    case GossipConfig.parse(response.body) do
      {:ok, config} ->
        {:ok,
         %GossipConfigResult{
           config: config,
           rate_limit: response.rate_limit,
           api_version: response.api_version,
           etag: response.etag,
           from_cache: response.from_cache,
           status: response.status
         }}

      {:error, %Error{} = error} ->
        {:error, %{error | rate_limit: response.rate_limit, api_version: response.api_version}}
    end
  end

  @doc "Raising `list_peers_as_gossip_config/3`."
  @spec list_peers_as_gossip_config!(Client.t(), Query.t() | keyword(), keyword()) ::
          GossipConfigResult.t() | NotModified.t()
  def list_peers_as_gossip_config!(client, query \\ %Query{}, opts \\ []),
    do: unwrap!(list_peers_as_gossip_config(client, query, opts))

  @doc "Raising `list_peers/3`."
  @spec list_peers!(Client.t(), Query.t() | keyword(), keyword()) ::
          PeerList.t() | NotModified.t()
  def list_peers!(client, query \\ %Query{}, opts \\ []),
    do: unwrap!(list_peers(client, query, opts))

  @doc """
  A lazy `Stream` over cursor pagination, to exhaustion.

  Each page after the first repeats `query` byte-identically with only `cursor`
  replaced by the previous page's `next_cursor`. The stream ends when
  `next_cursor` is `nil`, when a page is empty, when `available == false`, or at
  `max_items`.

  * `:max_items` — **defaults to 10 000** and is a hard guard. A client must not
    default to unbounded; the whole directory is ~900 nodes, which is 18
    anonymous pages at `limit=50`.
  * `:on_truncate` — `:stop` (default) ends the stream silently, `:raise` raises
    `%HlPeers.Error{kind: :truncated}`.

  🪤 A cursor is bound to the graph generation it was issued in. Paging across a data refresh answers `400 cursor-expired`, which surfaces here as
  `%HlPeers.Error{kind: :cursor_expired}` — this client **never** silently
  restarts the walk, because that would mix two generations into one result set,
  which is exactly what the server refused to do. Catch it and start over.

  Errors are raised into the stream, since a `Stream` has nowhere to put an
  `{:error, _}`; use `list_peers/3` in a loop if you need tuples.
  """
  @spec stream_peers(Client.t(), Query.t() | keyword(), keyword()) :: Enumerable.t()
  def stream_peers(client, query \\ %Query{}, opts \\ []) do
    max_items = Keyword.get(opts, :max_items, @default_max_items)
    on_truncate = Keyword.get(opts, :on_truncate, :stop)
    query = to_query(query)

    Stream.resource(
      fn -> {:start, 0} end,
      fn
        :halt ->
          {:halt, :halt}

        {cursor_state, seen} ->
          next_page(client, query, cursor_state, seen, max_items, on_truncate, opts)
      end,
      fn _ -> :ok end
    )
  end

  @doc "`stream_peers/3` under its spec name; the two are the same function."
  @spec iter_peers(Client.t(), Query.t() | keyword(), keyword()) :: Enumerable.t()
  def iter_peers(client, query \\ %Query{}, opts \\ []), do: stream_peers(client, query, opts)

  @doc """
  The eager convenience over `stream_peers/3`, with the same `max_items` guard.
  """
  @spec list_all_peers(Client.t(), Query.t() | keyword(), keyword()) ::
          {:ok, [Peer.t()]} | {:error, Error.t()}
  def list_all_peers(client, query \\ %Query{}, opts \\ []) do
    {:ok, client |> stream_peers(query, opts) |> Enum.to_list()}
  rescue
    error in [Error] -> {:error, error}
  end

  defp next_page(client, query, cursor_state, seen, max_items, on_truncate, opts) do
    query = if cursor_state == :start, do: query, else: Query.put(query, :cursor, cursor_state)

    case list_peers(client, query, opts) do
      {:ok, %PeerList{} = page} ->
        emit(page, seen, max_items, on_truncate)

      {:ok, %NotModified{}} ->
        {:halt, :halt}

      {:error, error} ->
        raise error
    end
  end

  defp emit(page, seen, max_items, on_truncate) do
    taken = Enum.take(page.peers, max(max_items - seen, 0))
    now_seen = seen + length(taken)
    truncated? = now_seen >= max_items

    cond do
      # Stop immediately on available: false, without raising. The caller tells
      # the difference from "no peers matched" by inspecting the flag.
      page.envelope.available == false -> {[], :halt}
      page.peers == [] -> {[], :halt}
      truncated? and on_truncate == :raise -> raise Error.new(:truncated, seen: now_seen)
      truncated? or is_nil(page.next_cursor) -> {taken, :halt}
      true -> {taken, {page.next_cursor, now_seen}}
    end
  end

  @doc """
  One peer by IPv4.

  🔴 A malformed IPv4 and a well-formed one that appears in no source are
  **different answers** and never collapse: the shape is validated locally first
  (so a typo does not spend a quota unit) and raises
  `%Error{kind: :invalid, parameter: "ip"}`, while a server `400` maps to
  `:bad_request` and a `404` to `:not_found`, distinctly. IPv6 input is rejected
  locally — the API is IPv4-only.
  """
  @spec get_peer(Client.t(), String.t(), keyword()) ::
          {:ok, PeerEnvelope.t() | NotModified.t()} | {:error, Error.t()}
  def get_peer(client, ip, opts \\ []) do
    with :ok <- validate_ip(ip),
         path = client.base_path <> @peers_path <> "/" <> Query.encode_component(ip),
         {:ok, response} <- Client.get(client, path, [], opts) do
      decode(response, &peer_envelope/2)
    end
  end

  @doc "Raising `get_peer/3`."
  @spec get_peer!(Client.t(), String.t(), keyword()) :: PeerEnvelope.t() | NotModified.t()
  def get_peer!(client, ip, opts \\ []), do: unwrap!(get_peer(client, ip, opts))

  defp validate_ip(ip) do
    cond do
      HlPeers.IP.ipv4?(ip) ->
        :ok

      HlPeers.IP.ipv6_like?(ip) ->
        {:error, Error.invalid("ip", "the peer API is IPv4-only; #{ip} is an IPv6 literal")}

      true ->
        {:error, Error.invalid("ip", "#{inspect(ip)} is not an IPv4 address")}
    end
  end

  @doc """
  Facet counts, over the set the query's filters select.

  Takes every **filter** of the peers query. `sort`, `limit`, `cursor`,
  `fields`, `format` and `try_new_peers` are refused with a client-side
  `Invalid{parameter}`, because the endpoint does not take them and you would
  believe they applied.

  Facet names are an open map (today including `serves_mempool` and
  `auction`); `result.geo_coverage` is the server's raw geo coverage object.

  🔴 A facet's `available: false` means the attribute is **not collected**; an
  empty `values` under `available: true` means it *is* collected and nothing
  matches. Use `HlPeers.Facets.available?/2` to pre-check a filter rather than
  discover `filter-unavailable` on a `400`.
  """
  @spec facets(Client.t(), Query.t() | keyword(), keyword()) ::
          {:ok, Facets.t() | NotModified.t()} | {:error, Error.t()}
  def facets(client, query \\ %Query{}, opts \\ []) do
    query = to_query(query)

    with :ok <- Query.validate_for_facets(query),
         {:ok, response} <-
           Client.get(client, client.base_path <> @facets_path, Query.to_params(query), opts) do
      decode(response, &facets_result/2)
    end
  end

  @doc "Raising `facets/3`."
  @spec facets!(Client.t(), Query.t() | keyword(), keyword()) :: Facets.t() | NotModified.t()
  def facets!(client, query \\ %Query{}, opts \\ []), do: unwrap!(facets(client, query, opts))

  # ------------------------------------------------------------------
  # presets — §2.4
  # ------------------------------------------------------------------

  @doc """
  The tier-1 peers: `tier=1`, one step from the network's sentry nodes.

  `tier` is exact and `max_tier` is a ceiling — different questions, never
  mapped onto each other.
  """
  @spec tier1(Client.t(), keyword()) ::
          {:ok, PeerList.t() | NotModified.t()} | {:error, Error.t()}
  def tier1(client, opts \\ []), do: list_peers(client, Query.tier1(), opts)

  @doc """
  The sentries: `label=sentry`, **not** `tier=0` — equivalent today because the
  two are different questions
  that may diverge.
  """
  @spec sentries(Client.t(), keyword()) ::
          {:ok, PeerList.t() | NotModified.t()} | {:error, Error.t()}
  def sentries(client, opts \\ []), do: list_peers(client, Query.sentries(), opts)

  @doc """
  The mempool servers: `serves_mempool=true`. Owner query 1 ("serves mempool
  AND in country X") is
  `list_peers(client, Query.mempool_servers() |> Query.cc(["DE"]))`.

  🪤 Peers whose `mempool.serves` is `nil` — **unknown** — are
  **excluded**. "unknown" is not "does not serve"; ask for
  `serves_mempool: [true, nil]` to include them.
  """
  @spec mempool_servers(Client.t(), keyword()) ::
          {:ok, PeerList.t() | NotModified.t()} | {:error, Error.t()}
  def mempool_servers(client, opts \\ []), do: list_peers(client, Query.mempool_servers(), opts)

  @doc """
  Peers with room for another inbound connection: `accepting=true`, which is
  exactly `connectable=yes`.

  🪤 **This is an estimate of whether the peer has room for another inbound peer, not a guarantee; it can be wrong in both directions.**

  The geo argument is tagged — `cc:` or `region:`. 🪤 `region` is published
  **only for cloud-hosted peers** (`provider:region`, for cloud-hosted peers) and answers `400 filter-unavailable` while no geo data is loaded —
  returned as `{:error, %Error{kind: :filter_unavailable}}`, never swallowed
  into an empty list. `cc` is the broad geo key.

      HlPeers.not_full(client, cc: ["SG"])
  """
  @spec not_full(Client.t(), keyword()) ::
          {:ok, PeerList.t() | NotModified.t()} | {:error, Error.t()}
  def not_full(client, geo \\ []) do
    {opts, geo} = Keyword.split(geo, [:max_items, :on_truncate, :headers])
    list_peers(client, Query.not_full(geo), opts)
  end

  # ------------------------------------------------------------------
  # the network map — §4.8
  # ------------------------------------------------------------------

  @doc """
  `GET <base_path>/network-map/graph` — the whole published graph.

  `filters` is a keyword list over `:max_tier`, `:show_unreached`,
  `:hide_unreachable`, `:connectable`, `:cc`, `:operator`, `:label` (see
  `HlPeers.NetworkMap`); an unknown filter or a bad value is a client-side
  `Invalid`. `opts` takes `:headers`. Same ETag cache, error mapping,
  rate-limit and `api_version` exposure as the peers calls.

  Nodes decode with the `HlPeers.Peer` decoder. 🔴 `available: false` is
  returned as-is, never an error.

      {:ok, graph} = HlPeers.network_map_graph(client, max_tier: 2)
      {graph.node_count, Enum.map(graph.edges, &{&1.from, &1.to})}
  """
  @spec network_map_graph(Client.t(), keyword(), keyword()) ::
          {:ok, NetworkMap.Graph.t() | NotModified.t()} | {:error, Error.t()}
  def network_map_graph(client, filters \\ [], opts \\ []) do
    with {:ok, params} <- NetworkMap.to_params(filters),
         {:ok, response} <- Client.get(client, client.base_path <> @graph_path, params, opts) do
      decode(response, &graph_result/2)
    end
  end

  @doc "Raising `network_map_graph/3`."
  @spec network_map_graph!(Client.t(), keyword(), keyword()) ::
          NetworkMap.Graph.t() | NotModified.t()
  def network_map_graph!(client, filters \\ [], opts \\ []),
    do: unwrap!(network_map_graph(client, filters, opts))

  @doc """
  `GET <base_path>/network-map/summary` — the graph's envelope and counts
  without nodes or edges, plus zero-filled `connectable` bucket counts. Takes
  the same filters as `network_map_graph/3`.
  """
  @spec network_map_summary(Client.t(), keyword(), keyword()) ::
          {:ok, NetworkMap.Summary.t() | NotModified.t()} | {:error, Error.t()}
  def network_map_summary(client, filters \\ [], opts \\ []) do
    with {:ok, params} <- NetworkMap.to_params(filters),
         {:ok, response} <- Client.get(client, client.base_path <> @summary_path, params, opts) do
      decode(response, &summary_result/2)
    end
  end

  @doc "Raising `network_map_summary/3`."
  @spec network_map_summary!(Client.t(), keyword(), keyword()) ::
          NetworkMap.Summary.t() | NotModified.t()
  def network_map_summary!(client, filters \\ [], opts \\ []),
    do: unwrap!(network_map_summary(client, filters, opts))

  # ------------------------------------------------------------------
  # the one helper that joins the two namespaces — §7
  # ------------------------------------------------------------------

  @doc """
  Query the API and produce, or update in place, an
  `override_gossip_config.json`. Exactly one round of work.

  1. collect peers with `stream_peers/3` at `max_roots * overfetch` items
     (`:overfetch` defaults to **4**, so `exclude` / `keep_existing` / validity
     filtering still leaves enough candidates). A caller may pass a peer **list**
     instead of a query and skip the network entirely;
  2. 🔴 if the response carries `available: false`, raise `:unavailable`
     **before** touching any file — an unbuildable graph is not an empty peer
     set, and turning it into one would empty an operator's root list, which is
     the crash-loop shape (D-G);
  3. load `path` if it exists, else start from a fresh config;
  4. `merge_peers/3`;
  5. `validate/1`, collecting warnings;
  6. unless `:no_write`, `save/3`.

  The `chain` for a fresh config comes from `opts[:chain]`, else the response
  envelope's `chain`, else it is an `Invalid` error. 🔴 It is **never** defaulted
  to `Mainnet`: a `chain` that disagrees with the node's `--chain` stops the node from starting, and guessing is how that happens.

  `result.json` is always present even when nothing was written, so "render to
  stdout and let the operator place it" is a first-class path.

      {:ok, result} = HlPeers.update_gossip_config(client, query, path, max_roots: 8)
      IO.write(result.json)

  🔴 Nothing here restarts, signals or reloads a node.
  """
  @spec update_gossip_config(
          Client.t(),
          Query.t() | keyword() | [term()],
          Path.t() | nil,
          keyword()
        ) ::
          {:ok, UpdateResult.t()} | {:error, Error.t()}
  def update_gossip_config(client, query_or_peers, path \\ nil, opts \\ []) do
    max_roots = Keyword.get(opts, :max_roots, 8)
    overfetch = Keyword.get(opts, :overfetch, 4)

    with {:ok, peers, envelope} <-
           collect(client, query_or_peers, max_roots * overfetch, opts),
         {:ok, chain} <- resolve_chain(opts, envelope, path),
         {:ok, config} <- base_config(path, chain),
         config = apply_try_new_peers(config, opts),
         {:ok, merged, _merge_warnings} <-
           GossipConfig.merge_peers(config, peers, Keyword.put(opts, :max_roots, max_roots)),
         # §6.5: validate/1 already unions the merge's pending warnings with the
         # state-derived ones, de-duplicated by code.
         {:ok, warnings} <- GossipConfig.validate(merged) do
      finish_update(merged, path, warnings, envelope, opts)
    end
  end

  @doc "Raising `update_gossip_config/4`."
  @spec update_gossip_config!(Client.t(), term(), Path.t() | nil, keyword()) :: UpdateResult.t()
  def update_gossip_config!(client, query_or_peers, path \\ nil, opts \\ []),
    do: unwrap!(update_gossip_config(client, query_or_peers, path, opts))

  defp collect(_client, peers, _max_items, _opts) when is_list(peers), do: {:ok, peers, nil}

  defp collect(client, query, max_items, opts) do
    collect_pages(client, to_query(query), :start, [], max_items, opts)
  end

  defp collect_pages(client, query, cursor, acc, max_items, opts) do
    query = if cursor == :start, do: query, else: Query.put(query, :cursor, cursor)

    case list_peers(client, query, opts) do
      {:ok, %PeerList{envelope: %Envelope{available: false}} = page} ->
        # 🔴 D-G: refuse BEFORE touching any file. An unbuildable graph is not an
        # empty peer set, and turning it into one would empty an operator's root
        # list — the crash-loop shape.
        {:error,
         Error.new(:unavailable,
           detail:
             "the producer could not build a graph (available: false). That is not an empty " <>
               "peer set, and writing it as one would empty your root_node_ips — the " <>
               "crash-loop shape.",
           rate_limit: page.rate_limit
         )}

      {:ok, %PeerList{} = page} ->
        acc = acc ++ Enum.take(page.peers, max(max_items - length(acc), 0))

        if page.next_cursor && page.peers != [] && length(acc) < max_items do
          collect_pages(client, query, page.next_cursor, acc, max_items, opts)
        else
          {:ok, acc, page.envelope}
        end

      {:ok, %NotModified{}} ->
        {:error, Error.invalid("If-None-Match", "update_gossip_config needs a body, not a 304")}

      {:error, error} ->
        {:error, error}
    end
  end

  defp resolve_chain(opts, envelope, path) do
    cond do
      chain = Keyword.get(opts, :chain) ->
        {:ok, chain}

      envelope && envelope.chain ->
        {:ok, envelope.chain}

      path && File.exists?(path) ->
        {:ok, nil}

      true ->
        {:error, Error.invalid("chain", "no chain in opts and none in the response envelope")}
    end
  end

  # `try_new_peers` is left exactly as it was unless the caller says otherwise.
  defp apply_try_new_peers(config, opts) do
    case Keyword.fetch(opts, :try_new_peers) do
      {:ok, value} -> GossipConfig.set_try_new_peers(config, value)
      :error -> config
    end
  end

  defp base_config(nil, chain), do: {:ok, GossipConfig.new(chain, [])}

  defp base_config(path, chain) do
    case GossipConfig.load(path) do
      {:ok, config} -> {:ok, config}
      {:error, %Error{kind: :not_found}} -> {:ok, GossipConfig.new(chain, [])}
      other -> other
    end
  end

  defp finish_update(config, path, warnings, envelope, opts) do
    json = GossipConfig.to_json(config)

    if path && not Keyword.get(opts, :no_write, false) do
      with {:ok, save} <- GossipConfig.save(config, path, opts) do
        {:ok,
         %UpdateResult{
           config: config,
           json: json,
           changed: save.changed,
           diff: save.diff,
           written_path: if(save.written, do: path),
           warnings: warnings,
           envelope: envelope
         }}
      end
    else
      existing = if path, do: File.read(path), else: :error
      changed = existing != {:ok, json}

      {:ok,
       %UpdateResult{
         config: config,
         json: json,
         changed: changed,
         diff: diff_for(existing, json, path),
         warnings: warnings,
         envelope: envelope
       }}
    end
  end

  defp diff_for({:ok, existing}, json, path), do: HlPeers.Diff.unified(existing, json, path)

  defp diff_for(_absent, json, path) when is_binary(path),
    do: HlPeers.Diff.unified("", json, path)

  defp diff_for(_absent, _json, _path), do: ""

  # ------------------------------------------------------------------
  # decoding
  # ------------------------------------------------------------------

  defp to_query(%Query{} = query), do: query
  defp to_query(opts) when is_list(opts) or is_map(opts), do: Query.new(opts)

  defp decode(%{not_modified: true} = response, _builder) do
    {:ok,
     %NotModified{
       etag: response.etag,
       rate_limit: response.rate_limit,
       api_version: response.api_version,
       status: 304
     }}
  end

  defp decode(response, builder) do
    case Jason.decode(response.body) do
      {:ok, body} when is_map(body) ->
        {:ok, builder.(body, response)}

      {:ok, other} ->
        {:error, decode_error(response, {:unexpected_body, other})}

      {:error, cause} ->
        {:error, decode_error(response, cause)}
    end
  end

  defp decode_error(response, cause) do
    Error.new(:decode,
      cause: cause,
      status: response.status,
      rate_limit: response.rate_limit,
      api_version: response.api_version,
      body_excerpt: String.slice(response.body, 0, 512)
    )
  end

  defp peer_list(body, response) do
    %PeerList{
      peers: Enum.map(body["peers"] || [], &Peer.from_map/1),
      count: body["count"],
      total: body["total"],
      next_cursor: body["next_cursor"],
      envelope: Envelope.from_body(body),
      rate_limit: response.rate_limit,
      api_version: response.api_version,
      etag: response.etag,
      from_cache: response.from_cache,
      status: response.status,
      body: body
    }
  end

  defp peer_envelope(body, response) do
    %PeerEnvelope{
      peer: body["peer"] && Peer.from_map(body["peer"]),
      envelope: Envelope.from_body(body),
      rate_limit: response.rate_limit,
      api_version: response.api_version,
      etag: response.etag,
      from_cache: response.from_cache,
      status: response.status,
      body: body
    }
  end

  defp facets_result(body, response) do
    %Facets{
      total: body["total"],
      facets: Map.new(body["facets"] || %{}, fn {k, v} -> {k, Facet.from_map(v)} end),
      geo_coverage: body["geo_coverage"],
      envelope: Envelope.from_body(body),
      rate_limit: response.rate_limit,
      api_version: response.api_version,
      etag: response.etag,
      from_cache: response.from_cache,
      status: response.status,
      body: body
    }
  end

  defp graph_result(body, response) do
    %NetworkMap.Graph{
      version: body["version"],
      available: body["available"],
      stale: body["stale"],
      generated_at: body["generated_at"],
      operator_nodes_count: operator_nodes_count(body),
      node_count: body["node_count"],
      edge_count: body["edge_count"],
      tier_count: body["tier_count"],
      unreached: body["unreached"],
      truncated: body["truncated"],
      tiers: tiers(body),
      nodes: Enum.map(body["nodes"] || [], &Peer.from_map/1),
      edges: Enum.map(body["edges"] || [], &NetworkMap.Edge.from_map/1),
      cities: cities(body),
      attribution: body["attribution"],
      rate_limit: response.rate_limit,
      api_version: response.api_version,
      etag: response.etag,
      from_cache: response.from_cache,
      status: response.status,
      body: body
    }
  end

  defp summary_result(body, response) do
    %NetworkMap.Summary{
      version: body["version"],
      available: body["available"],
      stale: body["stale"],
      generated_at: body["generated_at"],
      operator_nodes_count: operator_nodes_count(body),
      node_count: body["node_count"],
      edge_count: body["edge_count"],
      tier_count: body["tier_count"],
      unreached: body["unreached"],
      tiers: tiers(body),
      connectable: body["connectable"] || %{},
      serves_mempool: body["serves_mempool"] || %{},
      auction: body["auction"] || %{},
      cities: cities(body),
      attribution: body["attribution"],
      rate_limit: response.rate_limit,
      api_version: response.api_version,
      etag: response.etag,
      from_cache: response.from_cache,
      status: response.status,
      body: body
    }
  end

  # Absent `cities` stays `nil` (no city positions served), distinct from `[]`.
  defp cities(%{"cities" => list}) when is_list(list),
    do: Enum.map(list, &NetworkMap.CityCluster.from_map/1)

  defp cities(_body), do: nil

  defp operator_nodes_count(%{"operator_nodes" => %{"count" => count}}), do: count
  defp operator_nodes_count(_body), do: nil

  defp tiers(body), do: Enum.map(body["tiers"] || [], &%{tier: &1["tier"], count: &1["count"]})

  defp unwrap!({:ok, value}), do: value
  defp unwrap!({:error, %Error{} = error}), do: raise(error)
end

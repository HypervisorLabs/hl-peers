defmodule HlPeers.NetworkMap do
  @moduledoc """
  The two `/network-map/*` endpoints (`CLIENT_SPEC.md` §4.8): the parameter
  set, its validation and serialization. Call them through
  `HlPeers.network_map_graph/3` and `HlPeers.network_map_summary/3`.

  | filter | type | wire |
  |---|---|---|
  | `:max_tier` | integer ≥ 0 | as-is |
  | `:show_unreached` | boolean | `true` / `false` |
  | `:hide_unreachable` | boolean | `true` / `false` |
  | `:connectable` | list over `"yes"`, `"no"`, `"unknown"` (closed) | CSV |
  | `:cc` | string or list | as-is / CSV |
  | `:operator` | string or list | as-is / CSV |
  | `:label` | string or list | as-is / CSV |
  | `:serves_mempool` | list over `true`, `false`, `nil` (closed), or CSV | CSV, `nil` as `null` |
  | `:auction` | list over `"holds_slot"`, `"bidding"`, `"bid_before"`, `nil` (closed), or CSV | CSV, `nil` as `null` |
  | `:network` | `"mainnet"` or `"testnet"` (or the atoms; closed, case-sensitive) | as-is |

  `:serves_mempool`, `:auction` and `:network` are validated and serialized by
  `HlPeers.Query`, exactly like the `/peers` builder fields of the same name.
  `:network` (**Testnet, beta**) picks which network's graph is drawn; unset,
  it falls back to the client's `:network` default, and with neither nothing
  is sent (mainnet).

  Unset filters are omitted; parameters are lexicographic and percent-encoded
  exactly like the peers query (§2.3).
  """

  alias HlPeers.Error

  @filters ~w(auction cc connectable hide_unreachable label max_tier network operator
               serves_mempool show_unreached)a
  @connectable ~w(yes no unknown)

  @doc "The filter names the network-map endpoints take."
  @spec filters() :: [atom()]
  def filters, do: @filters

  @doc """
  Validates a keyword list of filters and returns the canonical wire params.

      iex> HlPeers.NetworkMap.to_params(max_tier: 2, connectable: ["yes", "no"])
      {:ok, [{"connectable", "yes,no"}, {"max_tier", "2"}]}

      iex> {:error, e} = HlPeers.NetworkMap.to_params(max_tier: -1)
      iex> {e.kind, e.parameter}
      {:invalid, "max_tier"}
  """
  @spec to_params(keyword() | map()) :: {:ok, [{String.t(), String.t()}]} | {:error, Error.t()}
  def to_params(filters) do
    filters
    |> Enum.reject(fn {_k, v} -> v in [nil, []] end)
    |> Enum.reduce_while({:ok, []}, fn {key, value}, {:ok, acc} ->
      case param(key, value) do
        {:ok, pair} -> {:cont, {:ok, [pair | acc]}}
        {:error, _} = error -> {:halt, error}
      end
    end)
    |> case do
      {:ok, params} -> {:ok, Enum.sort_by(params, &elem(&1, 0))}
      error -> error
    end
  end

  defp param(key, value) when is_binary(key) do
    case Enum.find(@filters, &(Atom.to_string(&1) == key)) do
      nil -> unknown(key)
      atom -> param(atom, value)
    end
  end

  defp param(:max_tier, n) when is_integer(n) and n >= 0, do: {:ok, {"max_tier", to_string(n)}}

  defp param(:max_tier, other),
    do: {:error, Error.invalid("max_tier", "#{inspect(other)} is not an integer >= 0")}

  defp param(key, value) when key in [:show_unreached, :hide_unreachable] and is_boolean(value),
    do: {:ok, {Atom.to_string(key), to_string(value)}}

  defp param(key, other) when key in [:show_unreached, :hide_unreachable],
    do: {:error, Error.invalid(Atom.to_string(key), "#{inspect(other)} is not a boolean")}

  defp param(:connectable, value) do
    members = members(value)

    case Enum.reject(members, &(&1 in @connectable)) do
      [] ->
        {:ok, {"connectable", Enum.join(members, ",")}}

      bad ->
        {:error,
         Error.new(:invalid,
           parameter: "connectable",
           reason: "#{inspect(bad)} is not one of #{inspect(@connectable)}",
           allowed: @connectable
         )}
    end
  end

  defp param(key, value) when key in [:serves_mempool, :auction, :network] do
    query = HlPeers.Query.new([{key, value}])

    with :ok <- HlPeers.Query.validate(query) do
      case HlPeers.Query.to_params(query) do
        [pair] -> {:ok, pair}
      end
    end
  end

  defp param(key, value) when key in [:cc, :operator, :label],
    do: {:ok, {Atom.to_string(key), value |> members() |> Enum.join(",")}}

  defp param(key, _value), do: unknown(key)

  defp unknown(key) do
    {:error,
     Error.invalid(
       to_string(key),
       "not a network-map filter; known: #{Enum.map_join(@filters, ", ", &Atom.to_string/1)}"
     )}
  end

  defp members(value) when is_list(value), do: Enum.map(value, &to_string/1)
  defp members(value) when is_binary(value), do: String.split(value, ",")
  defp members(value), do: [to_string(value)]
end

defmodule HlPeers.NetworkMap.Edge do
  @moduledoc """
  One directed edge of the published graph. `span` is `tier(to) - tier(from)`,
  or `nil` when either end is unplaced.
  """

  @type t :: %__MODULE__{from: String.t(), to: String.t(), span: integer() | nil}

  defstruct [:from, :to, :span]

  @doc "The wire keys this struct models."
  @spec known_fields() :: [String.t()]
  def known_fields, do: ~w(from span to)

  @doc false
  @spec from_map(map()) :: t()
  def from_map(map), do: %__MODULE__{from: map["from"], to: map["to"], span: map["span"]}
end

defmodule HlPeers.NetworkMap.CityCluster do
  @moduledoc """
  One server-side city cluster (`cities` on the network map). `lat` / `lon`
  are the city's own point rounded to 0.1° — the only coordinates in the API.
  `tiers` maps a tier (as a string; `"null"` = unreached) to a peer count.
  """

  @type t :: %__MODULE__{
          id: String.t() | nil,
          name: String.t() | nil,
          cc: String.t() | nil,
          lat: number() | nil,
          lon: number() | nil,
          peers: integer() | nil,
          tiers: %{optional(String.t()) => integer()}
        }

  defstruct [:id, :name, :cc, :lat, :lon, :peers, tiers: %{}]

  @doc "The wire keys this struct models."
  @spec known_fields() :: [String.t()]
  def known_fields, do: ~w(cc id lat lon name peers tiers)

  @doc false
  @spec from_map(map()) :: t()
  def from_map(map) do
    %__MODULE__{
      id: map["id"],
      name: map["name"],
      cc: map["cc"],
      lat: map["lat"],
      lon: map["lon"],
      peers: map["peers"],
      tiers: map["tiers"] || %{}
    }
  end
end

defmodule HlPeers.NetworkMap.Graph do
  @moduledoc """
  The result of `HlPeers.network_map_graph/3`: the whole published graph.

  `nodes` are decoded with the same decoder as a `/peers` row
  (`HlPeers.Peer`), `edges` are `HlPeers.NetworkMap.Edge`, and `tiers` is
  the histogram as `%{tier: integer, count: integer}` maps.
  `operator_nodes_count` is `nil` when the producer publishes its own nodes as
  ordinary rows.

  `cities` (`HlPeers.NetworkMap.CityCluster`) and `attribution` are `nil`
  when the producer serves no city positions — `nil` is not `[]`. With cities,
  each node's `city` is its cluster id.

  `network` is `"testnet"` on a `network: "testnet"` graph (beta) and `nil`
  otherwise (mainnet); each testnet node's `version` is its hardfork version.

  🔴 `available: false` is returned as-is — the producer could not build a
  graph. It is not an error and not an empty network.
  """

  @type tier :: %{tier: integer(), count: integer()}

  @type t :: %__MODULE__{
          version: String.t() | nil,
          available: boolean() | nil,
          stale: boolean() | nil,
          network: String.t() | nil,
          generated_at: String.t() | nil,
          operator_nodes_count: integer() | nil,
          node_count: integer() | nil,
          edge_count: integer() | nil,
          tier_count: integer() | nil,
          unreached: integer() | nil,
          truncated: boolean() | nil,
          tiers: [tier()],
          nodes: [HlPeers.Peer.t()],
          edges: [HlPeers.NetworkMap.Edge.t()],
          cities: [HlPeers.NetworkMap.CityCluster.t()] | nil,
          attribution: String.t() | nil,
          rate_limit: HlPeers.RateLimit.t(),
          api_version: String.t() | nil,
          etag: String.t() | nil,
          from_cache: boolean(),
          status: non_neg_integer(),
          body: map()
        }

  defstruct [
    :version,
    :available,
    :stale,
    :network,
    :generated_at,
    :operator_nodes_count,
    :node_count,
    :edge_count,
    :tier_count,
    :unreached,
    :truncated,
    :cities,
    :attribution,
    :rate_limit,
    :api_version,
    :etag,
    tiers: [],
    nodes: [],
    edges: [],
    from_cache: false,
    status: 200,
    body: %{}
  ]
end

defmodule HlPeers.NetworkMap.Summary do
  @moduledoc """
  The result of `HlPeers.network_map_summary/3`: the graph's envelope and
  counts without `nodes`, `edges` or `truncated`, plus `connectable` — a
  zero-filled `%{bucket => count}` map over `yes` / `no` / `unknown` (an open
  map; any other bucket decodes too), `serves_mempool` (`true` / `false` /
  `null`) and `auction` (`holds_slot` / `bidding` / `bid_before` / `null`)
  count maps (empty when absent), and `cities` / `attribution` as on the graph
  (served live though upstream's summary schema omits them, CLIENT_SPEC §10 D-S).
  `network` is `"testnet"` on a testnet summary (beta), else `nil`.

  🔴 `available: false` is returned as-is, never an error.
  """

  @type t :: %__MODULE__{
          version: String.t() | nil,
          available: boolean() | nil,
          stale: boolean() | nil,
          network: String.t() | nil,
          generated_at: String.t() | nil,
          operator_nodes_count: integer() | nil,
          node_count: integer() | nil,
          edge_count: integer() | nil,
          tier_count: integer() | nil,
          unreached: integer() | nil,
          tiers: [HlPeers.NetworkMap.Graph.tier()],
          connectable: %{optional(String.t()) => integer()},
          serves_mempool: %{optional(String.t()) => integer()},
          auction: %{optional(String.t()) => integer()},
          cities: [HlPeers.NetworkMap.CityCluster.t()] | nil,
          attribution: String.t() | nil,
          rate_limit: HlPeers.RateLimit.t(),
          api_version: String.t() | nil,
          etag: String.t() | nil,
          from_cache: boolean(),
          status: non_neg_integer(),
          body: map()
        }

  defstruct [
    :version,
    :available,
    :stale,
    :network,
    :generated_at,
    :operator_nodes_count,
    :node_count,
    :edge_count,
    :tier_count,
    :unreached,
    :cities,
    :attribution,
    :rate_limit,
    :api_version,
    :etag,
    tiers: [],
    connectable: %{},
    serves_mempool: %{},
    auction: %{},
    from_cache: false,
    status: 200,
    body: %{}
  ]
end

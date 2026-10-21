defmodule HlPeers.Query do
  @moduledoc """
  A typed, immutable, chainable query builder over exactly the parameters of
  `GET <base_path>/peers` (`CLIENT_SPEC.md` §2).

  Every setter returns a new query; nothing mutates. CSV parameters take a
  **list** — you never build CSV by hand — but a pre-joined string is accepted
  too.

      iex> alias HlPeers.Query
      iex> Query.new() |> Query.cc(["de", "nl"]) |> Query.tier([1]) |> Query.to_query_string()
      "cc=DE%2CNL&tier=1"

  ## `tier` is not `max_tier`

  `tier` is **exact**; `max_tier` is a **ceiling**. They are different questions
  and this client never maps one to the other. Both may be set at once; the
  server ANDs them. The literal `nil` is a selectable `tier` member and
  serializes as the string `null` — it means *this node was never placed in the peer graph*,
  which is NOT tier 0.

  ## Validation is deliberately thin

  Only the five rules of §2.2 are checked locally. `cc`, `region`, `asn`,
  `role`, `label`, `version` and `fields` are **open** sets that may grow
  additively within v1; validating them here would turn a server `400` that
  names the problem into a client-side guess. `limit` is not clamped locally
  either — the server clamps, and clamping here would hide the tier ceiling from
  you.

  ## Mempool and auction

  `serves_mempool` takes a list over `true`, `false` and `nil` (serialized
  `true` / `false` / `null`); `auction` a list over `"holds_slot"`, `"bidding"`,
  `"bid_before"` and `nil` (`null`). Both are **closed** enums, checked locally.
  🪤 `nil` is its own answer — *unknown* / *never seen in the auction* —
  and is **not** `false`:

      iex> alias HlPeers.Query
      iex> Query.new(serves_mempool: [true, nil], auction: ["holds_slot", "bidding"])
      ...> |> Query.to_query_string()
      "auction=holds_slot%2Cbidding&serves_mempool=true%2Cnull"

  ## `country`

  `country` is the server-side alias of `cc` (both given ⇒ union). It is a
  separate field so the URL says what you wrote; it is upper-cased like `cc`.

  ## `try_new_peers`

  Only meaningful with `format=gossip_config`, i.e. with
  `HlPeers.list_peers_as_gossip_config/3`.

  ## `sort`

  No `sort` is sent unless you set one (D-J), so the server's default governs.
  """

  alias HlPeers.Error

  @csv_fields ~w(cc region asn tier role label country connectable state serves_mempool
                 auction version fields)a
  @scalar_fields ~w(chain max_tier accepting max_blocks_behind min_uptime
                    max_latency_ms vantage seen_within sort limit cursor format
                    try_new_peers)a
  @fields @csv_fields ++ @scalar_fields

  @enums %{
    chain: ~w(Mainnet Testnet),
    sort: ~w(tier blocks_behind out_deg last_seen random),
    format: ~w(json gossip_config),
    connectable: ~w(yes no unknown),
    state: ~w(responsive open full syncing listening unreachable unknown),
    serves_mempool: ~w(true false null),
    auction: ~w(holds_slot bidding bid_before null)
  }

  # §4.4: the facets endpoint takes every *filter*; these are not filters.
  @not_facet_params ~w(sort limit cursor fields format try_new_peers)a

  # Members that serialize the literal `nil` as the string "null".
  @nullable_csv ~w(tier serves_mempool auction)a

  @type t :: %__MODULE__{}

  defstruct @fields

  @doc "The parameter names this builder knows."
  @spec fields() :: [atom()]
  def fields, do: @fields

  @doc """
  Builds a query from a keyword list or map.

      iex> HlPeers.Query.new(tier: [1], label: ["sentry"]) |> HlPeers.Query.to_query_string()
      "label=sentry&tier=1"
  """
  @spec new(Enumerable.t()) :: t()
  def new(opts \\ []) do
    Enum.reduce(opts, %__MODULE__{}, fn {k, v}, q -> put(q, k, v) end)
  end

  @doc "Sets one field, returning a new query. Unknown fields raise `ArgumentError`."
  @spec put(t(), atom(), term()) :: t()
  def put(%__MODULE__{} = query, field, value) when field in @fields do
    Map.put(query, field, value)
  end

  def put(%__MODULE__{}, field, _value) do
    raise ArgumentError,
          "#{inspect(field)} is not a peer query parameter. Known: #{inspect(@fields)}"
  end

  @doc "Merges a keyword list or map of fields, returning a new query."
  @spec merge(t(), Enumerable.t()) :: t()
  def merge(%__MODULE__{} = query, opts) do
    Enum.reduce(opts, query, fn {k, v}, q -> put(q, k, v) end)
  end

  @field_docs %{
    region: """
    Sets `region` (a list, CSV on the wire).

    🪤 `region` is published **only for cloud-hosted peers** (`provider:region`,
    for cloud-hosted peers) and the server answers
    `400 filter-unavailable` while no geo data is loaded; that surfaces as
    `%HlPeers.Error{kind: :filter_unavailable}`, never as an empty list. `cc`
    is the broad geo key.
    """,
    cc: "Sets `cc` (ISO country codes; upper-cased on the wire). The broad geo key.",
    country: "Sets `country`, the server-side alias of `cc` (both given ⇒ union).",
    serves_mempool: """
    Sets `serves_mempool`: a list over `true`, `false`, `nil` (closed enum).

    🪤 `nil` means **unknown** (unknown) and is not `false`.
    """,
    auction: """
    Sets `auction`: a list over `"holds_slot"`, `"bidding"`, `"bid_before"`,
    `nil` (closed enum). `nil` means never seen in the auction.
    """,
    try_new_peers: "Sets `try_new_peers`; only meaningful with `format=gossip_config`.",
    state:
      "Sets `state`: a list over `responsive`, `open`, `full`, `syncing`, " <>
        "`listening`, `unreachable`, `unknown` (closed enum). `listening` = the " <>
        "peer accepts inbound connections; other details unknown."
  }

  for field <- @fields do
    @doc Map.get(@field_docs, field, "Sets `#{field}`.")
    @spec unquote(field)(t(), term()) :: t()
    def unquote(field)(%__MODULE__{} = query, value), do: put(query, unquote(field), value)
  end

  # ------------------------------------------------------------------
  # validation — §2.2
  # ------------------------------------------------------------------

  @doc """
  Checks the five rules a client is allowed to enforce locally, and nothing else.

      iex> HlPeers.Query.validate(HlPeers.Query.new(seen_within: "1h"))
      :ok

      iex> {:error, e} = HlPeers.Query.validate(HlPeers.Query.new(seen_within: "1 hour"))
      iex> {e.kind, e.parameter}
      {:invalid, "seen_within"}

      iex> {:error, e} = HlPeers.Query.validate(HlPeers.Query.new(max_latency_ms: 80))
      iex> e.parameter
      "vantage"
  """
  @spec validate(t()) :: :ok | {:error, Error.t()}
  def validate(%__MODULE__{} = query) do
    with :ok <- validate_seen_within(query.seen_within),
         :ok <- validate_min_uptime(query.min_uptime),
         :ok <- validate_non_negative(query),
         :ok <- validate_limit(query.limit),
         :ok <- validate_vantage(query),
         do: validate_enums(query)
  end

  defp validate_seen_within(nil), do: :ok

  defp validate_seen_within(value) when is_binary(value) do
    if Regex.match?(~r/^[0-9]+[mhd]$/, value) do
      :ok
    else
      {:error,
       Error.invalid("seen_within", "#{value} does not match ^[0-9]+[mhd]$ (e.g. \"1h\")")}
    end
  end

  defp validate_seen_within(other),
    do: {:error, Error.invalid("seen_within", "#{inspect(other)} is not a duration string")}

  defp validate_min_uptime(nil), do: :ok

  defp validate_min_uptime(value) when is_number(value) and value >= 0 and value <= 1, do: :ok

  defp validate_min_uptime(other),
    do: {:error, Error.invalid("min_uptime", "#{inspect(other)} is outside [0, 1]")}

  defp validate_non_negative(query) do
    Enum.find_value(
      [:max_tier, :max_blocks_behind, :max_latency_ms],
      :ok,
      fn field ->
        case Map.fetch!(query, field) do
          nil -> nil
          n when is_integer(n) and n >= 0 -> nil
          other -> {:error, Error.invalid(to_string(field), "#{inspect(other)} is negative")}
        end
      end
    )
  end

  defp validate_limit(nil), do: :ok
  defp validate_limit(n) when is_integer(n) and n >= 1, do: :ok

  defp validate_limit(other),
    do: {:error, Error.invalid("limit", "#{inspect(other)} is below 1")}

  defp validate_vantage(%{max_latency_ms: nil}), do: :ok
  defp validate_vantage(%{vantage: v}) when is_binary(v), do: :ok

  defp validate_vantage(_),
    do:
      {:error,
       Error.invalid("vantage", "max_latency_ms is only meaningful with a vantage; set both")}

  defp validate_enums(query) do
    Enum.find_value(@enums, :ok, fn {field, allowed} ->
      check_enum(field, Map.fetch!(query, field), allowed)
    end)
  end

  defp check_enum(_field, nil, _allowed), do: nil

  defp check_enum(field, value, allowed) do
    case value |> csv_members() |> Enum.reject(&(&1 in allowed)) do
      [] ->
        nil

      bad ->
        {:error,
         Error.new(:invalid,
           parameter: to_string(field),
           reason: "#{inspect(bad)} is not one of #{inspect(allowed)}",
           allowed: allowed
         )}
    end
  end

  defp csv_members(value) when is_list(value), do: Enum.map(value, &member_string/1)
  defp csv_members(value) when is_binary(value), do: String.split(value, ",")
  defp csv_members(value), do: [to_string(value)]

  defp member_string(nil), do: "null"
  defp member_string(member), do: to_string(member)

  # ------------------------------------------------------------------
  # serialization — §2.3
  # ------------------------------------------------------------------

  @doc """
  The wire parameters as a name-sorted keyword list. Unset values and empty
  lists are omitted entirely (an empty list is *not* `?cc=`).

      iex> HlPeers.Query.to_params(HlPeers.Query.new(tier: [1, nil], cc: []))
      [{"tier", "1,null"}]
  """
  @spec to_params(t()) :: [{String.t(), String.t()}]
  def to_params(%__MODULE__{} = query) do
    @fields
    |> Enum.flat_map(fn field ->
      case encode(field, Map.fetch!(query, field)) do
        nil -> []
        value -> [{to_string(field), value}]
      end
    end)
    |> Enum.sort_by(&elem(&1, 0))
  end

  @doc """
  The canonical query string: parameters lexicographic by name, values
  percent-encoded per RFC 3986 — the CSV `,` **is** encoded (`%2C`), no special
  case. Two clients issuing the same query produce the same URL and so the same
  CDN cache key.
  """
  @spec to_query_string(t()) :: String.t()
  def to_query_string(%__MODULE__{} = query) do
    query
    |> to_params()
    |> Enum.map_join("&", fn {k, v} -> encode_component(k) <> "=" <> encode_component(v) end)
  end

  @doc false
  @spec encode_component(String.t()) :: String.t()
  def encode_component(value), do: URI.encode(value, &URI.char_unreserved?/1)

  defp encode(_field, nil), do: nil
  defp encode(_field, []), do: nil

  defp encode(field, values) when field in @csv_fields and is_list(values) do
    values |> Enum.map_join(",", &csv_member(field, &1))
  end

  defp encode(field, value) when field in [:cc, :country] and is_binary(value),
    do: String.upcase(value)

  defp encode(_field, value) when is_binary(value), do: value
  defp encode(_field, value) when is_boolean(value), do: to_string(value)
  defp encode(_field, value) when is_integer(value), do: Integer.to_string(value)
  # 🪤 Shortest round-trip, with no trailing `.0`: `1` and `1.0` are the same
  # bound to the server but two different CDN cache keys, so all four clients
  # spell an integral value without a fractional part (CLIENT_SPEC §2.3).
  defp encode(_field, value) when is_float(value) do
    if value == Float.round(value) and abs(value) < 1.0e15,
      do: value |> trunc() |> Integer.to_string(),
      else: Float.to_string(value)
  end

  defp encode(_field, value) when is_atom(value), do: Atom.to_string(value)

  # `tier: [1, nil]`, `serves_mempool: [nil]`, `auction: [nil]` — the absent
  # member serializes as the string "null".
  defp csv_member(field, nil) when field in @nullable_csv, do: "null"

  defp csv_member(field, member) when field in [:cc, :country],
    do: member |> to_string() |> String.upcase()

  defp csv_member(_field, member), do: to_string(member)

  # ------------------------------------------------------------------
  # facets — §4.4
  # ------------------------------------------------------------------

  @doc """
  `GET <base_path>/peers/facets` takes every **filter** of the peers query (the
  counts are over the filtered set). `sort`, `limit`, `cursor`, `fields`,
  `format` and `try_new_peers` are not filters: passing one is a client-side
  `Invalid`, because the endpoint does not take it and you would believe it
  applied.
  """
  @spec validate_for_facets(t()) :: :ok | {:error, Error.t()}
  def validate_for_facets(%__MODULE__{} = query) do
    case Enum.filter(@not_facet_params, &(Map.fetch!(query, &1) not in [nil, []])) do
      [] ->
        validate(query)

      [field | _] ->
        {:error,
         Error.invalid(
           to_string(field),
           "the facets endpoint does not take #{field}; it would be ignored, leaving you " <>
             "to believe it applied"
         )}
    end
  end

  # ------------------------------------------------------------------
  # presets — §2.4
  # ------------------------------------------------------------------

  @doc """
  `tier: [1]` — the peers one step from the network's sentry nodes.

      iex> HlPeers.Query.tier1() |> HlPeers.Query.to_query_string()
      "tier=1"
  """
  @spec tier1() :: t()
  def tier1, do: new(tier: [1])

  @doc """
  `label: ["sentry"]`.

  This uses `label=sentry`, **not** `tier=0`. They are equivalent today because
  they are
  different questions and may diverge.

      iex> HlPeers.Query.sentries() |> HlPeers.Query.to_query_string()
      "label=sentry"
  """
  @spec sentries() :: t()
  def sentries, do: new(label: ["sentry"])

  @doc """
  `accepting: true` plus a tagged geo argument — the peers with room for you.

  🪤 This is exactly `connectable=yes`, which is an **estimate of whether the peer has room for another inbound peer, not a guarantee; it can be wrong in both directions.**

  The geo argument is tagged — `cc:` or `region:` — never a bare string.
  🪤 `region` is published **only for cloud-hosted peers** (`provider:region`,
  e.g. `"aws:eu-central-1"`, for cloud-hosted peers) and
  answers `400 filter-unavailable` while no geo data is loaded — that error is
  returned, never swallowed into an empty list. `cc` is the broad geo key.

      iex> HlPeers.Query.not_full(cc: ["SG"]) |> HlPeers.Query.to_query_string()
      "accepting=true&cc=SG"
  """
  @spec not_full(keyword()) :: t()
  def not_full(geo \\ []), do: geo |> geo_query() |> put(:accepting, true)

  defp geo_query(geo) do
    Enum.reduce(geo, %__MODULE__{}, fn
      {:cc, value}, q ->
        put(q, :cc, List.wrap(value))

      {:region, value}, q ->
        put(q, :region, List.wrap(value))

      {key, _}, _q ->
        raise ArgumentError, "geo argument must be cc: or region:, got #{inspect(key)}"
    end)
  end

  @doc """
  `serves_mempool: [true]` — the peers that serve the mempool.

  Owner query 1, "serves mempool AND in country X", is
  `mempool_servers() |> cc(["DE"])`.

  🪤 Peers with `mempool.serves == nil` — **unknown** — are **excluded**.
  "unknown" is not "does not serve": to include the unknowns, ask for
  `serves_mempool: [true, nil]`.

      iex> HlPeers.Query.mempool_servers() |> HlPeers.Query.cc(["DE"])
      ...> |> HlPeers.Query.to_query_string()
      "cc=DE&serves_mempool=true"
  """
  @spec mempool_servers() :: t()
  def mempool_servers, do: new(serves_mempool: [true])
end

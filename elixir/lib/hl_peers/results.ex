defmodule HlPeers.PeerList do
  @moduledoc """
  One page of `GET <base_path>/peers`.

  Everything about the response is exposed (`CLIENT_SPEC.md` §5.3): the decoded
  `:peers`, the `:envelope`, the `:rate_limit` block, the `:api_version`
  (`API-Version` header), the `:etag`, whether the body came `:from_cache`, and
  the HTTP `:status` (200 or 304).

  🔴 `envelope.available == false` is **not** an error and **not** "no peers
  matched". Check it before you conclude anything from an empty list.
  """

  @type t :: %__MODULE__{
          peers: [HlPeers.Peer.t()],
          count: integer() | nil,
          total: integer() | nil,
          next_cursor: String.t() | nil,
          envelope: HlPeers.Envelope.t(),
          rate_limit: HlPeers.RateLimit.t(),
          api_version: String.t() | nil,
          etag: String.t() | nil,
          from_cache: boolean(),
          status: non_neg_integer(),
          body: map()
        }

  defstruct peers: [],
            count: nil,
            total: nil,
            next_cursor: nil,
            envelope: nil,
            rate_limit: nil,
            api_version: nil,
            etag: nil,
            from_cache: false,
            status: 200,
            body: %{}

  @doc "Shorthand for `result.envelope.available`."
  @spec available?(t()) :: boolean()
  def available?(%__MODULE__{envelope: %{available: available}}), do: available == true
end

defmodule HlPeers.PeerEnvelope do
  @moduledoc """
  The result of `HlPeers.get_peer/2`: one `:peer` plus the standing envelope
  and the same response metadata as `HlPeers.PeerList`.
  """

  @type t :: %__MODULE__{
          peer: HlPeers.Peer.t() | nil,
          envelope: HlPeers.Envelope.t(),
          rate_limit: HlPeers.RateLimit.t(),
          api_version: String.t() | nil,
          etag: String.t() | nil,
          from_cache: boolean(),
          status: non_neg_integer(),
          body: map()
        }

  defstruct [
    :peer,
    :envelope,
    :rate_limit,
    :api_version,
    :etag,
    :body,
    from_cache: false,
    status: 200
  ]
end

defmodule HlPeers.Facet do
  @moduledoc """
  One facet.

  🔴 `available: false` means the attribute behind the filter is **not
  collected**. An empty `values` under `available: true` means it *is* collected
  and nothing currently matches. The two are different answers and this client
  keeps them distinguishable — there is no accessor that flattens a facet to a
  plain list of values without also handing you `available`.

  `value` is always a string, including the selectable absent-value member,
  which is the literal `"null"`.
  """

  @type t :: %__MODULE__{available: boolean(), values: [%{value: String.t(), count: integer()}]}

  defstruct available: false, values: []

  @doc false
  @spec from_map(map()) :: t()
  def from_map(map) do
    %__MODULE__{
      available: map["available"] == true,
      values: Enum.map(map["values"] || [], &%{value: &1["value"], count: &1["count"]})
    }
  end
end

defmodule HlPeers.Facets do
  @moduledoc """
  The result of `HlPeers.facets/3`: `{envelope, total, facets, geo_coverage}`
  where each facet is a `HlPeers.Facet`.

  Facet names are an **open** map — today `tier`, `cc`, `region`, `asn`,
  `role`, `label`, `state`, `connectable`, `version`, `uptime`,
  `serves_mempool` and `auction`; any other name the server adds decodes too.
  `geo_coverage` is the server's raw geo-enrichment coverage object (or `nil`),
  kept as a plain map because its shape is informational and may grow.

  `available?/2` lets you pre-check a filter rather than discover
  `filter-unavailable` on a `400`:

      HlPeers.Facets.available?(facets, "serves_mempool")
  """

  @type t :: %__MODULE__{
          total: integer() | nil,
          facets: %{optional(String.t()) => HlPeers.Facet.t()},
          geo_coverage: map() | nil,
          envelope: HlPeers.Envelope.t(),
          rate_limit: HlPeers.RateLimit.t(),
          api_version: String.t() | nil,
          etag: String.t() | nil,
          from_cache: boolean(),
          status: non_neg_integer(),
          body: map()
        }

  defstruct total: nil,
            facets: %{},
            geo_coverage: nil,
            envelope: nil,
            rate_limit: nil,
            api_version: nil,
            etag: nil,
            from_cache: false,
            status: 200,
            body: %{}

  @doc """
  Whether the named attribute is collected at all.

      iex> facets = %HlPeers.Facets{facets: %{"asn" => %HlPeers.Facet{available: false}}}
      iex> {HlPeers.Facets.available?(facets, "asn"), HlPeers.Facets.available?(facets, "cc")}
      {false, false}
  """
  @spec available?(t(), String.t()) :: boolean()
  def available?(%__MODULE__{facets: facets}, name) do
    match?(%HlPeers.Facet{available: true}, Map.get(facets, name))
  end

  @doc """
  The values of a facet, **with** its `available` flag, so the two answers stay
  distinguishable: `{:ok, values}` when collected, `:unavailable` when not.
  """
  @spec values(t(), String.t()) :: {:ok, [%{value: String.t(), count: integer()}]} | :unavailable
  def values(%__MODULE__{facets: facets}, name) do
    case Map.get(facets, name) do
      %HlPeers.Facet{available: true, values: values} -> {:ok, values}
      _ -> :unavailable
    end
  end
end

defmodule HlPeers.NotModified do
  @moduledoc """
  What a **caller-supplied** `If-None-Match` gets back on a `304`.

  The client's own ETag cache answers a `304` with the stored body and
  `from_cache: true` instead; this struct exists because a caller who asked the
  conditional question itself deserves the answer to that question, not a body
  it did not ask for.
  """

  @type t :: %__MODULE__{
          etag: String.t() | nil,
          rate_limit: HlPeers.RateLimit.t(),
          api_version: String.t() | nil,
          status: non_neg_integer()
        }

  defstruct [:etag, :rate_limit, :api_version, status: 304]
end

defmodule HlPeers.UpdateResult do
  @moduledoc """
  What `HlPeers.update_gossip_config/4` answers.

  `:json` is **always** present, even when nothing was written, so "generate to
  stdout and let the operator place the file" is a first-class path:

      {:ok, result} = HlPeers.update_gossip_config(client, query)
      IO.write(result.json)

  `:changed` is false when the rendered bytes equal what was already on disk.
  """

  @type t :: %__MODULE__{
          config: HlPeers.GossipConfig.t(),
          json: String.t(),
          changed: boolean(),
          diff: String.t(),
          written_path: String.t() | nil,
          warnings: [HlPeers.Warning.t()],
          envelope: HlPeers.Envelope.t() | nil
        }

  defstruct [:config, :json, :diff, :written_path, :envelope, changed: false, warnings: []]
end

defmodule HlPeers.GossipConfigResult do
  @moduledoc """
  What `HlPeers.list_peers_as_gossip_config/3` answers: the server's
  `format=gossip_config` rendering, parsed with `HlPeers.GossipConfig.parse/1`,
  plus the response metadata (`CLIENT_SPEC.md` §4.7).

  Nothing is written. Write it with the one safe writer,
  `HlPeers.GossipConfig.save/3` (validate, atomic rename, mode `0600`,
  optional `.bak`):

      {:ok, result} = HlPeers.list_peers_as_gossip_config(client, Query.new(cc: ["DE"]))
      {:ok, _saved} = HlPeers.GossipConfig.save(result.config, path)

  `HlPeers.GossipConfig.to_json/1` re-renders in this library's canonical key
  order (`chain`, `root_node_ips`, `try_new_peers`, …), not the server's.

  > 🔴 The node reads `override_gossip_config.json` **when it starts**. Saving it does not steer a running node.
  """

  @type t :: %__MODULE__{
          config: HlPeers.GossipConfig.t(),
          rate_limit: HlPeers.RateLimit.t(),
          api_version: String.t() | nil,
          etag: String.t() | nil,
          from_cache: boolean(),
          status: non_neg_integer()
        }

  defstruct [:config, :rate_limit, :api_version, :etag, from_cache: false, status: 200]
end

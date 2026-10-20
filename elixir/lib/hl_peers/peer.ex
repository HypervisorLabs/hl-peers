defmodule HlPeers.Peer do
  @moduledoc """
  One peer as published (`CLIENT_SPEC.md` §3.2).

  Decoding rules that are load-bearing:

  * 🔴 `tier: nil` means **this node was never placed in the peer graph**. It is **NOT** tier
    0, and this client never defaults it to `0`, `-1` or any sentinel.
  * `nil` is always "not provided" — never zero, never false.
  * `connectable` is **three-state and never folded**. `unknown` means *we
    never observed this*; it is not `no`, not `false` — see `connectable?/1`,
    which is named for the positive confirmation only.
  * `connectable` wire values are the **strings** `"yes"`, `"no"`, `"unknown"`,
    kept as strings, never decoded to a `boolean | nil`.
  * `state`, `role` and `labels` are **open** sets: an unrecognised member is
    kept as-is, never an error, never dropped. Known states: `responsive`,
    `open`, `full`, `syncing`, `listening` (accepts inbound connections; other
    details unknown),
    `unreachable`, `unknown`.
  * `city` is the id of the peer's city cluster (`<CC>-<city>`, e.g.
    `"JP-tokyo"`) when the producer serves city positions, else `nil`. Never a
    coordinate.
  * Geo: `cc_confidence` (`high` | `medium` | `low`), `cc_source`
    (`cloud-range` | `db` | `db+rtt-confirmed`), `asn` and `org` are open
    strings. `region` is set only for cloud-hosted peers (for cloud-hosted peers). `uptime` is a nullable fraction, `feeders` a list of IPv4
    strings (possibly empty).
  * `version` is **always `nil`** today (not published). Documented, not
    asserted.
  * 🔴 `mempool` (`HlPeers.Peer.Mempool`) and `auction`
    (`HlPeers.Peer.Auction`) are always structs. An all-`nil` struct means
    **unknown**; `mempool.serves == nil` is *unknown*, not `false`. A peer
    from an older server that omits either key decodes to the all-`nil` struct.
  * `:extra` holds every key the server adds later, verbatim.
  """

  alias HlPeers.Peer.{Auction, Mempool}

  @type t :: %__MODULE__{
          id: String.t(),
          tier: integer() | nil,
          state: String.t() | nil,
          role: String.t() | nil,
          cc: String.t() | nil,
          cc_confidence: String.t() | nil,
          cc_source: String.t() | nil,
          region: String.t() | nil,
          asn: String.t() | nil,
          org: String.t() | nil,
          city: String.t() | nil,
          operator: String.t() | nil,
          name: String.t() | nil,
          in_deg: integer() | nil,
          out_deg: integer() | nil,
          feeders: [String.t()] | nil,
          latest_height: integer() | nil,
          blocks_behind: integer() | nil,
          live: boolean() | nil,
          connectable: String.t() | nil,
          labels: [String.t()],
          last_seen: String.t() | nil,
          uptime: number() | nil,
          version: String.t() | nil,
          mempool: Mempool.t(),
          auction: Auction.t(),
          extra: map()
        }

  @known ~w(asn auction blocks_behind cc cc_confidence cc_source city connectable feeders id
            in_deg labels last_seen latest_height live mempool name operator org out_deg
            region role state tier uptime version)

  defstruct [
    :id,
    :tier,
    :state,
    :role,
    :cc,
    :cc_confidence,
    :cc_source,
    :region,
    :asn,
    :org,
    :city,
    :operator,
    :name,
    :in_deg,
    :out_deg,
    :feeders,
    :latest_height,
    :blocks_behind,
    :live,
    :connectable,
    :last_seen,
    :uptime,
    :version,
    labels: [],
    mempool: %Mempool{},
    auction: %Auction{},
    extra: %{}
  ]

  @doc "The wire keys this struct models (everything else lands in `:extra`)."
  @spec known_fields() :: [String.t()]
  def known_fields, do: @known

  @doc """
  Decodes one peer, keeping every unrecognised key in `:extra`.

      iex> peer = HlPeers.Peer.from_map(%{"id" => "192.0.2.10", "tier" => nil, "future" => 1})
      iex> {peer.id, peer.tier, peer.extra, peer.mempool.serves}
      {"192.0.2.10", nil, %{"future" => 1}, nil}
  """
  @spec from_map(map()) :: t()
  def from_map(map) when is_map(map) do
    %__MODULE__{
      id: map["id"],
      tier: map["tier"],
      state: map["state"],
      role: map["role"],
      cc: map["cc"],
      cc_confidence: map["cc_confidence"],
      cc_source: map["cc_source"],
      region: map["region"],
      asn: map["asn"],
      org: map["org"],
      city: map["city"],
      operator: map["operator"],
      name: map["name"],
      in_deg: map["in_deg"],
      out_deg: map["out_deg"],
      feeders: map["feeders"],
      latest_height: map["latest_height"],
      blocks_behind: map["blocks_behind"],
      live: map["live"],
      connectable: map["connectable"],
      labels: map["labels"] || [],
      last_seen: map["last_seen"],
      uptime: map["uptime"],
      version: map["version"],
      mempool: Mempool.from_map(map["mempool"]),
      auction: Auction.from_map(map["auction"]),
      extra: Map.drop(map, @known)
    }
  end

  @doc """
  True only for `connectable == "yes"`.

  🪤 An **estimate** of whether the peer has room for another inbound peer, not
  a guarantee; it can be wrong in both directions. `unknown` is not `no`.
  """
  @spec connectable?(t()) :: boolean()
  def connectable?(%__MODULE__{connectable: connectable}), do: connectable == "yes"

  @doc """
  True only for `mempool.serves == true`. `false` for a peer whose status is
  unknown (`nil`) — which is **not** the same as "does not serve"; read
  `peer.mempool.serves` when the difference matters.
  """
  @spec serves_mempool?(t()) :: boolean()
  def serves_mempool?(%__MODULE__{mempool: %Mempool{serves: serves}}), do: serves == true

  @doc """
  True when this node was never placed in the peer graph — which is **not** tier 0.

      iex> HlPeers.Peer.unplaced?(%HlPeers.Peer{tier: nil})
      true
      iex> HlPeers.Peer.unplaced?(%HlPeers.Peer{tier: 0})
      false
  """
  @spec unplaced?(t()) :: boolean()
  def unplaced?(%__MODULE__{tier: tier}), do: is_nil(tier)

  @doc false
  @spec timestamp(term()) :: DateTime.t() | nil
  def timestamp(value) when is_binary(value) do
    case DateTime.from_iso8601(value) do
      {:ok, at, _offset} -> at
      {:error, _} -> nil
    end
  end

  def timestamp(_value), do: nil
end

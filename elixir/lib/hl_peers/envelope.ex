defmodule HlPeers.Envelope do
  @moduledoc """
  The standing fields on every successful body (`CLIENT_SPEC.md` §3.1). They are
  exposed on every result object and are never hidden from the caller.

  🔴 `available: false` means *the producer could not build a graph*. The empty
  `peers` list that comes with it is **not a finding about the network**. This
  client never converts it into an error on a query path and never lets it be
  mistaken for "no peers matched" — but `HlPeers.GossipConfig.from_peers/2`
  and `HlPeers.update_gossip_config/4` **do** refuse it (`:unavailable`,
  decision D-G), because the alternative is emitting a zero-root config, which
  is the crash-loop shape.

  Check `available` **before** `stale`: a dark subsystem reports both as false.

  `next_sweep_at` is the data's own clock; `HlPeers.Poller` aligns to it
  rather than guessing.

  `attribution` is the data-licence credit line the server sends with the geo
  fields (e.g. the IP-geolocation database's CC BY notice). If you republish
  `cc` / `asn` / `org`, carry it with them.
  """

  @type t :: %__MODULE__{
          version: String.t() | nil,
          available: boolean() | nil,
          stale: boolean() | nil,
          chain: String.t() | nil,
          generated_at: String.t() | nil,
          next_sweep_at: String.t() | nil,
          attribution: String.t() | nil
        }

  defstruct [:version, :available, :stale, :chain, :generated_at, :next_sweep_at, :attribution]

  @doc "The wire keys this struct models."
  @spec known_fields() :: [String.t()]
  def known_fields, do: ~w(attribution available chain generated_at next_sweep_at stale version)

  @doc false
  @spec from_body(map()) :: t()
  def from_body(body) when is_map(body) do
    %__MODULE__{
      version: body["version"],
      available: body["available"],
      stale: body["stale"],
      chain: body["chain"],
      generated_at: body["generated_at"],
      next_sweep_at: body["next_sweep_at"],
      attribution: body["attribution"]
    }
  end
end

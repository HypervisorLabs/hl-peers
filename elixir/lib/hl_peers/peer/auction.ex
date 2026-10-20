defmodule HlPeers.Peer.Auction do
  @moduledoc """
  The peer's standing in the on-chain gossip read-priority auction (public
  chain data; `CLIENT_SPEC.md` §3.2).

  * `status` — `"holds_slot"`, `"bidding"`, `"bid_before"` (an open string);
    `nil` means **never seen in the auction**;
  * `slot`, `since` — set only for `holds_slot`;
  * `last_bid_at` — the last bid, a UTC `DateTime`.

  An all-`nil` struct means unknown, and is what a peer from an older server
  that omits the key decodes to. Unknown keys inside the object are ignored.
  """

  @type t :: %__MODULE__{
          status: String.t() | nil,
          slot: integer() | nil,
          since: DateTime.t() | nil,
          last_bid_at: DateTime.t() | nil
        }

  defstruct [:status, :slot, :since, :last_bid_at]

  @known ~w(last_bid_at since slot status)

  @doc "The wire keys this struct models."
  @spec known_fields() :: [String.t()]
  def known_fields, do: @known

  @doc """
  Decodes the object; a missing or non-object value is the all-`nil` struct.

      iex> HlPeers.Peer.Auction.from_map(%{"status" => "bidding", "future" => 1})
      %HlPeers.Peer.Auction{status: "bidding"}
  """
  @spec from_map(term()) :: t()
  def from_map(map) when is_map(map) do
    %__MODULE__{
      status: map["status"],
      slot: map["slot"],
      since: HlPeers.Peer.timestamp(map["since"]),
      last_bid_at: HlPeers.Peer.timestamp(map["last_bid_at"])
    }
  end

  def from_map(_absent), do: %__MODULE__{}
end

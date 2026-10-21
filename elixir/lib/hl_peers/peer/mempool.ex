defmodule HlPeers.Peer.Mempool do
  @moduledoc """
  Does this peer serve the mempool? (`CLIENT_SPEC.md` §3.2.)

  🔴 `serves: nil` is **unknown — unknown**, not "no". An all-`nil` struct
  means the whole object is unknown, and is what a peer from an older server
  that omits the key decodes to.

  * `last_checked` — when it was last checked, a UTC `DateTime` (`nil` if absent
    or not an ISO-8601 instant);
  * `verdict` — a short machine verdict (`^[a-z0-9_-]{1,32}$`), open set;
  * `first_frame_ms` — time to the first mempool message;
  * `source` — `"probe"` or `"relay"`: which of our measurements produced the
    verdict; an open string.

  Unknown keys inside the object are ignored.
  """

  @type t :: %__MODULE__{
          serves: boolean() | nil,
          last_checked: DateTime.t() | nil,
          verdict: String.t() | nil,
          first_frame_ms: integer() | nil,
          source: String.t() | nil
        }

  defstruct [:serves, :last_checked, :verdict, :first_frame_ms, :source]

  @known ~w(first_frame_ms last_checked serves source verdict)

  @doc "The wire keys this struct models."
  @spec known_fields() :: [String.t()]
  def known_fields, do: @known

  @doc """
  Decodes the object; a missing or non-object value is the all-`nil` struct.

      iex> HlPeers.Peer.Mempool.from_map(nil)
      %HlPeers.Peer.Mempool{}
  """
  @spec from_map(term()) :: t()
  def from_map(map) when is_map(map) do
    %__MODULE__{
      serves: map["serves"],
      last_checked: HlPeers.Peer.timestamp(map["last_checked"]),
      verdict: map["verdict"],
      first_frame_ms: map["first_frame_ms"],
      source: map["source"]
    }
  end

  def from_map(_absent), do: %__MODULE__{}
end

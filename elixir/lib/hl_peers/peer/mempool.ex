defmodule HlPeers.Peer.Mempool do
  @moduledoc """
  Does this peer serve the mempool? (`CLIENT_SPEC.md` §3.2.)

  🔴 `serves: nil` is **unknown — unknown**, not "no". An all-`nil` struct
  means the whole object is unknown, and is what a peer from an older server
  that omits the key decodes to.

  `serves: true` means a serve confirmed within the last 48 hours.

  * `last_checked` — kept for compatibility: equals `last_confirmed` when the
    peer has a confirmation, otherwise when the verdict was last observed; a UTC
    `DateTime` (`nil` if absent or not an ISO-8601 instant);
  * `last_confirmed` — when a serve was last confirmed (`DateTime` or `nil`);
  * `last_attempted` — when the peer's mempool was last checked, any outcome
    (`DateTime` or `nil`);
  * `verdict` — a short machine verdict (`^[a-z0-9_-]{1,32}$`), open set, e.g.
    `"serves"` or `"served_previously"` (a serve was confirmed, but not within
    the last 48 hours; `serves` is then `nil`);
  * `first_frame_ms` — time to the first mempool message;
  * `source` — `"probe"` (observed by our network probing) or `nil`; an open
    string.

  Unknown keys inside the object are ignored.
  """

  @type t :: %__MODULE__{
          serves: boolean() | nil,
          last_checked: DateTime.t() | nil,
          verdict: String.t() | nil,
          first_frame_ms: integer() | nil,
          source: String.t() | nil,
          last_confirmed: DateTime.t() | nil,
          last_attempted: DateTime.t() | nil
        }

  defstruct [
    :serves,
    :last_checked,
    :verdict,
    :first_frame_ms,
    :source,
    :last_confirmed,
    :last_attempted
  ]

  @known ~w(first_frame_ms last_attempted last_checked last_confirmed serves source verdict)

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
      source: map["source"],
      last_confirmed: HlPeers.Peer.timestamp(map["last_confirmed"]),
      last_attempted: HlPeers.Peer.timestamp(map["last_attempted"])
    }
  end

  def from_map(_absent), do: %__MODULE__{}
end

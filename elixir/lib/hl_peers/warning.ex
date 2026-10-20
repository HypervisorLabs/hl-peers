defmodule HlPeers.Warning do
  @moduledoc """
  A non-fatal finding from `HlPeers.GossipConfig.validate/1`,
  `merge_peers/3`, `save/3` or `HlPeers.update_gossip_config/4`.

  The `:code` is the stable identifier the conformance suite asserts on; the
  `:message` is prose and is not asserted. The closed set is
  `CLIENT_SPEC.md` §6.4.

  🔴 Warnings are never swallowed: they ride on every result that can produce
  them, and the `>24 roots`, `>64 roots`, `truncated` and
  `exclude beats keep_existing` ones are the whole point of the exercise.
  """

  @type code ::
          :many_roots_gt_24
          | :many_roots_gt_64
          | :chain_absent
          | :try_new_peers_false_multi_root
          | :truncated
          | :exclude_overrides_keep_existing

  @type t :: %__MODULE__{code: code(), message: String.t(), detail: term()}

  defstruct [:code, :message, :detail]

  @codes [
    :many_roots_gt_24,
    :many_roots_gt_64,
    :chain_absent,
    :try_new_peers_false_multi_root,
    :truncated,
    :exclude_overrides_keep_existing
  ]

  @doc "The closed set of warning codes."
  @spec codes() :: [code()]
  def codes, do: @codes

  @doc false
  @spec new(code(), term()) :: t()
  def new(code, detail \\ nil) when code in @codes do
    %__MODULE__{code: code, message: message_for(code, detail), detail: detail}
  end

  @doc """
  The codes of a warning list, in order.

      iex> HlPeers.Warning.codes([HlPeers.Warning.new(:chain_absent)])
      [:chain_absent]
  """
  @spec codes([t()]) :: [code()]
  def codes(warnings) when is_list(warnings), do: Enum.map(warnings, & &1.code)

  defp message_for(:many_roots_gt_24, n),
    do:
      "root_node_ips holds #{n} entries: a long root list makes the node slower to start " <>
        "and buys nothing; prefer a short list"

  defp message_for(:many_roots_gt_64, n),
    do:
      "root_node_ips holds #{n} entries (over 64, escalated): a long root list makes the " <>
        "node slower to start and buys nothing; prefer a short list"

  defp message_for(:chain_absent, _),
    do:
      "emit `chain` matching the node's `--chain`; a mismatch stops the node from starting, " <>
        "and this library cannot tell which chain your node runs"

  defp message_for(:try_new_peers_false_multi_root, _),
    do:
      "whether `try_new_peers: false` restricts a multi-entry list to exactly that list is " <>
        "unverified; only a single root is known to pin"

  defp message_for(:truncated, n),
    do: "dropped #{n} candidate(s) at max_roots; the head of the API's ranking was kept"

  defp message_for(:exclude_overrides_keep_existing, ips),
    do:
      "exclude beats keep_existing for #{Enum.join(List.wrap(ips), ", ")}: an operator who " <>
        "asks for an IP to be excluded means it"
end

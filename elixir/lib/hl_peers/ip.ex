defmodule HlPeers.IP do
  @moduledoc """
  Dotted-quad IPv4 recognition, and the one IPv6 refusal message the whole
  library shares (`CLIENT_SPEC.md` §6.4(3), decision D-B).

  The peer API is IPv4-only and `hl-node`'s acceptance of an IPv6 literal in a
  gossip config is **not evidenced**. Refusing is the safe side of an unknown
  whose failure mode is a crash loop.
  """

  @ipv6_reason "IPv6 is out of contract: the peer API is IPv4-only and the node's support " <>
                 "for an IPv6 literal here is unverified. Refusing rather than writing an " <>
                 "untested value into a file whose failure mode is a crash loop."

  @doc "The verbatim IPv6 refusal reason."
  @spec ipv6_reason() :: String.t()
  def ipv6_reason, do: @ipv6_reason

  @doc """
  True for a strict dotted quad: four decimal octets, `0..255`, no leading
  zeros, nothing else.

      iex> HlPeers.IP.ipv4?("192.0.2.10")
      true
      iex> HlPeers.IP.ipv4?("999.0.2.10")
      false
      iex> HlPeers.IP.ipv4?("2001:db8::1")
      false
      iex> HlPeers.IP.ipv4?("192.0.2.010")
      false
  """
  @spec ipv4?(term()) :: boolean()
  def ipv4?(value) when is_binary(value) do
    case String.split(value, ".") do
      [_, _, _, _] = parts -> Enum.all?(parts, &octet?/1)
      _ -> false
    end
  end

  def ipv4?(_), do: false

  @doc """
  True for anything that looks like an IPv6 literal, so the refusal can say so
  rather than the generic "not an IPv4".

      iex> HlPeers.IP.ipv6_like?("2001:db8::1")
      true
      iex> HlPeers.IP.ipv6_like?("192.0.2.10")
      false
  """
  @spec ipv6_like?(term()) :: boolean()
  def ipv6_like?(value) when is_binary(value), do: String.contains?(value, ":")
  def ipv6_like?(_), do: false

  @doc false
  @spec reason(term()) :: String.t()
  def reason(value) do
    if ipv6_like?(value), do: @ipv6_reason, else: "#{inspect_value(value)} is not an IPv4 address"
  end

  defp inspect_value(v) when is_binary(v), do: v
  defp inspect_value(v), do: inspect(v)

  defp octet?(""), do: false
  defp octet?("0"), do: true
  defp octet?(<<?0, _::binary>>), do: false

  defp octet?(part) do
    case Integer.parse(part) do
      {n, ""} when n >= 0 and n <= 255 -> true
      _ -> false
    end
  end
end

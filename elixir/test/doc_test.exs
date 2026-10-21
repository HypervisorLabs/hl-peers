defmodule HlPeers.DocTest do
  @moduledoc """
  Every `iex>` example in the library's documentation is executed here, so a
  doc that drifts from the code fails the build rather than misleading an
  operator.
  """

  use ExUnit.Case, async: true

  doctest HlPeers.Client
  doctest HlPeers.Diff
  doctest HlPeers.Error
  doctest HlPeers.Facets
  doctest HlPeers.GossipConfig
  doctest HlPeers.GossipConfig.Serializer
  doctest HlPeers.IP
  doctest HlPeers.Peer
  doctest HlPeers.Query
  doctest HlPeers.RateLimit
  doctest HlPeers.Warning
end

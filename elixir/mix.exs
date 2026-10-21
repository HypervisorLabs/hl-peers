defmodule HlPeers.MixProject do
  use Mix.Project

  @version "1.1.0"

  def project do
    [
      app: :hl_peers,
      version: @version,
      elixir: "~> 1.15",
      elixirc_paths: elixirc_paths(Mix.env()),
      start_permanent: Mix.env() == :prod,
      deps: deps(),
      name: "hl_peers",
      description:
        "Client for the Hyperliquid Peer API and a pure, offline editor for hl-node's override_gossip_config.json.",
      package: package(),
      docs: docs(),
      dialyzer: [
        plt_local_path: "_build/plts",
        plt_core_path: "_build/plts",
        flags: [:error_handling, :extra_return, :missing_return]
      ],
      test_coverage: [summary: false]
    ]
  end

  def application do
    [extra_applications: [:logger, :crypto]]
  end

  defp elixirc_paths(:test), do: ["lib", "test/support"]
  defp elixirc_paths(_), do: ["lib"]

  defp deps do
    [
      {:jason, "~> 1.4"},
      {:req, "~> 0.5"},
      {:credo, "~> 1.7", only: [:dev, :test], runtime: false},
      {:dialyxir, "~> 1.4", only: [:dev, :test], runtime: false},
      {:ex_doc, "~> 0.34", only: :dev, runtime: false}
    ]
  end

  # 🔴 Local only: the owner reviews before anything is public. This package is
  # NOT published to Hex; the entry exists so `mix docs` has metadata.
  defp package do
    [
      licenses: ["MIT"],
      files: ~w(lib mix.exs README.md CHANGELOG.md),
      links: %{"GitHub" => "https://github.com/HypervisorLabs/hl-peers"}
    ]
  end

  defp docs do
    [
      main: "readme",
      extras: ["README.md", "CHANGELOG.md", "../docs/elixir.md": [title: "Guide"]],
      source_ref: "v#{@version}",
      groups_for_modules: [
        "Query surface": [
          HlPeers,
          HlPeers.Client,
          HlPeers.Query,
          HlPeers.Peer,
          HlPeers.Envelope,
          HlPeers.PeerList,
          HlPeers.PeerEnvelope,
          HlPeers.Facets,
          HlPeers.Peer.Mempool,
          HlPeers.Peer.Auction,
          HlPeers.NetworkMap,
          HlPeers.NetworkMap.Graph,
          HlPeers.NetworkMap.Summary,
          HlPeers.NetworkMap.Edge,
          HlPeers.RateLimit,
          HlPeers.Poller
        ],
        "Gossip config": [
          HlPeers.GossipConfig,
          HlPeers.GossipConfig.SaveResult,
          HlPeers.GossipConfigResult,
          HlPeers.UpdateResult,
          HlPeers.Warning
        ],
        Plumbing: [
          HlPeers.Adapter,
          HlPeers.Adapter.Req,
          HlPeers.Error,
          HlPeers.Response
        ]
      ]
    ]
  end
end

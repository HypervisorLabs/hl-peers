defmodule HlPeers.GossipConfig do
  @moduledoc """
  A pure, offline, deterministic editor for hl-node's
  `override_gossip_config.json` (`CLIENT_SPEC.md` §6).

  **No network, no clock, no environment lookups, no restart of anything.**
  Given the same input this module produces the same bytes on every platform,
  in every language, forever. It does not depend on the HTTP layer: an operator
  can edit a config on a box with no API access at all.

  > 🔴 The node reads `override_gossip_config.json` **when it starts**. Writing this file does **not** steer a running node: the change
  > takes effect at the node's next start. This library will **never** restart,
  > signal, or otherwise disturb your node — that is the operator's decision, at
  > the operator's moment. Restarting a node *to apply a config* while it is
  > behind can make a lagging box worse.

  The node's file is `$HOME/override_gossip_config.json`.

  ## Two entry shapes in one file

  🪤 `root_node_ips` entries are `{"Ip": "192.0.2.10"}` objects — capital `I`,
  lowercase `p`, load-bearing — while `reserved_peer_ips` entries are **bare
  strings**. Round-tripping one shape through the other silently produces a file
  the node will not accept, so `validate/1` refuses both confusions.

  ## Return conventions

  Mutators that cannot fail (`add_root_ip/2`, `set_chain/2`, …) return the
  config, so they chain. The ones that can — `set_n_gossip_peers/2`,
  `merge_peers/3`, `from_peers/2`, `save/3`, `validate/1`, `parse/1`, `load/1` —
  answer `{:ok, …}` or `{:error, %HlPeers.Error{}}`, each with a `!` variant.
  `merge_peers/3` and `from_peers/2` answer `{:ok, config, warnings}` — and also
  keep those warnings **pending** on the config, so `validate/1` and `save/3`
  re-report them (§6.5). `clear_warnings/1` is the only thing that drops them.

      iex> alias HlPeers.GossipConfig
      iex> config = GossipConfig.new("Mainnet", ["192.0.2.10"])
      iex> config |> GossipConfig.add_root_ip("192.0.2.11") |> GossipConfig.root_ips()
      ["192.0.2.10", "192.0.2.11"]
  """

  import Bitwise, only: [{:&&&, 2}]

  alias HlPeers.{Error, IP, Warning}
  alias HlPeers.GossipConfig.{SaveResult, Serializer}

  @type chain :: String.t()
  @type t :: %__MODULE__{
          data: %{optional(String.t()) => term()},
          pending_warnings: [Warning.t()]
        }

  defstruct data: %{}, pending_warnings: []

  @empty_roots_reason "refusing to emit an empty root_node_ips: hl-node asserts on a " <>
                        "non-empty list at start and will crash-loop; there is no built-in fallback"

  @doc "The verbatim zero-roots refusal reason — the single most important guard in the library."
  @spec empty_roots_reason() :: String.t()
  def empty_roots_reason, do: @empty_roots_reason

  # ------------------------------------------------------------------
  # constructors and I/O
  # ------------------------------------------------------------------

  @doc """
  Builds a config from scratch.

  `root_ips` accepts bare IPv4 strings **or** `%{"Ip" => …}` entries on input and
  always stores the object shape.

      iex> HlPeers.GossipConfig.new("Mainnet", ["192.0.2.10"]) |> HlPeers.GossipConfig.to_json()
      "{\\n  \\"chain\\": \\"Mainnet\\",\\n  \\"root_node_ips\\": [\\n    {\\n      \\"Ip\\": \\"192.0.2.10\\"\\n    }\\n  ]\\n}\\n"
  """
  @spec new(chain() | nil, [String.t() | map()]) :: t()
  def new(chain \\ nil, root_ips \\ []) do
    data = %{"root_node_ips" => Enum.map(root_ips, &root_entry/1)}
    %__MODULE__{data: if(chain, do: Map.put(data, "chain", chain), else: data)}
  end

  @doc """
  Decodes config text. Unknown keys are **preserved verbatim**; nothing is
  validated here — `validate/1` does that.

  Invalid JSON is a `:decode` error; a non-object top level is
  `Invalid{parameter: "$"}`.

      iex> {:ok, config} = HlPeers.GossipConfig.parse(~s({"root_node_ips": [{"Ip": "192.0.2.10"}]}))
      iex> HlPeers.GossipConfig.root_ips(config)
      ["192.0.2.10"]

      iex> {:error, e} = HlPeers.GossipConfig.parse("[]")
      iex> {e.kind, e.parameter}
      {:invalid, "$"}
  """
  @spec parse(String.t()) :: {:ok, t()} | {:error, Error.t()}
  def parse(json_text) when is_binary(json_text) do
    case Jason.decode(json_text) do
      {:ok, data} when is_map(data) ->
        {:ok, %__MODULE__{data: data}}

      {:ok, _other} ->
        {:error, Error.invalid("$", "expected a JSON object")}

      {:error, cause} ->
        {:error, Error.new(:decode, cause: cause, body_excerpt: String.slice(json_text, 0, 512))}
    end
  end

  @doc "Raising `parse/1`."
  @spec parse!(String.t()) :: t()
  def parse!(json_text) do
    case parse(json_text) do
      {:ok, config} -> config
      {:error, error} -> raise error
    end
  end

  @doc """
  Reads and parses a file.

  A missing file is `{:error, %Error{kind: :not_found}}`. 🔴 This library never
  invents an empty config for a missing file, because an empty config *is* the
  crash-loop shape. `load_or_new/2` is the explicit opt-in to that.
  """
  @spec load(Path.t()) :: {:ok, t()} | {:error, Error.t()}
  def load(path) do
    case File.read(path) do
      {:ok, text} -> parse(text)
      {:error, :enoent} -> {:error, Error.new(:not_found, path: path)}
      {:error, reason} -> {:error, Error.new(:transport, cause: reason, path: path)}
    end
  end

  @doc "Raising `load/1`."
  @spec load!(Path.t()) :: t()
  def load!(path) do
    case load(path) do
      {:ok, config} -> config
      {:error, error} -> raise error
    end
  end

  @doc """
  `load/1`, but a missing file yields `new(chain, [])` instead of `:not_found`.

  The result has zero roots, so it must be filled before it can be saved.
  """
  @spec load_or_new(Path.t(), chain() | nil) :: {:ok, t()} | {:error, Error.t()}
  def load_or_new(path, chain \\ nil) do
    case load(path) do
      {:error, %Error{kind: :not_found}} -> {:ok, new(chain, [])}
      other -> other
    end
  end

  @doc """
  The default path, `$HOME/override_gossip_config.json`.

  It is a *default*, never a discovery: this library **will not** inspect a
  running process, a systemd unit or `/proc` to guess where your node's file is.

  🪤 The upstream evidence cannot distinguish `$HOME`-relative from cwd-relative,
  because on every observed box `cwd == $HOME`; `$HOME`-relative is the labelled
  **hypothesis**. If you are not sure, render to stdout with `to_json/1` and
  place the file yourself — that is the safest option.
  """
  @spec default_config_path() :: Path.t()
  def default_config_path do
    Path.join(System.get_env("HOME") || System.user_home() || ".", "override_gossip_config.json")
  end

  @doc """
  Serializes to the byte-exact form of `CLIENT_SPEC.md` §6.3. Does **not**
  validate — `save/3` validates.
  """
  @spec to_json(t()) :: String.t()
  def to_json(%__MODULE__{data: data}), do: Serializer.render(data)

  @doc "`to_json/1`, but `validate/1` runs first."
  @spec to_json_validated(t()) :: {:ok, String.t()} | {:error, Error.t()}
  def to_json_validated(%__MODULE__{} = config) do
    with {:ok, _warnings} <- validate(config), do: {:ok, to_json(config)}
  end

  @doc "The raw decoded map, unknown keys included."
  @spec to_map(t()) :: map()
  def to_map(%__MODULE__{data: data}), do: data

  # ------------------------------------------------------------------
  # field algebra
  # ------------------------------------------------------------------

  @doc """
  The root IPs as **bare strings**, in file order. The `{"Ip": …}` wrapper is an
  encoding detail and does not leak into this accessor.
  """
  @spec root_ips(t()) :: [String.t()]
  def root_ips(%__MODULE__{data: data}) do
    data |> Map.get("root_node_ips") |> List.wrap() |> Enum.map(&entry_ip/1)
  end

  @doc """
      iex> HlPeers.GossipConfig.new("Mainnet", ["192.0.2.10"])
      ...> |> HlPeers.GossipConfig.has_root_ip?("192.0.2.10")
      true
  """
  @spec has_root_ip?(t(), String.t()) :: boolean()
  def has_root_ip?(%__MODULE__{} = config, ip), do: ip in root_ips(config)

  @doc "Appends a root. A duplicate is a **no-op**, not an error."
  @spec add_root_ip(t(), String.t()) :: t()
  def add_root_ip(%__MODULE__{} = config, ip) do
    if has_root_ip?(config, ip) do
      config
    else
      put(config, "root_node_ips", entries(config) ++ [root_entry(ip)])
    end
  end

  @doc """
  Removes a root by bare string. An absent IP is a no-op — including on a
  config that carries no `root_node_ips` key, which is left without one rather
  than gaining an empty list.
  """
  @spec remove_root_ip(t(), String.t()) :: t()
  def remove_root_ip(%__MODULE__{data: data} = config, ip) do
    if Map.has_key?(data, "root_node_ips") do
      put(config, "root_node_ips", Enum.reject(entries(config), &(entry_ip(&1) == ip)))
    else
      config
    end
  end

  @doc "Replaces the root list wholesale, preserving the given order, de-duplicating first-wins."
  @spec set_root_ips(t(), [String.t() | map()]) :: t()
  def set_root_ips(%__MODULE__{} = config, ips) do
    put(config, "root_node_ips", ips |> Enum.map(&root_entry/1) |> uniq_entries())
  end

  @doc "The reserved peer IPs — 🪤 **bare strings**, not `{\"Ip\": …}` objects."
  @spec reserved_peer_ips(t()) :: [String.t()]
  def reserved_peer_ips(%__MODULE__{data: data}),
    do: data |> Map.get("reserved_peer_ips") |> List.wrap()

  @doc "Appends a reserved peer IP as a bare string. A duplicate is a no-op."
  @spec add_reserved_peer_ip(t(), String.t()) :: t()
  def add_reserved_peer_ip(%__MODULE__{} = config, ip) do
    current = reserved_peer_ips(config)
    if ip in current, do: config, else: put(config, "reserved_peer_ips", current ++ [ip])
  end

  @doc """
  Removes a reserved peer IP. An absent IP is a no-op, and a config with no
  `reserved_peer_ips` key does not gain an empty one.
  """
  @spec remove_reserved_peer_ip(t(), String.t()) :: t()
  def remove_reserved_peer_ip(%__MODULE__{data: data} = config, ip) do
    if Map.has_key?(data, "reserved_peer_ips") do
      put(config, "reserved_peer_ips", Enum.reject(reserved_peer_ips(config), &(&1 == ip)))
    else
      config
    end
  end

  @doc "Replaces the reserved peer list wholesale (bare strings, first-wins de-duplication)."
  @spec set_reserved_peer_ips(t(), [String.t()]) :: t()
  def set_reserved_peer_ips(%__MODULE__{} = config, ips) do
    put(config, "reserved_peer_ips", Enum.uniq(ips))
  end

  @doc "The `chain` value, or `nil` when the key is absent."
  @spec chain(t()) :: String.t() | nil
  def chain(%__MODULE__{data: data}), do: Map.get(data, "chain")

  @doc """
  Sets `chain`. The spelling is exact and case matters: `Mainnet` / `Testnet`.
  A `chain` that disagrees with the node's `--chain` stops the node from starting.
  """
  @spec set_chain(t(), chain()) :: t()
  def set_chain(%__MODULE__{} = config, chain), do: put(config, "chain", chain)

  @doc "The `try_new_peers` value, or `nil` when absent."
  @spec try_new_peers(t()) :: boolean() | nil
  def try_new_peers(%__MODULE__{data: data}), do: Map.get(data, "try_new_peers")

  @doc "Sets `try_new_peers`; `nil` removes the key."
  @spec set_try_new_peers(t(), boolean() | nil) :: t()
  def set_try_new_peers(%__MODULE__{} = config, value), do: put(config, "try_new_peers", value)

  @doc "The `n_gossip_peers` value, or `nil` when absent."
  @spec n_gossip_peers(t()) :: integer() | nil
  def n_gossip_peers(%__MODULE__{data: data}), do: Map.get(data, "n_gossip_peers")

  @doc """
  Sets `n_gossip_peers`, **refusing** anything outside `[8, 100]` at both ends
  (decision D-C). Silently clamping would write a number the operator did not
  choose into a file they will read back later. `nil` removes the key.

      iex> {:error, e} = HlPeers.GossipConfig.new("Mainnet", ["192.0.2.10"])
      ...>               |> HlPeers.GossipConfig.set_n_gossip_peers(7)
      iex> {e.kind, e.parameter}
      {:invalid, "n_gossip_peers"}
  """
  @spec set_n_gossip_peers(t(), integer() | nil) :: {:ok, t()} | {:error, Error.t()}
  def set_n_gossip_peers(%__MODULE__{} = config, nil),
    do: {:ok, put(config, "n_gossip_peers", nil)}

  def set_n_gossip_peers(%__MODULE__{} = config, value)
      when is_integer(value) and value in 8..100 do
    {:ok, put(config, "n_gossip_peers", value)}
  end

  def set_n_gossip_peers(%__MODULE__{}, value) do
    {:error,
     Error.invalid(
       "n_gossip_peers",
       "#{inspect(value)} is outside [8, 100]; this client refuses rather than clamping (D-C)"
     )}
  end

  @doc "Raising `set_n_gossip_peers/2`."
  @spec set_n_gossip_peers!(t(), integer() | nil) :: t()
  def set_n_gossip_peers!(config, value) do
    case set_n_gossip_peers(config, value) do
      {:ok, updated} -> updated
      {:error, error} -> raise error
    end
  end

  @doc "The `split_client_blocks` value, or `nil` when absent."
  @spec split_client_blocks(t()) :: boolean() | nil
  def split_client_blocks(%__MODULE__{data: data}), do: Map.get(data, "split_client_blocks")

  @doc """
  `true` writes the key; `false` **removes** it, because the node's own
  serializer omits it when false.

  An explicit `false` already present in a parsed file is preserved by a round
  trip — only this setter removes it. `unset_split_client_blocks/1` removes it
  in either case.
  """
  @spec set_split_client_blocks(t(), boolean() | nil) :: t()
  def set_split_client_blocks(%__MODULE__{} = config, true),
    do: put(config, "split_client_blocks", true)

  def set_split_client_blocks(%__MODULE__{} = config, _falsey),
    do: unset_split_client_blocks(config)

  @doc "Removes `split_client_blocks` whatever its current value."
  @spec unset_split_client_blocks(t()) :: t()
  def unset_split_client_blocks(%__MODULE__{} = config),
    do: put(config, "split_client_blocks", nil)

  # ------------------------------------------------------------------
  # validate
  # ------------------------------------------------------------------

  @doc """
  Answers `{:ok, warnings}` or the **first** error in the fixed order of
  `CLIENT_SPEC.md` §6.4, so all four language clients report the same one.

  The order: zero/absent roots · root entry shape · IPv4 validity (IPv6 gets its
  own refusal) · duplicates · `chain` spelling · `n_gossip_peers` range ·
  boolean types.

      iex> {:error, e} = HlPeers.GossipConfig.new("Mainnet", []) |> HlPeers.GossipConfig.validate()
      iex> {e.kind, e.parameter, String.contains?(e.reason, "empty root_node_ips")}
      {:invalid, "root_node_ips", true}

      iex> {:ok, warnings} = HlPeers.GossipConfig.new(nil, ["192.0.2.10"])
      ...>                   |> HlPeers.GossipConfig.validate()
      iex> HlPeers.Warning.codes(warnings)
      [:chain_absent]
  """
  @spec validate(t()) :: {:ok, [Warning.t()]} | {:error, Error.t()}
  def validate(%__MODULE__{data: data} = config) do
    with :ok <- check_roots_present(data),
         :ok <- check_root_entry_shapes(Map.fetch!(data, "root_node_ips")),
         :ok <- check_reserved_list(data),
         :ok <- check_root_ips_valid(config),
         :ok <- check_reserved_ips_valid(config),
         :ok <- check_duplicates("root_node_ips", root_ips(config)),
         :ok <- check_duplicates("reserved_peer_ips", reserved_peer_ips(config)),
         :ok <- check_chain(data),
         :ok <- check_n_gossip_peers(data),
         :ok <- check_boolean(data, "try_new_peers"),
         :ok <- check_boolean(data, "split_client_blocks") do
      {:ok, all_warnings(config)}
    end
  end

  # §6.5: merge-time warnings are PENDING on the config, so a caller who ignored
  # what merge_peers/3 returned still sees them here, in save/3 and in
  # HlPeers.update_gossip_config/4. De-duplicated by code; compared as a set.
  defp all_warnings(%__MODULE__{pending_warnings: pending} = config) do
    Enum.uniq_by(pending ++ warnings(config), & &1.code)
  end

  @doc "Raising `validate/1`; returns the warnings."
  @spec validate!(t()) :: [Warning.t()]
  def validate!(config) do
    case validate(config) do
      {:ok, warnings} -> warnings
      {:error, error} -> raise error
    end
  end

  defp check_roots_present(data) do
    case Map.get(data, "root_node_ips") do
      list when is_list(list) and list != [] ->
        :ok

      nil ->
        {:error, Error.invalid("root_node_ips", @empty_roots_reason)}

      [] ->
        {:error, Error.invalid("root_node_ips", @empty_roots_reason)}

      other ->
        {:error, Error.invalid("root_node_ips", "expected an array, got #{inspect(other)}")}
    end
  end

  defp check_root_entry_shapes(entries) do
    entries
    |> Enum.with_index()
    |> Enum.find_value(:ok, fn {entry, i} ->
      case entry do
        %{"Ip" => _} = e when map_size(e) == 1 ->
          nil

        _ ->
          {:error,
           Error.invalid(
             "root_node_ips[#{i}]",
             "expected an object with the single key \"Ip\" (capital I, lowercase p — the " <>
               "spelling is load-bearing), got #{inspect(entry)}"
           )}
      end
    end)
  end

  defp check_reserved_list(data) do
    case Map.get(data, "reserved_peer_ips") do
      nil ->
        :ok

      list when is_list(list) ->
        list
        |> Enum.with_index()
        |> Enum.find_value(:ok, fn {entry, i} -> bare_string_or_error(entry, i) end)

      other ->
        {:error, Error.invalid("reserved_peer_ips", "expected an array, got #{inspect(other)}")}
    end
  end

  defp bare_string_or_error(entry, _index) when is_binary(entry), do: nil

  defp bare_string_or_error(_entry, index) do
    {:error,
     Error.invalid(
       "reserved_peer_ips[#{index}]",
       "expected a bare IPv4 string; reserved_peer_ips does NOT take the {\"Ip\": …} object " <>
         "shape that root_node_ips takes, and a node will not accept the confusion"
     )}
  end

  defp check_root_ips_valid(config) do
    config
    |> root_ips()
    |> Enum.with_index()
    |> Enum.find_value(:ok, fn {ip, i} ->
      if IP.ipv4?(ip),
        do: nil,
        else: {:error, Error.invalid("root_node_ips[#{i}]", IP.reason(ip))}
    end)
  end

  defp check_reserved_ips_valid(config) do
    config
    |> reserved_peer_ips()
    |> Enum.with_index()
    |> Enum.find_value(:ok, fn {ip, i} ->
      if IP.ipv4?(ip),
        do: nil,
        else: {:error, Error.invalid("reserved_peer_ips[#{i}]", IP.reason(ip))}
    end)
  end

  defp check_duplicates(parameter, ips) do
    case ips -- Enum.uniq(ips) do
      [] -> :ok
      [dup | _] -> {:error, Error.invalid(parameter, "duplicate #{dup}")}
    end
  end

  defp check_chain(data) do
    case Map.get(data, "chain") do
      nil ->
        :ok

      c when c in ["Mainnet", "Testnet"] ->
        :ok

      other ->
        {:error,
         Error.invalid("chain", "#{inspect(other)} is not Mainnet or Testnet; case matters")}
    end
  end

  defp check_n_gossip_peers(data) do
    case Map.get(data, "n_gossip_peers") do
      nil ->
        :ok

      n when is_integer(n) and n in 8..100 ->
        :ok

      other ->
        {:error,
         Error.invalid("n_gossip_peers", "#{inspect(other)} is not an integer inside [8, 100]")}
    end
  end

  defp check_boolean(data, key) do
    case Map.get(data, key) do
      nil -> :ok
      v when is_boolean(v) -> :ok
      other -> {:error, Error.invalid(key, "#{inspect(other)} is not a boolean")}
    end
  end

  defp warnings(config) do
    roots = root_ips(config)
    n = length(roots)

    size_warning =
      cond do
        n > 64 -> [Warning.new(:many_roots_gt_64, n)]
        n > 24 -> [Warning.new(:many_roots_gt_24, n)]
        true -> []
      end

    chain_warning = if is_nil(chain(config)), do: [Warning.new(:chain_absent)], else: []

    # 🔴 The codes are INDEPENDENT: every condition that holds fires. 25 roots with
    # try_new_peers: false reports both many_roots_gt_24 and
    # try_new_peers_false_multi_root — they describe two unrelated facts (this list
    # is slow to boot; this list may or may not pin), and swallowing one because
    # the other fired would hide the harder problem behind the easier one. The one
    # exception is the gt_24/gt_64 pair above, which is mutually exclusive.
    pin_warning =
      if try_new_peers(config) == false and n > 1,
        do: [Warning.new(:try_new_peers_false_multi_root)],
        else: []

    size_warning ++ chain_warning ++ pin_warning
  end

  @doc """
  The warnings pending on this config from its last `merge_peers/3` or
  `from_peers/2`, which `validate/1` and `save/3` re-report.
  """
  @spec pending_warnings(t()) :: [Warning.t()]
  def pending_warnings(%__MODULE__{pending_warnings: pending}), do: pending

  @doc """
  Drops the pending merge warnings. Nothing else clears them — in particular a
  successful `save/3` does **not**, so `save` then `validate` reports the same
  set twice rather than a shrinking one.
  """
  @spec clear_warnings(t()) :: t()
  def clear_warnings(%__MODULE__{} = config), do: %{config | pending_warnings: []}

  # ------------------------------------------------------------------
  # merge_peers / from_peers
  # ------------------------------------------------------------------

  @merge_defaults %{
    max_roots: 8,
    mode: :replace,
    dedupe: true,
    keep_existing: [],
    exclude: []
  }

  @doc """
  Builds a **new** config from peers (peer structs, decoded maps with an `id`,
  or bare IPv4 strings).

  `opts[:chain]` is required and is never guessed: a `chain` that disagrees with
  the node's `--chain` stops the node from starting. `try_new_peers` defaults to
  `true` here. Everything else is `merge_peers/3`.

      iex> {:ok, config, []} = HlPeers.GossipConfig.from_peers(
      ...>   [%{"id" => "192.0.2.10"}], chain: "Mainnet", try_new_peers: true)
      iex> HlPeers.GossipConfig.root_ips(config)
      ["192.0.2.10"]
  """
  @spec from_peers([term()], keyword()) :: {:ok, t(), [Warning.t()]} | {:error, Error.t()}
  def from_peers(peers, opts \\ []) do
    case Keyword.get(opts, :chain) do
      nil ->
        {:error,
         Error.invalid(
           "chain",
           "from_peers needs an explicit chain: this library will not default it to Mainnet, " <>
             "because a chain that disagrees with the node's --chain stops the node from starting"
         )}

      chain ->
        base = new(chain, [])
        try_new = Keyword.get(opts, :try_new_peers, true)

        base =
          if is_nil(try_new), do: base, else: set_try_new_peers(base, try_new)

        merge_peers(base, peers, Keyword.put(opts, :mode, :replace))
    end
  end

  @doc "Raising `from_peers/2`; returns `{config, warnings}`."
  @spec from_peers!([term()], keyword()) :: {t(), [Warning.t()]}
  def from_peers!(peers, opts \\ []) do
    case from_peers(peers, opts) do
      {:ok, config, warnings} -> {config, warnings}
      {:error, error} -> raise error
    end
  end

  @doc """
  Folds peers into an existing config's `root_node_ips`.

  | opt | default | meaning |
  |---|---|---|
  | `:max_roots` | `8` | hard cap on the resulting list |
  | `:mode` | `:replace` | `:replace` \\| `:append` \\| `:prepend` |
  | `:dedupe` | `true` | drop repeats, first occurrence wins |
  | `:keep_existing` | `[]` | IPs the operator pins; always retained, placed first |
  | `:exclude` | `[]` | IPs never to include, applied last and to everything |

  The algorithm is deterministic and order-preserving: **the client never
  re-ranks**, because the API already ranked by `sort`. A candidate that is not
  a valid IPv4 is an error, not a silent drop. 🪤 `exclude` beats
  `keep_existing`, and the conflict is surfaced as a warning naming the IP.

  🔴 If the result is empty the zero-roots error is raised and nothing is
  written — never a fallback to the previous roots.

  Why `max_roots` defaults to **8** rather than the upstream contract's ≤ 24
  (D-D): a shorter root list starts the node faster, and a longer one does not
  buy better peers. Pass `max_roots: 24` for the upstream default.
  """
  @spec merge_peers(t(), [term()], keyword()) :: {:ok, t(), [Warning.t()]} | {:error, Error.t()}
  def merge_peers(%__MODULE__{} = config, peers, opts \\ []) do
    opts = Map.merge(@merge_defaults, Map.new(opts))

    with {:ok, candidates} <- candidate_ips(peers),
         {:ok, keep} <- candidate_ips(opts.keep_existing),
         {:ok, exclude} <- candidate_ips(opts.exclude) do
      existing = root_ips(config)

      merged =
        case opts.mode do
          :replace -> keep ++ candidates
          :append -> keep ++ existing ++ candidates
          :prepend -> keep ++ candidates ++ existing
          other -> {:bad_mode, other}
        end

      case merged do
        {:bad_mode, other} ->
          {:error,
           Error.invalid("mode", "#{inspect(other)} is not one of :replace, :append, :prepend")}

        merged ->
          finish_merge(config, merged, keep, exclude, opts)
      end
    end
  end

  @doc "Raising `merge_peers/3`; returns `{config, warnings}`."
  @spec merge_peers!(t(), [term()], keyword()) :: {t(), [Warning.t()]}
  def merge_peers!(config, peers, opts \\ []) do
    case merge_peers(config, peers, opts) do
      {:ok, merged, warnings} -> {merged, warnings}
      {:error, error} -> raise error
    end
  end

  defp finish_merge(config, merged, keep, exclude, opts) do
    kept_but_excluded = Enum.filter(keep, &(&1 in exclude))
    after_exclude = Enum.reject(merged, &(&1 in exclude))
    deduped = if opts.dedupe, do: Enum.uniq(after_exclude), else: after_exclude
    final = Enum.take(deduped, opts.max_roots)
    dropped = length(deduped) - length(final)

    warnings =
      Enum.concat([
        if(kept_but_excluded == [],
          do: [],
          else: [Warning.new(:exclude_overrides_keep_existing, kept_but_excluded)]
        ),
        if(dropped > 0, do: [Warning.new(:truncated, dropped)], else: [])
      ])

    if final == [] do
      {:error,
       Error.invalid(
         "root_node_ips",
         @empty_roots_reason <> "; the filter matched no peers — widen it, or pass keep_existing"
       )}
    else
      merged_config =
        config
        |> put("root_node_ips", Enum.map(final, &root_entry/1))
        # The next merge REPLACES the pending set; it never appends to it.
        |> Map.put(:pending_warnings, warnings)

      # §6.5(6): dedupe: false is only usable on already-distinct input, and
      # validate — not a silent repair — is what says so.
      case validate(merged_config) do
        {:ok, _all} -> {:ok, merged_config, warnings}
        {:error, error} -> {:error, error}
      end
    end
  end

  # 🔴 An invalid candidate is an error, never a silent drop — dropping would
  # shrink the root list without telling the operator. The offending entry is
  # named `peers[<n>]` so the caller can find it in what they passed rather than
  # in a config it never reached; all four clients spell it that way.
  defp candidate_ips(peers) do
    peers
    |> Enum.with_index()
    |> Enum.reduce_while({:ok, []}, fn {peer, index}, {:ok, acc} ->
      case peer_ip(peer, index) do
        {:ok, ip} -> {:cont, {:ok, [ip | acc]}}
        {:error, error} -> {:halt, {:error, error}}
      end
    end)
    |> case do
      {:ok, acc} -> {:ok, Enum.reverse(acc)}
      other -> other
    end
  end

  defp peer_ip(%{__struct__: _} = peer, n), do: peer |> Map.get(:id) |> validated_ip(n)
  defp peer_ip(%{"id" => id}, n), do: validated_ip(id, n)
  defp peer_ip(%{id: id}, n), do: validated_ip(id, n)
  defp peer_ip(%{"Ip" => ip}, n), do: validated_ip(ip, n)
  defp peer_ip(ip, n) when is_binary(ip), do: validated_ip(ip, n)
  defp peer_ip(other, n), do: {:error, Error.invalid("peers[#{n}]", IP.reason(other))}

  defp validated_ip(ip, n) do
    if IP.ipv4?(ip),
      do: {:ok, ip},
      else: {:error, Error.invalid("peers[#{n}]", IP.reason(ip))}
  end

  # ------------------------------------------------------------------
  # save
  # ------------------------------------------------------------------

  @doc """
  Validates, renders and writes the file — atomically, and only when the bytes
  would actually change.

  > 🔴 The node reads `override_gossip_config.json` **when it starts**. Writing it does not steer a running node; the change takes effect at the node's next start. `save/3` never restarts, signals or reloads anything —
  > that is the operator's decision, at the operator's moment.

  Options:

  * `:dry_run` (default `false`) — write nothing, return the unified diff;
  * `:backup` (default `false`) — copy the target to
    `<path>.<UTC %Y%m%dT%H%M%SZ>.bak` first, preserving mode. This is the only
    place a clock is read, and `:now` (a `DateTime`) injects it for tests;
  * `:file_mode` — the mode for a **new** file; defaults to `0o600`. An existing
    file's mode is preserved and never widened. 🔴 It is `:file_mode`, never
    `:mode`: `:mode` is `merge_peers/3`'s strategy (`:replace` / `:append` /
    `:prepend`), and the two travel in one options list through
    `HlPeers.update_gossip_config/4` (§6.6).

  Order of operations is normative: validate (nothing is written on error) →
  render → no-op if identical → dry-run diff → backup → temp file **in the same
  directory**, `fsync`, atomic rename.
  """
  @spec save(t(), Path.t(), keyword()) :: {:ok, SaveResult.t()} | {:error, Error.t()}
  def save(%__MODULE__{} = config, path, opts \\ []) do
    # 🔴 Step 0: follow a symlinked path to its target, so the atomic rename
    # replaces the file the operator pointed at rather than the link itself.
    with {:ok, warnings} <- validate(config),
         {:ok, target} <- resolve_symlink(path) do
      rendered = to_json(config)
      existing = read_existing(target)

      cond do
        existing == rendered ->
          {:ok,
           %SaveResult{
             written: false,
             changed: false,
             path: path,
             resolved_path: target,
             warnings: warnings
           }}

        Keyword.get(opts, :dry_run, false) ->
          {:ok,
           %SaveResult{
             written: false,
             changed: true,
             path: path,
             resolved_path: target,
             diff: HlPeers.Diff.unified(existing || "", rendered, path),
             warnings: warnings
           }}

        true ->
          write(path, target, rendered, existing, warnings, opts)
      end
    end
  end

  @symlink_depth 32

  # 🔴 `save/3` replaces the **target**, never the link: renaming over a symlink
  # swaps an operator's deliberate arrangement for a regular file, and from that
  # moment the node and their tooling read two different files with nothing to
  # announce it.
  defp resolve_symlink(path), do: resolve_symlink(path, path, @symlink_depth)

  defp resolve_symlink(original, _current, 0) do
    {:error,
     Error.invalid(
       "path",
       "#{original} resolves through more than #{@symlink_depth} symbolic links; " <>
         "refusing to follow further"
     )}
  end

  defp resolve_symlink(original, current, fuel) do
    case File.read_link(current) do
      {:error, _} ->
        {:ok, current}

      {:ok, link} ->
        next =
          if Path.type(link) == :absolute,
            do: link,
            else: Path.expand(link, Path.dirname(current))

        if File.exists?(next) or File.dir?(Path.dirname(next)) do
          resolve_symlink(original, next, fuel - 1)
        else
          {:error,
           Error.invalid(
             "path",
             "#{original} is a symbolic link to #{next}, whose directory does not exist; " <>
               "refusing to create one rather than guess at the operator's layout"
           )}
        end
    end
  end

  @doc "Raising `save/3`."
  @spec save!(t(), Path.t(), keyword()) :: SaveResult.t()
  def save!(config, path, opts \\ []) do
    case save(config, path, opts) do
      {:ok, result} -> result
      {:error, error} -> raise error
    end
  end

  defp read_existing(path) do
    case File.read(path) do
      {:ok, bytes} -> bytes
      {:error, _} -> nil
    end
  end

  defp write(path, target, rendered, existing, warnings, opts) do
    with {:ok, backup_path} <- backup(target, existing, opts),
         :ok <- atomic_write(target, rendered, existing, opts) do
      {:ok,
       %SaveResult{
         written: true,
         changed: true,
         path: path,
         resolved_path: target,
         diff: HlPeers.Diff.unified(existing || "", rendered, path),
         backup_path: backup_path,
         warnings: warnings
       }}
    else
      {:error, %Error{} = error} -> {:error, error}
      {:error, reason} -> {:error, Error.new(:transport, cause: reason, path: path)}
    end
  end

  defp backup(_path, nil, _opts), do: {:ok, nil}

  defp backup(path, _existing, opts) do
    if Keyword.get(opts, :backup, false) do
      now = Keyword.get(opts, :now) || DateTime.utc_now()
      stamp = Calendar.strftime(now, "%Y%m%dT%H%M%SZ")
      target = "#{path}.#{stamp}.bak"

      with :ok <- File.cp(path, target),
           {:ok, %File.Stat{mode: mode}} <- File.stat(path),
           :ok <- File.chmod(target, mode &&& 0o7777) do
        {:ok, target}
      end
    else
      {:ok, nil}
    end
  end

  defp atomic_write(path, rendered, existing, opts) do
    dir = Path.dirname(path)
    rand = 8 |> :crypto.strong_rand_bytes() |> Base.url_encode64(padding: false)
    tmp = Path.join(dir, ".#{Path.basename(path)}.#{rand}.tmp")

    mode =
      case existing && File.stat(path) do
        {:ok, %File.Stat{mode: m}} -> m &&& 0o7777
        _ -> new_file_mode(opts)
      end

    try do
      with {:ok, fd} <- File.open(tmp, [:write, :binary]),
           :ok <- IO.binwrite(fd, rendered),
           :ok <- :file.sync(fd),
           :ok <- File.close(fd),
           :ok <- File.chmod(tmp, mode),
           :ok <- File.rename(tmp, path) do
        :ok
      else
        {:error, reason} ->
          _ = File.rm(tmp)
          {:error, reason}
      end
    rescue
      e ->
        _ = File.rm(tmp)
        {:error, Error.new(:transport, cause: e, path: path)}
    end
  end

  # ------------------------------------------------------------------
  # helpers
  # ------------------------------------------------------------------

  # 🔴 §6.6: the permission-bits option is `:file_mode`. `:mode` belongs to
  # merge_peers/3's strategy and to nothing else — the two would collide the
  # moment update_gossip_config/4 takes a single options bag.
  defp new_file_mode(opts), do: Keyword.get(opts, :file_mode, 0o600)

  defp entries(%__MODULE__{data: data}), do: data |> Map.get("root_node_ips") |> List.wrap()

  defp root_entry(%{"Ip" => _} = entry), do: entry
  defp root_entry(%{"ip" => ip}), do: %{"Ip" => ip}
  defp root_entry(ip) when is_binary(ip), do: %{"Ip" => ip}
  defp root_entry(other), do: other

  defp entry_ip(%{"Ip" => ip}), do: ip
  defp entry_ip(other), do: other

  defp uniq_entries(entries), do: Enum.uniq_by(entries, &entry_ip/1)

  defp put(%__MODULE__{data: data} = config, key, nil),
    do: %{config | data: Map.delete(data, key)}

  defp put(%__MODULE__{data: data} = config, key, value),
    do: %{config | data: Map.put(data, key, value)}
end

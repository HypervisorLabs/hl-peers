defmodule HlPeers.Store do
  @moduledoc """
  The per-client mutable corner: the ETag cache (`CLIENT_SPEC.md` §4.5).

  The cache is **per client instance, never global, never on disk**. It is an
  `Agent` started by `HlPeers.Client.new/1` and linked to whoever called it,
  so it lives exactly as long as the code that owns the client. Nothing else in
  this library keeps state, and `HlPeers.GossipConfig` never touches this
  module at all.

  Keys are `method \\0 path \\0 canonical_query_string \\0 ("k" | "a")` — the
  key includes **whether an API key was sent**, because the tier changes the
  page ceiling and a cached anonymous body must not be served to a keyed call.
  """

  use Agent

  @type entry :: %{etag: String.t(), body: binary(), stored_at: integer()}

  @doc false
  @spec start_link(keyword()) :: {:ok, pid()} | {:error, term()}
  def start_link(opts \\ []) do
    capacity = Keyword.get(opts, :capacity, 64)
    Agent.start_link(fn -> %{entries: %{}, order: [], capacity: capacity} end)
  end

  @doc "The cache key of §4.5."
  @spec key(String.t(), String.t(), String.t(), boolean()) :: String.t()
  def key(method, path, query_string, keyed?) do
    Enum.join([method, path, query_string, if(keyed?, do: "k", else: "a")], <<0>>)
  end

  @doc "The stored entry for a key, or `nil`."
  @spec fetch(pid() | nil, String.t()) :: entry() | nil
  def fetch(nil, _key), do: nil
  def fetch(pid, key), do: Agent.get(pid, & &1.entries[key])

  @doc "Stores a body under its ETag, evicting the least recently used past capacity."
  @spec put(pid() | nil, String.t(), String.t(), binary()) :: :ok
  def put(nil, _key, _etag, _body), do: :ok

  def put(pid, key, etag, body) do
    Agent.update(pid, fn state ->
      entry = %{etag: etag, body: body, stored_at: System.monotonic_time(:millisecond)}
      order = [key | Enum.reject(state.order, &(&1 == key))]
      {keep, drop} = Enum.split(order, state.capacity)

      %{
        state
        | entries: entry |> then(&Map.put(state.entries, key, &1)) |> Map.drop(drop),
          order: keep
      }
    end)
  end

  @doc "Marks a key as most recently used."
  @spec touch(pid() | nil, String.t()) :: :ok
  def touch(nil, _key), do: :ok

  def touch(pid, key) do
    Agent.update(pid, fn state ->
      %{state | order: [key | Enum.reject(state.order, &(&1 == key))]}
    end)
  end

  @doc "Empties the cache."
  @spec clear(pid() | nil) :: :ok
  def clear(nil), do: :ok
  def clear(pid), do: Agent.update(pid, &%{&1 | entries: %{}, order: []})
end

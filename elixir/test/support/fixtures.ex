defmodule HlPeers.Test.Fixtures do
  @moduledoc """
  Loads the monorepo's language-neutral fixtures from `../fixtures`.

  Nothing here is Elixir-specific: the same files drive the TypeScript, Rust and
  Python suites, which is how four independent implementations converge without
  coordinating.
  """

  @root Path.expand("../../..", __DIR__)

  @spec root() :: Path.t()
  def root, do: @root

  @spec http() :: [map()]
  def http do
    @root
    |> Path.join("fixtures/http/*.json")
    |> Path.wildcard()
    |> Enum.sort()
    |> Enum.map(&(&1 |> File.read!() |> Jason.decode!()))
  end

  @spec http(String.t()) :: map()
  def http(name), do: Enum.find(http(), &(&1["name"] == name)) || raise("no fixture #{name}")

  @spec gossip_cases() :: [map()]
  def gossip_cases do
    @root
    |> Path.join("fixtures/gossip/cases/*.json")
    |> Path.wildcard()
    |> Enum.sort()
    |> Enum.map(&(&1 |> File.read!() |> Jason.decode!()))
  end
end

defmodule HlPeers.Test.FixtureAdapter do
  @moduledoc """
  An `HlPeers.Adapter` that replays `fixtures/http` offline, with the matching
  rules of `fixtures/README.md` and `tools/mock_server.py`:

  * method, path and the **full** decoded query must be equal;
  * a fixture naming `request.headers` matches only when every named header is
    present with that value, and **outranks** a header-free fixture for the same
    query — that is how one query has both a `200` and a `429`;
  * a request carrying `If-None-Match` equal to a matched `200` fixture's ETag
    gets a synthesised `304`, as the real API and the mock server do.

  It also records every request in an Agent, so a test can assert on the
  *decisions* — how many attempts, which headers — rather than on wall-clock
  delays.
  """

  @behaviour HlPeers.Adapter

  alias HlPeers.Test.Fixtures

  @spec start_link(keyword()) :: {:ok, pid()}
  def start_link(opts \\ []) do
    Agent.start_link(fn ->
      %{
        requests: [],
        fixtures: Keyword.get(opts, :fixtures, Fixtures.http()),
        fail_first_n: Keyword.get(opts, :fail_first_n, 0),
        count: 0
      }
    end)
  end

  @spec requests(pid()) :: [map()]
  def requests(pid), do: pid |> Agent.get(& &1.requests) |> Enum.reverse()

  @spec request_count(pid()) :: non_neg_integer()
  def request_count(pid), do: pid |> requests() |> length()

  @impl true
  def request(request, opts) do
    pid = Keyword.fetch!(opts, :recorder)

    n =
      Agent.get_and_update(pid, fn state ->
        {state.count + 1, %{state | count: state.count + 1, requests: [request | state.requests]}}
      end)

    state = Agent.get(pid, & &1)

    cond do
      n <= state.fail_first_n -> {:ok, injected_503()}
      state[:transport_error] -> {:error, state.transport_error}
      true -> serve(state.fixtures, request)
    end
  end

  @doc "Makes the next N requests answer 503, to drive a retry test."
  @spec fail_first_n(pid(), non_neg_integer()) :: :ok
  def fail_first_n(pid, n), do: Agent.update(pid, &%{&1 | fail_first_n: n, count: 0})

  @doc "Makes every request fail at the transport, to drive the transport retry test."
  @spec transport_error(pid(), term()) :: :ok
  def transport_error(pid, reason),
    do: Agent.update(pid, &Map.put(&1, :transport_error, reason))

  defp injected_503 do
    body =
      Jason.encode!(%{
        "type" => "https://example.invalid/problems/producer-unavailable",
        "title" => "Peer graph unavailable",
        "status" => 503,
        "detail" => "Injected failure."
      })

    %{status: 503, headers: [{"content-type", "application/problem+json"}], body: body}
  end

  defp serve(fixtures, request) do
    %{path: path, query: query} = split(request.url)
    headers = Map.new(request.headers, fn {k, v} -> {String.downcase(to_string(k)), v} end)

    matches =
      fixtures
      |> Enum.filter(fn fx ->
        fx["request"]["method"] == "GET" and fx["request"]["path"] == path and
          normalize(fx["request"]["query"] || %{}) == query
      end)
      |> Enum.sort_by(fn fx -> -map_size(fx["request"]["headers"] || %{}) end)
      |> Enum.filter(fn fx ->
        Enum.all?(fx["request"]["headers"] || %{}, fn {k, v} ->
          headers[String.downcase(k)] == v
        end)
      end)

    case matches do
      [] ->
        {:ok,
         %{
           status: 404,
           headers: [{"content-type", "application/problem+json"}],
           body:
             Jason.encode!(%{
               "type" => "https://example.invalid/problems/no-fixture",
               "title" => "No fixture matches this request",
               "status" => 404,
               "detail" => "GET #{path}?#{inspect(query)}"
             })
         }}

      [fixture | _] ->
        respond(fixture, headers)
    end
  end

  defp respond(fixture, request_headers) do
    response = fixture["response"]
    headers = response["headers"] || %{}
    inm = request_headers["if-none-match"]

    if inm && response["status"] == 200 && headers["ETag"] == inm do
      keep =
        headers
        |> Enum.filter(fn {k, _} ->
          k in ["ETag", "Cache-Control"] or String.starts_with?(k, "RateLimit-")
        end)
        |> Enum.map(fn {k, v} -> {String.downcase(k), v} end)

      {:ok, %{status: 304, headers: keep, body: ""}}
    else
      body =
        cond do
          Map.has_key?(response, "body_text") -> response["body_text"]
          is_nil(response["body"]) -> ""
          true -> Jason.encode!(response["body"])
        end

      {:ok,
       %{
         status: response["status"],
         headers: Enum.map(headers, fn {k, v} -> {String.downcase(k), v} end),
         body: body
       }}
    end
  end

  defp split(url) do
    uri = URI.parse(url)
    query = uri.query |> Kernel.||("") |> URI.decode_query() |> normalize()
    %{path: uri.path, query: query}
  end

  defp normalize(map), do: Map.new(map, fn {k, v} -> {to_string(k), to_string(v)} end)
end

defmodule HlPeers.Adapter.Req do
  @moduledoc """
  The default adapter, on `Req`.

  `Req`'s own retry and redirect handling are switched **off**: the retry policy
  of `CLIENT_SPEC.md` §5.2 lives in `HlPeers.Client`, which is the only place
  that knows a `429` with an over-cap `Retry-After` must raise rather than
  sleep. The body is requested raw so this client can decode it and report a
  `:decode` error with an excerpt when it is not JSON.

  `adapter_opts` are passed through to `Req.request/1`, so a caller can plug in
  `Req`'s own test stubs (`plug:`) without replacing the adapter.
  """

  @behaviour HlPeers.Adapter

  @impl true
  def request(%{method: :get, url: url, headers: headers, timeout: timeout}, opts) do
    options =
      [
        method: :get,
        url: url,
        headers: headers,
        decode_body: false,
        raw: true,
        retry: false,
        redirect: false,
        receive_timeout: timeout,
        connect_options: [timeout: timeout]
      ]
      |> Keyword.merge(opts)

    case Req.request(options) do
      {:ok, %Req.Response{status: status, headers: resp_headers, body: body}} ->
        {:ok, %{status: status, headers: flatten(resp_headers), body: to_binary(body)}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  # Req hands back a map of name => [value]; this client works in a flat list.
  defp flatten(headers) do
    Enum.flat_map(headers, fn {k, v} -> Enum.map(List.wrap(v), &{k, &1}) end)
  end

  defp to_binary(body) when is_binary(body), do: body
  defp to_binary(nil), do: ""
  defp to_binary(other), do: to_string(other)
end

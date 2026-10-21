defmodule HlPeers.Adapter do
  @moduledoc """
  The seam between this client and an HTTP library.

  The default is `HlPeers.Adapter.Req`, which works out of the box. Swapping
  in your own — or the fixture replayer the test suite uses — is how the whole
  suite runs offline:

      client = HlPeers.Client.new(adapter: MyAdapter, adapter_opts: [...])

  An adapter answers `{:ok, %{status: integer, headers: [{name, value}], body: binary}}`
  or `{:error, reason}`; the reason becomes `%HlPeers.Error{kind: :transport}`
  and is what the retry policy backs off on. The body stays a **binary**: this
  library decodes it itself, because "the body is not JSON" is a `:decode` error
  it must be able to tell you about, with an excerpt.
  """

  @type request :: %{
          method: :get,
          url: String.t(),
          headers: [{String.t(), String.t()}],
          timeout: pos_integer()
        }

  @type response :: %{
          status: non_neg_integer(),
          headers: [{String.t(), String.t()}],
          body: binary()
        }

  @callback request(request(), keyword()) :: {:ok, response()} | {:error, term()}
end

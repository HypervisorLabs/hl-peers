#!/usr/bin/env elixir
# JSON-in / JSON-out differential harness for the Elixir client.
#
# One NDJSON request per line on stdin, one NDJSON answer per line on stdout.
# `tools/cross_check/run.py` drives this and the three sibling harnesses side by
# side and asserts that all four answer identically.
#
# The harness holds no expectations of its own: it reports what the client did.
#
#     cd elixir && mix run cross_check/harness.exs < programmes.ndjson

defmodule HlPeers.CrossCheck.Harness do
  @moduledoc false

  alias HlPeers.{
    Envelope,
    Error,
    Facet,
    Facets,
    GossipConfig,
    GossipConfigResult,
    NetworkMap,
    NotModified,
    Peer,
    PeerEnvelope,
    PeerList,
    Query,
    RateLimit
  }

  alias HlPeers.GossipConfig.SaveResult

  @base "https://api.hypervisor.gg"

  # 🔴 `Error.kind` is an atom; `CLIENT_SPEC.md` §5.1 names it in CamelCase and
  # that name — not the atom — is what the four harnesses compare.
  @kind_names %{
    rate_limited: "RateLimited",
    filter_unavailable: "FilterUnavailable",
    chain_not_served: "ChainNotServed",
    cursor_expired: "CursorExpired",
    bad_request: "BadRequest",
    unauthorized: "Unauthorized",
    not_found: "NotFound",
    unavailable: "Unavailable",
    server: "Server",
    transport: "Transport",
    decode: "Decode",
    invalid: "Invalid",
    truncated: "Truncated"
  }

  @bool_fields ~w(accepting try_new_peers)
  @int_fields ~w(max_tier max_blocks_behind max_latency_ms limit)
  @float_fields ~w(min_uptime)

  @merge_opts %{
    "max_roots" => :max_roots,
    "mode" => :mode,
    "dedupe" => :dedupe,
    "keep_existing" => :keep_existing,
    "exclude" => :exclude,
    "chain" => :chain,
    "try_new_peers" => :try_new_peers
  }

  @save_opts %{"dry_run" => :dry_run, "backup" => :backup, "file_mode" => :file_mode}

  @modes %{"replace" => :replace, "append" => :append, "prepend" => :prepend}

  @default_max_items 10_000

  # ------------------------------------------------------------------
  # the loop
  # ------------------------------------------------------------------

  def main do
    # No client in this process may read the real environment (§1): the driver
    # already strips these, and this is the belt to that braces.
    System.delete_env("HL_PEER_API_BASE_URL")
    System.delete_env("HL_PEER_API_KEY")
    _ = :io.setopts(:standard_io, encoding: :unicode)

    :stdio
    |> IO.stream(:line)
    |> Enum.each(&handle_line/1)
  end

  defp handle_line(line) do
    case String.trim(line) do
      "" -> :ok
      text -> text |> decode_request() |> answer() |> emit()
    end
  end

  defp decode_request(text) do
    case Jason.decode(text) do
      {:ok, request} when is_map(request) -> request
      {:ok, other} -> %{"id" => nil, "kind" => nil, "__bad__" => inspect(other)}
      {:error, error} -> %{"id" => nil, "kind" => nil, "__bad__" => Exception.message(error)}
    end
  end

  defp answer(%{"__bad__" => message} = request),
    do: Map.put(harness_error(message), "id", request["id"])

  defp answer(request) do
    answer =
      try do
        run(request)
      rescue
        error -> harness_error(describe_exception(error))
      catch
        kind, reason -> harness_error("#{kind}: #{inspect(reason)}")
      end

    Map.put(answer, "id", request["id"])
  end

  defp run(%{"kind" => "gossip"} = request), do: run_gossip(request)
  defp run(%{"kind" => "query_url"} = request), do: run_query_url(request)
  defp run(%{"kind" => "http"} = request), do: run_http(request)
  defp run(%{"kind" => kind}), do: harness_error("unknown kind #{inspect(kind)}")
  defp run(_request), do: harness_error("request has no kind")

  defp emit(answer), do: IO.write(:stdio, [Jason.encode!(answer), "\n"])

  defp harness_error(message), do: %{"ok" => false, "harness_error" => to_string(message)}

  defp describe_exception(%{__struct__: module} = error),
    do: "#{inspect(module)}: #{Exception.message(error)}"

  defp describe_exception(other), do: inspect(other)

  # ------------------------------------------------------------------
  # errors
  # ------------------------------------------------------------------

  defp error_payload(error, at_op \\ nil)

  defp error_payload(%Error{} = error, at_op) do
    %{"kind" => Map.fetch!(@kind_names, error.kind)}
    |> put_present("parameter", error.parameter)
    |> put_present("filter", error.filter)
    |> put_present("retry_after", error.retry_after)
    |> put_present("seen", error.seen)
    # `status` is deliberately not reported: CLIENT_SPEC §5.1 puts it on the
    # `Server` variant alone, so exposing it elsewhere is a per-client accident.
    |> put_present("at_op", at_op)
  end

  defp put_present(map, _key, nil), do: map
  defp put_present(map, key, value), do: Map.put(map, key, value)

  # ------------------------------------------------------------------
  # gossip
  # ------------------------------------------------------------------

  defp run_gossip(request) do
    tmp =
      Path.join(
        System.tmp_dir!(),
        "hlp-xcheck-ex-#{:erlang.unique_integer([:positive])}"
      )

    File.mkdir_p!(tmp)

    try do
      run_gossip(request, tmp)
    after
      File.rm_rf(tmp)
    end
  end

  defp run_gossip(request, tmp) do
    case build_subject(request) do
      {:error, %Error{} = error} ->
        %{"ok" => false, "error" => error_payload(error, -1)}

      {:ok, subject} ->
        state = %{subject: subject, value: nil, op_warnings: nil, saved_text: nil}

        case apply_ops(state, request["operations"] || [], tmp) do
          {:ok, state} -> gossip_answer(state, tmp)
          {:error, error, index} -> %{"ok" => false, "error" => error_payload(error, index)}
          {:harness_error, message} -> harness_error(message)
        end
    end
  end

  defp build_subject(%{"input_json_text" => text}) when is_binary(text),
    do: GossipConfig.parse(text)

  defp build_subject(%{"input_config" => config}) when is_map(config),
    do: config |> Jason.encode!() |> GossipConfig.parse()

  defp build_subject(_request), do: {:ok, nil}

  defp gossip_answer(state, tmp) do
    answer = %{
      "ok" => true,
      "value" => unsubst(state.value, tmp),
      "json_text" => state.subject && GossipConfig.to_json(state.subject),
      "op_warnings" => state.op_warnings,
      "saved_text" => state.saved_text
    }

    final_warnings(answer, state.subject)
  end

  defp final_warnings(answer, nil), do: Map.put(answer, "warnings", nil)

  defp final_warnings(answer, subject) do
    case GossipConfig.validate(subject) do
      {:ok, warnings} ->
        Map.put(answer, "warnings", codes(warnings))

      {:error, %Error{} = error} ->
        answer
        |> Map.put("warnings", nil)
        |> Map.put("validate_error", error_payload(error))
    end
  end

  defp codes(warnings) do
    warnings |> Enum.map(&Atom.to_string(&1.code)) |> Enum.sort()
  end

  defp apply_ops(state, operations, tmp) do
    operations
    |> Enum.with_index()
    |> Enum.reduce_while({:ok, state}, fn {operation, index}, {:ok, acc} ->
      args = subst(operation["args"] || [], tmp)
      opts = subst(operation["opts"] || %{}, tmp)

      case safe_dispatch(acc, operation["op"], args, opts) do
        {:ok, next} -> {:cont, {:ok, next}}
        {:error, %Error{} = error} -> {:halt, {:error, error, index}}
        {:harness_error, message} -> {:halt, {:harness_error, message}}
      end
    end)
  end

  defp safe_dispatch(state, op, args, opts) do
    dispatch(state, op, args, opts)
  rescue
    error in [Error] -> {:error, error}
    error -> {:harness_error, describe_exception(error)}
  end

  # -- constructors ---------------------------------------------------

  defp dispatch(state, "new", args, _opts) do
    {chain, ips} =
      case args do
        [chain, ips] -> {chain, ips}
        [chain] -> {chain, []}
        [] -> {nil, []}
      end

    {:ok, %{state | subject: GossipConfig.new(chain, ips), value: nil}}
  end

  defp dispatch(state, "from_peers", [peers], opts) do
    with {:ok, keyword} <- merge_keyword(opts),
         {:ok, config, warnings} <- GossipConfig.from_peers(peers, keyword) do
      {:ok, %{state | subject: config, value: nil, op_warnings: codes(warnings)}}
    end
  end

  defp dispatch(state, "merge_peers", [peers], opts) do
    with {:ok, keyword} <- merge_keyword(opts),
         {:ok, config, warnings} <- GossipConfig.merge_peers(state.subject, peers, keyword) do
      {:ok, %{state | subject: config, value: nil, op_warnings: codes(warnings)}}
    end
  end

  # -- save / validate ------------------------------------------------

  defp dispatch(state, "save", [path | _], opts) do
    with {:ok, keyword} <- save_keyword(opts),
         {:ok, %SaveResult{} = result} <- GossipConfig.save(state.subject, path, keyword) do
      value = %{
        "written" => result.written,
        "changed" => result.changed,
        "diff" => result.diff,
        "backup" => not is_nil(result.backup_path)
      }

      {:ok,
       %{
         state
         | value: value,
           op_warnings: codes(result.warnings),
           saved_text: read_if_exists(path)
       }}
    end
  end

  defp dispatch(state, "validate", _args, _opts) do
    with {:ok, warnings} <- GossipConfig.validate(state.subject) do
      {:ok, %{state | value: "ok", op_warnings: codes(warnings)}}
    end
  end

  # -- accessors ------------------------------------------------------

  defp dispatch(state, "to_json", _args, _opts),
    do: {:ok, %{state | value: GossipConfig.to_json(state.subject)}}

  defp dispatch(state, "root_ips", _args, _opts),
    do: {:ok, %{state | value: GossipConfig.root_ips(state.subject)}}

  defp dispatch(state, "reserved_peer_ips", _args, _opts),
    do: {:ok, %{state | value: GossipConfig.reserved_peer_ips(state.subject)}}

  defp dispatch(state, "has_root_ip", [ip], _opts),
    do: {:ok, %{state | value: GossipConfig.has_root_ip?(state.subject, ip)}}

  # -- mutators (config-returning: the reported value is null) ---------

  defp dispatch(state, "add_root_ip", [ip], _opts),
    do: mutated(state, GossipConfig.add_root_ip(state.subject, ip))

  defp dispatch(state, "remove_root_ip", [ip], _opts),
    do: mutated(state, GossipConfig.remove_root_ip(state.subject, ip))

  defp dispatch(state, "set_root_ips", [ips], _opts),
    do: mutated(state, GossipConfig.set_root_ips(state.subject, ips))

  defp dispatch(state, "add_reserved_peer_ip", [ip], _opts),
    do: mutated(state, GossipConfig.add_reserved_peer_ip(state.subject, ip))

  defp dispatch(state, "remove_reserved_peer_ip", [ip], _opts),
    do: mutated(state, GossipConfig.remove_reserved_peer_ip(state.subject, ip))

  defp dispatch(state, "set_reserved_peer_ips", [ips], _opts),
    do: mutated(state, GossipConfig.set_reserved_peer_ips(state.subject, ips))

  defp dispatch(state, "set_chain", [chain], _opts),
    do: mutated(state, GossipConfig.set_chain(state.subject, chain))

  defp dispatch(state, "set_try_new_peers", [value], _opts),
    do: mutated(state, GossipConfig.set_try_new_peers(state.subject, value))

  defp dispatch(state, "set_n_gossip_peers", [value], _opts) do
    with {:ok, config} <- GossipConfig.set_n_gossip_peers(state.subject, value),
         do: mutated(state, config)
  end

  defp dispatch(state, "set_split_client_blocks", [value], _opts),
    do: mutated(state, GossipConfig.set_split_client_blocks(state.subject, value))

  defp dispatch(state, "unset_split_client_blocks", _args, _opts),
    do: mutated(state, GossipConfig.unset_split_client_blocks(state.subject))

  defp dispatch(_state, op, args, _opts),
    do: {:harness_error, "unknown operation #{inspect(op)}/#{length(args)}"}

  defp mutated(state, config), do: {:ok, %{state | subject: config, value: nil}}

  defp read_if_exists(path) do
    case File.read(path) do
      {:ok, text} -> text
      {:error, _reason} -> nil
    end
  end

  defp merge_keyword(opts), do: keyword(opts, @merge_opts, "merge")
  defp save_keyword(opts), do: keyword(opts, @save_opts, "save")

  defp keyword(opts, allowed, label) do
    Enum.reduce_while(opts, {:ok, []}, fn {key, value}, {:ok, acc} ->
      case Map.fetch(allowed, key) do
        {:ok, :mode} -> {:cont, {:ok, [{:mode, mode(value)} | acc]}}
        {:ok, atom} -> {:cont, {:ok, [{atom, value} | acc]}}
        :error -> {:halt, {:harness_error, "unknown #{label} option #{inspect(key)}"}}
      end
    end)
  end

  # An unknown spelling is left as-is so `merge_peers/3` reports it, rather than
  # the harness inventing an error the other three clients do not raise.
  defp mode(value) when is_binary(value), do: Map.get(@modes, value, value)
  defp mode(value), do: value

  # ------------------------------------------------------------------
  # $TMPDIR substitution
  # ------------------------------------------------------------------

  defp subst(value, tmp) when is_binary(value), do: String.replace(value, "$TMPDIR", tmp)
  defp subst(value, tmp) when is_list(value), do: Enum.map(value, &subst(&1, tmp))

  defp subst(value, tmp) when is_map(value),
    do: Map.new(value, fn {k, v} -> {k, subst(v, tmp)} end)

  defp subst(value, _tmp), do: value

  defp unsubst(value, tmp) when is_binary(value), do: String.replace(value, tmp, "$TMPDIR")
  defp unsubst(value, tmp) when is_list(value), do: Enum.map(value, &unsubst(&1, tmp))

  defp unsubst(value, tmp) when is_map(value),
    do: Map.new(value, fn {k, v} -> {k, unsubst(v, tmp)} end)

  defp unsubst(value, _tmp), do: value

  # ------------------------------------------------------------------
  # query_url
  # ------------------------------------------------------------------

  defp run_query_url(%{"preset" => preset}) do
    case preset_query(preset) do
      {:ok, query} -> query_answer(query)
      {:harness_error, message} -> harness_error(message)
    end
  end

  defp run_query_url(request) do
    case build_query(request["query"] || %{}) do
      {:ok, query} -> query_answer(query)
      {:harness_error, message} -> harness_error(message)
    end
  end

  defp query_answer(query) do
    case Query.validate(query) do
      :ok ->
        qs = Query.to_query_string(query)

        %{
          "ok" => true,
          "url" => @base <> "/peers" <> if(qs == "", do: "", else: "?" <> qs)
        }

      {:error, %Error{} = error} ->
        %{"ok" => false, "error" => error_payload(error)}
    end
  end

  defp preset_query("tier1"), do: {:ok, Query.tier1()}
  defp preset_query("sentries"), do: {:ok, Query.sentries()}
  defp preset_query("not_full_cc"), do: {:ok, Query.not_full(cc: ["SG"])}
  defp preset_query("mempool_servers"), do: {:ok, Query.mempool_servers()}
  defp preset_query(other), do: {:harness_error, "unknown preset #{inspect(other)}"}

  defp build_query(spec) do
    case split_query(spec) do
      {query, []} -> {:ok, query}
      {_query, [{name, _} | _]} -> {:harness_error, "unknown query field #{inspect(name)}"}
    end
  end

  # The builder models exactly the published parameters. A name it does not
  # know (the removed `mempool=` filter, which the server answers
  # `400 filter-removed`) is returned separately so the http family can send it
  # raw through `HlPeers.Client.get/4` — the transport's escape hatch.
  defp split_query(spec) do
    fields = Map.new(Query.fields(), fn field -> {Atom.to_string(field), field} end)

    spec
    |> Enum.sort_by(fn {name, _} -> name end)
    |> Enum.reduce({Query.new(), []}, fn {name, value}, {query, extra} ->
      case Map.fetch(fields, name) do
        {:ok, field} -> {Query.put(query, field, coerce(name, value)), extra}
        :error -> {query, extra ++ [{name, to_string(value)}]}
      end
    end)
  end

  # Values arrive either as JSON lists or as pre-joined wire strings; §2.1 of
  # CLIENT_SPEC.md requires a CSV field to accept both, so a list is passed
  # through untouched and only the scalars are coerced off their wire spelling.
  defp coerce(name, value) when name in @bool_fields and is_binary(value), do: value == "true"
  defp coerce(name, value) when name in @int_fields, do: to_integer(value)
  defp coerce(name, value) when name in @float_fields, do: to_float(value)
  defp coerce(_name, value), do: value

  defp to_integer(value) when is_integer(value), do: value
  defp to_integer(value) when is_float(value), do: trunc(value)

  defp to_integer(value) when is_binary(value) do
    case Integer.parse(value) do
      {integer, ""} -> integer
      _ -> value
    end
  end

  defp to_integer(value), do: value

  defp to_float(value) when is_float(value), do: value
  defp to_float(value) when is_integer(value), do: value * 1.0

  defp to_float(value) when is_binary(value) do
    case Float.parse(value) do
      {float, ""} -> float
      _ -> value
    end
  end

  defp to_float(value), do: value

  # ------------------------------------------------------------------
  # http
  # ------------------------------------------------------------------

  defp run_http(request) do
    client = client(request)

    case request["op"] do
      op when op in ["network_map_graph", "network_map_summary"] ->
        network_map_op(op, request, client)

      _ ->
        {query, extra} = split_query(request["query"] || %{})
        http_op(request, client, query, extra)
    end
  end

  defp client(request) do
    opts = [
      base_url: request["base_url"],
      sleep_fun: fn _ms -> :ok end,
      rand_fun: fn -> 0.0 end
    ]

    opts = if request["api_key"], do: Keyword.put(opts, :api_key, request["api_key"]), else: opts

    # Absent ⇒ the client default (0: auto-retry is opt-in).
    opts =
      case request["max_retries"] do
        nil -> opts
        n -> Keyword.put(opts, :max_retries, to_integer(n))
      end

    case clock(request["clock_now"]) do
      nil -> HlPeers.Client.new(opts)
      moment -> HlPeers.Client.new(Keyword.put(opts, :clock_fun, fn -> moment end))
    end
  end

  defp clock(nil), do: nil

  # `DateTime.from_iso8601/1` already answers in UTC whatever offset was written.
  defp clock(text) do
    case DateTime.from_iso8601(text) do
      {:ok, moment, _offset} -> moment
      _ -> nil
    end
  end

  defp http_op(request, client, query, [_ | _] = extra) do
    case request["op"] do
      "list_peers" -> raw_list_peers(request, client, query, extra)
      other -> harness_error("op #{inspect(other)} cannot carry #{inspect(extra)}")
    end
  end

  defp http_op(request, client, query, []) do
    case request["op"] do
      "list_peers" -> peers_answer(HlPeers.list_peers(client, query), request)
      "list_peers_twice" -> list_peers_twice(client, query, request)
      "get_peer" -> peer_answer(HlPeers.get_peer(client, request["ip"]), request)
      "facets" -> facets_answer(HlPeers.facets(client, query), request)
      "iter_peers" -> iter_answer(client, query, request)
      "list_peers_as_gossip_config" -> gossip_config_answer(client, query, request)
      other -> harness_error("unknown op #{inspect(other)}")
    end
  end

  # A parameter the builder does not model, sent raw. Only the error path is
  # reachable today (`400 filter-removed`); a 200 is decoded like a page.
  defp raw_list_peers(request, client, query, extra) do
    params = Enum.sort_by(Query.to_params(query) ++ extra, &elem(&1, 0))

    case HlPeers.Client.get(client, client.base_path <> "/peers", params) do
      {:error, %Error{} = error} ->
        http_error(error, request)

      {:ok, response} ->
        body = Jason.decode!(response.body)

        peers_answer(
          {:ok,
           %PeerList{
             peers: Enum.map(body["peers"] || [], &Peer.from_map/1),
             count: body["count"],
             total: body["total"],
             next_cursor: body["next_cursor"],
             envelope: Envelope.from_body(body),
             rate_limit: response.rate_limit,
             api_version: response.api_version,
             etag: response.etag,
             from_cache: response.from_cache,
             status: response.status
           }},
          request
        )
    end
  end

  defp list_peers_twice(client, query, request) do
    case HlPeers.list_peers(client, query) do
      {:error, %Error{}} = failed -> peers_answer(failed, request)
      {:ok, _first} -> peers_answer(HlPeers.list_peers(client, query), request)
    end
  end

  defp peers_answer({:ok, %NotModified{} = result}, _request) do
    %{
      "ok" => true,
      "status" => result.status,
      "not_modified" => true,
      "etag" => result.etag,
      "rate_limit" => rate_limit_payload(result.rate_limit),
      "api_version" => result.api_version
    }
  end

  defp peers_answer({:ok, %PeerList{} = page}, _request) do
    %{
      "ok" => true,
      "status" => page.status,
      "peer_ids" => Enum.map(page.peers, & &1.id),
      "signals" => signals(page.peers),
      "rows" => rows(page.peers),
      "count" => page.count,
      "total" => page.total,
      "next_cursor" => page.next_cursor,
      "envelope" => envelope_payload(page.envelope),
      "rate_limit" => rate_limit_payload(page.rate_limit),
      "etag" => page.etag,
      "from_cache" => page.from_cache,
      "api_version" => page.api_version
    }
  end

  defp peers_answer({:error, %Error{} = error}, request), do: http_error(error, request)

  defp peer_answer({:ok, %NotModified{} = result}, request),
    do: peers_answer({:ok, result}, request)

  defp peer_answer({:ok, %PeerEnvelope{} = result}, _request) do
    %{
      "ok" => true,
      "status" => result.status,
      "peer_ids" => [result.peer && result.peer.id],
      "signals" => signals(List.wrap(result.peer)),
      "rows" => rows(List.wrap(result.peer)),
      "envelope" => envelope_payload(result.envelope),
      "rate_limit" => rate_limit_payload(result.rate_limit),
      "etag" => result.etag,
      "from_cache" => result.from_cache,
      "api_version" => result.api_version
    }
  end

  defp peer_answer({:error, %Error{} = error}, request), do: http_error(error, request)

  # [id, state, tier, city] per peer, in order.
  defp rows(peers), do: Enum.map(peers, &[&1.id, &1.state, &1.tier, &1.city])

  defp cities_payload(nil), do: nil

  defp cities_payload(cities),
    do: Enum.map(cities, &[&1.id, &1.name, &1.cc, &1.lat, &1.lon, &1.peers])

  defp signals(peers) do
    Enum.map(peers, fn %Peer{} = peer ->
      [peer.id, peer.mempool.serves, peer.mempool.source, peer.auction.status, peer.auction.slot]
    end)
  end

  defp facets_answer({:ok, %NotModified{} = result}, request),
    do: peers_answer({:ok, result}, request)

  defp facets_answer({:ok, %Facets{} = result}, _request) do
    %{
      "ok" => true,
      "status" => result.status,
      "envelope" => envelope_payload(result.envelope),
      "total" => result.total,
      "facets" => facets_payload(result.facets),
      "rate_limit" => rate_limit_payload(result.rate_limit),
      "etag" => result.etag,
      "from_cache" => result.from_cache,
      "api_version" => result.api_version
    }
  end

  defp facets_answer({:error, %Error{} = error}, request), do: http_error(error, request)

  defp facets_payload(facets) do
    facets
    |> Enum.sort_by(fn {name, _} -> name end)
    |> Map.new(fn {name, %Facet{} = facet} ->
      {name,
       %{
         "available" => facet.available,
         "values" => Enum.map(facet.values, fn v -> [v.value, v.count] end)
       }}
    end)
  end

  defp gossip_config_answer(client, query, request) do
    case HlPeers.list_peers_as_gossip_config(client, query) do
      {:ok, %GossipConfigResult{} = result} ->
        %{
          "ok" => true,
          "status" => result.status,
          "json_text" => GossipConfig.to_json(result.config),
          "api_version" => result.api_version,
          "rate_limit" => rate_limit_payload(result.rate_limit),
          "etag" => result.etag,
          "from_cache" => result.from_cache
        }

      {:ok, %NotModified{} = result} ->
        peers_answer({:ok, result}, request)

      {:error, %Error{} = error} ->
        http_error(error, request)
    end
  end

  # -- network map ------------------------------------------------------

  @map_bool ~w(show_unreached hide_unreachable)

  defp network_map_op(op, request, client) do
    filters =
      (request["query"] || %{})
      |> Enum.sort_by(fn {name, _} -> name end)
      |> Enum.map(fn {name, value} -> {map_key(name), map_value(name, value)} end)

    result =
      case op do
        "network_map_graph" -> HlPeers.network_map_graph(client, filters)
        "network_map_summary" -> HlPeers.network_map_summary(client, filters)
      end

    case result do
      {:ok, %NetworkMap.Graph{} = graph} ->
        graph
        |> map_common()
        |> Map.merge(%{
          "peer_ids" => Enum.map(graph.nodes, & &1.id),
          "edges" => Enum.map(graph.edges, &[&1.from, &1.to, &1.span]),
          "truncated" => graph.truncated,
          "node_cities" => Enum.map(graph.nodes, & &1.city),
          "cities" => cities_payload(graph.cities),
          "attribution" => graph.attribution
        })

      {:ok, %NetworkMap.Summary{} = summary} ->
        summary
        |> map_common()
        |> Map.merge(%{
          "connectable" => summary.connectable,
          "serves_mempool" => summary.serves_mempool,
          "auction" => summary.auction,
          "cities" => cities_payload(summary.cities),
          "attribution" => summary.attribution
        })

      {:ok, %NotModified{} = result} ->
        peers_answer({:ok, result}, request)

      {:error, %Error{} = error} ->
        http_error(error, request)
    end
  end

  # An unknown name is passed through as a string key so the client's own
  # `Invalid` names it, rather than the harness guessing.
  defp map_key(name) do
    Enum.find(NetworkMap.filters(), name, &(Atom.to_string(&1) == name))
  end

  defp map_value("max_tier", value), do: to_integer(value)
  defp map_value(name, value) when name in @map_bool and is_binary(value), do: value == "true"
  defp map_value(_name, value), do: value

  defp map_common(result) do
    %{
      "ok" => true,
      "status" => result.status,
      "tiers" => Enum.map(result.tiers, &[&1.tier, &1.count]),
      "node_count" => result.node_count,
      "edge_count" => result.edge_count,
      "unreached" => result.unreached,
      "operator_nodes_count" => result.operator_nodes_count,
      "envelope" => %{
        "version" => result.version,
        "available" => result.available,
        "stale" => result.stale,
        "generated_at" => result.generated_at
      },
      "api_version" => result.api_version,
      "rate_limit" => rate_limit_payload(result.rate_limit),
      "etag" => result.etag,
      "from_cache" => result.from_cache
    }
  end

  # 🪤 `stream_peers/3` is a plain `Stream`, which has nowhere to hang a
  # truncation flag: the sibling clients return an iterator object carrying
  # `truncated`. The walk stops at `max_items` exactly, so "the guard cut it
  # short" is `seen >= max_items` — the same predicate the other three set the
  # flag from.
  defp iter_answer(client, query, request) do
    max_items = request["max_items"] || @default_max_items

    ids =
      client
      |> HlPeers.stream_peers(query, max_items: max_items)
      |> Enum.map(& &1.id)

    %{"ok" => true, "peer_ids" => ids, "truncated" => length(ids) >= max_items}
  rescue
    error in [Error] -> http_error(error, request)
  end

  defp http_error(%Error{} = error, request) do
    %{
      "ok" => false,
      "error" => http_error_payload(error, request),
      "rate_limit" => rate_limit_payload(error.rate_limit)
    }
  end

  # A RateLimited payload carries `signup_url` always (null if absent)
  # and `reset_at` only when the request injected `clock_now`.
  defp http_error_payload(%Error{kind: :rate_limited} = error, request) do
    payload = error |> error_payload() |> Map.put("signup_url", error.signup_url)

    if request["clock_now"] && error.reset_at,
      do: Map.put(payload, "reset_at", iso_seconds(error.reset_at)),
      else: payload
  end

  defp http_error_payload(%Error{} = error, _request), do: error_payload(error)

  defp iso_seconds(%DateTime{} = at) do
    at |> DateTime.truncate(:second) |> Calendar.strftime("%Y-%m-%dT%H:%M:%SZ")
  end

  defp rate_limit_payload(nil), do: %{}

  defp rate_limit_payload(%RateLimit{} = rate_limit) do
    %{
      "limit" => rate_limit.limit,
      "remaining" => rate_limit.remaining,
      "reset" => rate_limit.reset,
      "policy" => rate_limit.policy,
      "retry_after" => rate_limit.retry_after
    }
  end

  defp envelope_payload(nil), do: nil

  defp envelope_payload(%Envelope{} = envelope) do
    %{
      "version" => envelope.version,
      "available" => envelope.available,
      "stale" => envelope.stale,
      "chain" => envelope.chain,
      "generated_at" => envelope.generated_at,
      "next_sweep_at" => envelope.next_sweep_at,
      "attribution" => envelope.attribution
    }
  end
end

HlPeers.CrossCheck.Harness.main()

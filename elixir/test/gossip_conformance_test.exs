defmodule HlPeers.GossipConformanceTest do
  @moduledoc """
  **One generic runner** over `../fixtures/gossip/cases/*.json`, as
  `CLIENT_SPEC.md` §9(1) requires — not one test per case.

  The cases are language-neutral, so this file is the whole of the Elixir
  client's claim to producing byte-identical output to the other three clients.
  """

  use ExUnit.Case, async: true

  alias HlPeers.{Error, GossipConfig, Warning}
  alias HlPeers.GossipConfig.SaveResult
  alias HlPeers.Test.Fixtures

  @cases Fixtures.gossip_cases()

  test "every gossip conformance case" do
    results = Enum.map(@cases, &run_case/1)
    failures = for {name, {:error, message}} <- results, do: "#{name}: #{message}"
    passed = Enum.count(results, fn {_, r} -> r == :ok end)

    IO.puts("\nconformance: #{passed}/#{length(@cases)} gossip cases pass byte-exactly")

    assert failures == [],
           "#{length(failures)} of #{length(@cases)} conformance cases failed:\n" <>
             Enum.join(failures, "\n")

    assert passed == length(@cases)
  end

  # ------------------------------------------------------------------

  defp run_case(kase) do
    name = kase["name"]

    tmpdir =
      Path.join(
        System.tmp_dir!(),
        "hl_peers_case_#{name}_#{:erlang.unique_integer([:positive])}"
      )

    File.mkdir_p!(tmpdir)

    try do
      {name, check(kase, tmpdir)}
    catch
      kind, reason -> {name, {:error, "#{kind}: #{inspect(reason)}"}}
    after
      File.rm_rf!(tmpdir)
    end
  end

  defp check(kase, tmpdir) do
    expect = kase["expect"]
    ops = kase["operations"] || []

    case apply_ops(kase, ops, tmpdir) do
      {:error, error, at_op} ->
        expect_error(expect, error, at_op)

      {:ok, state} ->
        with :ok <- expect_no_error(expect),
             :ok <- expect_json(expect, state),
             :ok <- expect_value(expect, state, tmpdir),
             :ok <- expect_warnings(expect, state),
             :ok <- expect_diff(expect, state) do
          expect_files(expect, tmpdir)
        end
    end
  end

  defp apply_ops(kase, ops, tmpdir) do
    case build(kase) do
      {:error, error} ->
        {:error, error, nil}

      {:ok, config} ->
        ops
        |> Enum.with_index()
        |> Enum.reduce_while(
          {:ok, %{config: config, value: nil, warnings: []}},
          &step(&1, &2, tmpdir)
        )
    end
  end

  defp step({op, index}, {:ok, state}, tmpdir) do
    case apply_op(state, op, tmpdir) do
      {:ok, next} -> {:cont, {:ok, next}}
      {:error, error} -> {:halt, {:error, error, index}}
    end
  end

  defp build(%{"input_json_text" => text}) when is_binary(text), do: GossipConfig.parse(text)

  defp build(%{"input_config" => config}) when is_map(config) do
    # "parse(render(input_config)), or construct directly — either is fine, they
    # are equivalent." Going through render+parse exercises both directions.
    config |> GossipConfig.Serializer.render() |> GossipConfig.parse()
  end

  defp build(_), do: {:ok, nil}

  defp apply_op(state, %{"op" => op} = operation, tmpdir) do
    args = Enum.map(operation["args"] || [], &substitute(&1, tmpdir))
    opts = operation["opts"] || %{}
    dispatch(state, op, args, keyword(opts))
  end

  defp dispatch(state, "new", [chain, ips], _opts),
    do: ok(state, GossipConfig.new(chain, ips))

  defp dispatch(state, "to_json", _args, _opts),
    do: {:ok, %{state | value: GossipConfig.to_json(state.config), warnings: []}}

  defp dispatch(state, "validate", _args, _opts) do
    case GossipConfig.validate(state.config) do
      {:ok, warnings} -> {:ok, %{state | value: "ok", warnings: warnings}}
      {:error, error} -> {:error, error}
    end
  end

  defp dispatch(state, "save", [path], opts) do
    case GossipConfig.save(state.config, path, opts) do
      {:ok, %SaveResult{} = result} -> {:ok, %{state | value: result, warnings: result.warnings}}
      {:error, error} -> {:error, error}
    end
  end

  defp dispatch(state, "root_ips", _args, _opts),
    do: {:ok, %{state | value: GossipConfig.root_ips(state.config), warnings: []}}

  defp dispatch(state, "has_root_ip", [ip], _opts),
    do: {:ok, %{state | value: GossipConfig.has_root_ip?(state.config, ip), warnings: []}}

  defp dispatch(state, "add_root_ip", [ip], _opts),
    do: ok(state, GossipConfig.add_root_ip(state.config, ip))

  defp dispatch(state, "remove_root_ip", [ip], _opts),
    do: ok(state, GossipConfig.remove_root_ip(state.config, ip))

  defp dispatch(state, "set_root_ips", [ips], _opts),
    do: ok(state, GossipConfig.set_root_ips(state.config, ips))

  defp dispatch(state, "reserved_peer_ips", _args, _opts),
    do: {:ok, %{state | value: GossipConfig.reserved_peer_ips(state.config), warnings: []}}

  defp dispatch(state, "add_reserved_peer_ip", [ip], _opts),
    do: ok(state, GossipConfig.add_reserved_peer_ip(state.config, ip))

  defp dispatch(state, "remove_reserved_peer_ip", [ip], _opts),
    do: ok(state, GossipConfig.remove_reserved_peer_ip(state.config, ip))

  defp dispatch(state, "set_reserved_peer_ips", [ips], _opts),
    do: ok(state, GossipConfig.set_reserved_peer_ips(state.config, ips))

  defp dispatch(state, "set_chain", [chain], _opts),
    do: ok(state, GossipConfig.set_chain(state.config, chain))

  defp dispatch(state, "set_try_new_peers", [value], _opts),
    do: ok(state, GossipConfig.set_try_new_peers(state.config, value))

  defp dispatch(state, "set_n_gossip_peers", [value], _opts) do
    case GossipConfig.set_n_gossip_peers(state.config, value) do
      {:ok, config} -> ok(state, config)
      {:error, error} -> {:error, error}
    end
  end

  defp dispatch(state, "set_split_client_blocks", [value], _opts),
    do: ok(state, GossipConfig.set_split_client_blocks(state.config, value))

  defp dispatch(state, "unset_split_client_blocks", _args, _opts),
    do: ok(state, GossipConfig.unset_split_client_blocks(state.config))

  defp dispatch(state, "merge_peers", [peers], opts) do
    case GossipConfig.merge_peers(state.config, peers, opts) do
      {:ok, config, warnings} ->
        {:ok, %{state | config: config, value: config, warnings: warnings}}

      {:error, error} ->
        {:error, error}
    end
  end

  defp dispatch(state, "from_peers", [peers], opts) do
    case GossipConfig.from_peers(peers, opts) do
      {:ok, config, warnings} ->
        {:ok, %{state | config: config, value: config, warnings: warnings}}

      {:error, error} ->
        {:error, error}
    end
  end

  defp dispatch(_state, op, args, _opts),
    do: {:error, Error.invalid("op", "unhandled operation #{op}/#{length(args)}")}

  defp ok(state, config), do: {:ok, %{state | config: config, value: config, warnings: []}}

  # ------------------------------------------------------------------
  # expectations
  # ------------------------------------------------------------------

  defp expect_error(%{"error" => want}, error, at_op) do
    with :ok <- compare(kind(want["kind"]), error.kind, "error kind"),
         :ok <- compare_optional(want["parameter"], error.parameter, "error parameter"),
         :ok <- compare_optional(want["at_op"], at_op, "at_op") do
      expect_reason(want["reason_contains"], error)
    end
  end

  defp expect_error(_expect, error, at_op),
    do: {:error, "unexpected error at op #{inspect(at_op)}: #{inspect(error)}"}

  defp expect_reason(nil, _error), do: :ok

  defp expect_reason(needle, error) do
    haystack = String.downcase("#{error.reason} #{error.message} #{error.detail}")

    if String.contains?(haystack, String.downcase(needle)),
      do: :ok,
      else: {:error, "reason #{inspect(error.reason)} does not contain #{inspect(needle)}"}
  end

  defp expect_no_error(%{"error" => want}), do: {:error, "expected a #{want["kind"]} error"}
  defp expect_no_error(_), do: :ok

  defp expect_json(%{"json_text" => want}, state) do
    actual = GossipConfig.to_json(state.config)

    if actual == want,
      do: :ok,
      else: {:error, "json_text mismatch\n  want: #{inspect(want)}\n  got:  #{inspect(actual)}"}
  end

  defp expect_json(_expect, _state), do: :ok

  defp expect_value(%{"value" => want}, state, tmpdir),
    do: compare_value(substitute(want, tmpdir), state.value)

  defp expect_value(_expect, _state, _tmpdir), do: :ok

  defp compare_value(want, %SaveResult{} = result) when is_map(want) do
    # Subset match: only the keys present in the fixture are compared.
    Enum.find_value(want, :ok, fn {k, v} ->
      actual = Map.get(result, String.to_existing_atom(k))

      if actual == v,
        do: nil,
        else: {:error, "save.#{k}: want #{inspect(v)}, got #{inspect(actual)}"}
    end)
  end

  defp compare_value(want, actual) when want == actual, do: :ok

  defp compare_value(want, actual),
    do: {:error, "value: want #{inspect(want)}, got #{inspect(actual)}"}

  # `fixtures/README.md`: after the last operation the runner calls validate/1 and
  # compares the resulting warning SET — §6.4's state-derived codes unioned with
  # any pending merge warnings (§6.5), de-duplicated by code.
  defp expect_warnings(%{"warnings" => want}, state) do
    case GossipConfig.validate(state.config) do
      {:ok, warnings} ->
        got = warnings |> Warning.codes() |> Enum.map(&Atom.to_string/1) |> Enum.sort()

        if got == Enum.sort(want),
          do: :ok,
          else: {:error, "warnings: want #{inspect(Enum.sort(want))}, got #{inspect(got)}"}

      {:error, error} ->
        {:error, "warnings: validate/1 failed after the last operation: #{inspect(error)}"}
    end
  end

  defp expect_warnings(_expect, _state), do: :ok

  defp expect_diff(%{"diff_contains" => needles}, state) do
    diff = if is_struct(state.value, SaveResult), do: state.value.diff, else: ""

    Enum.find_value(needles, :ok, fn needle ->
      if String.contains?(diff, needle),
        do: nil,
        else: {:error, "diff does not contain #{inspect(needle)}; diff = #{inspect(diff)}"}
    end)
  end

  defp expect_diff(_expect, _state), do: :ok

  defp expect_files(expect, tmpdir) do
    with :ok <- expect_file_after(expect["file_after"], tmpdir),
         do: expect_file_absent(expect["file_absent"], tmpdir)
  end

  defp expect_file_after(nil, _tmpdir), do: :ok

  defp expect_file_after(%{"path" => path, "json_text" => want}, tmpdir) do
    path = substitute(path, tmpdir)

    case File.read(path) do
      {:ok, ^want} -> :ok
      {:ok, other} -> {:error, "file_after mismatch: #{inspect(other)}"}
      {:error, reason} -> {:error, "file_after: cannot read #{path}: #{inspect(reason)}"}
    end
  end

  defp expect_file_absent(nil, _tmpdir), do: :ok

  defp expect_file_absent(path, tmpdir) do
    path = substitute(path, tmpdir)
    if File.exists?(path), do: {:error, "file_absent: #{path} exists"}, else: :ok
  end

  defp compare(want, got, _label) when want == got, do: :ok

  defp compare(want, got, label),
    do: {:error, "#{label}: want #{inspect(want)}, got #{inspect(got)}"}

  defp compare_optional(nil, _got, _label), do: :ok
  defp compare_optional(want, got, label), do: compare(want, got, label)

  defp kind(name), do: name |> Macro.underscore() |> String.to_existing_atom()

  # ------------------------------------------------------------------

  defp substitute(value, tmpdir) when is_binary(value),
    do: String.replace(value, "$TMPDIR", tmpdir)

  defp substitute(value, tmpdir) when is_list(value), do: Enum.map(value, &substitute(&1, tmpdir))

  defp substitute(value, tmpdir) when is_map(value),
    do: Map.new(value, fn {k, v} -> {k, substitute(v, tmpdir)} end)

  defp substitute(value, _tmpdir), do: value

  defp keyword(opts) when is_map(opts) do
    Enum.map(opts, fn {k, v} -> {String.to_existing_atom(k), option_value(k, v)} end)
  end

  defp option_value("mode", v) when is_binary(v), do: String.to_existing_atom(v)
  defp option_value(_k, v), do: v
end

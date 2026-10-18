defmodule Embervm.LogFormatterMetadataTest do
  use ExUnit.Case, async: true

  # Explicit Logger metadata missing at #7054's baseline. Keep this independent
  # of the formatter's allowlist so deleting a restored field fails a test.
  @restored_metadata [
    absent_ms: 1_000,
    anchor_node: "node-a",
    blessed_generation: 2,
    bound_ms: 5_000,
    cidr: "10.0.0.0/24",
    class: :stateful,
    consecutive: 3,
    consecutive_aborts: 4,
    deadline_ms: 10_000,
    declared_usable_mib: 4_096,
    degraded_member: "db-0",
    deleted: 10,
    deployment: "embervm-brick-4gi",
    desired: 2,
    destroyed: 3,
    event: :generation_quarantined,
    evicted: 4,
    failures: 1,
    generation: 3,
    group_instance_id: "group-1",
    ip: "10.0.0.2",
    member_name: "db-0",
    members: 2,
    mode: :relight,
    namespace: "embervm",
    new_seq: 5,
    old_seq: 4,
    oldest_waiter_age_ms: 100,
    park_depth: 2,
    pause_spread_ms: 50,
    port: 5_432,
    previous_blessed_generation: 1,
    previous_generation: 2,
    registered: 3,
    relight_ms: 12,
    reported_mem_budget_mib: [4_096, 8_192],
    requested_generation: 4,
    result: "{:error, :timeout}",
    row_generation: 2,
    secret_ref: "database-credentials",
    set_id: "set-1",
    snapshot_bytes: 1_048_576,
    source_evicted: true,
    store_generation: 3,
    teardown_confirmed: false,
    version: "v1",
    waited_ms: 200
  ]

  for {key, value} <- @restored_metadata do
    expected =
      cond do
        is_boolean(value) -> value
        is_atom(value) -> Atom.to_string(value)
        is_list(value) -> inspect(value)
        true -> value
      end

    test "preserves restored #{key} metadata and the log envelope" do
      metadata = %{
        unquote(key) => unquote(Macro.escape(value)),
        :time => 0,
        :trace_id => "trace-1",
        :task_id => "task-1",
        :session_id => "session-1"
      }

      decoded = decode(metadata)
      assert decoded[unquote(Atom.to_string(key))] == unquote(Macro.escape(expected))
      assert decoded["message"] == "diagnostic"
      assert decoded["level"] == "warning"
      assert decoded["time"] == "1970-01-01T00:00:00.000000Z"
      assert decoded["trace_id"] == "trace-1"
      assert decoded["task_id"] == "task-1"
      assert decoded["session_id"] == "session-1"
    end
  end

  test "keeps jsonable scalar and inspected compound semantics" do
    for {value, expected} <- [
          {"ready", "ready"},
          {42, 42},
          {1.5, 1.5},
          {true, true},
          {false, false},
          {:ready, "ready"},
          {nil, "nil"},
          {{:error, :timeout}, "{:error, :timeout}"},
          {%{count: 2}, "%{count: 2}"},
          {[1, 2], "[1, 2]"}
        ] do
      assert decode(%{result: value})["result"] == expected
    end
  end

  test "excludes unknown metadata and synthetic secret contents" do
    decoded =
      decode(%{
        "generation" => "untrusted-string-key",
        secret_ref: "database-credentials",
        password: "synthetic-password",
        token: "synthetic-token",
        secret: "synthetic-secret",
        secret_data: %{password: "synthetic-nested-password"},
        unknown: "synthetic-unknown"
      })

    assert decoded == %{
             "message" => "diagnostic",
             "level" => "warning",
             "time" => "",
             "secret_ref" => "database-credentials"
           }
  end

  test "falls back without raising for invalid metadata or unencodable strings" do
    for metadata <- [[generation: 1], %{secret_ref: <<255>>}] do
      assert format(metadata) == "warning diagnostic\n"
    end

    assert Embervm.LogFormatter.format(:invalid_event, %{}) == "log\n"
  end

  test "every explicit top-level Logger keyword field survives the real formatter" do
    # Relative to the test, so this also discovers lib/ in Bazel's staged Mix tree.
    root = Path.expand("../../lib", __DIR__)
    files = Path.wildcard(Path.join(root, "**/*.ex"))
    assert files != [], "no control sources found under #{root}"

    fields =
      Enum.flat_map(files, fn file ->
        file
        |> File.read!()
        |> Code.string_to_quoted!(file: file)
        |> logger_fields()
        |> Enum.map(fn {line, key} -> {file, line, key} end)
      end)

    assert fields != [], "no explicit Logger metadata found under #{root}"

    missing =
      for {file, line, key} <- fields,
          decode(%{key => "metadata-sentinel"})[Atom.to_string(key)] != "metadata-sentinel" do
        "#{Path.relative_to(file, root)}:#{line}: #{key}"
      end

    assert missing == [], "formatter drops explicit Logger metadata:\n#{Enum.join(missing, "\n")}"
  end

  test "source guard reads only literal top-level metadata on Logger log calls" do
    ast =
      Code.string_to_quoted!("""
      Logger.warning("warning", result: [nested: 1], generation: call(inner: 2))
      Logger.log(:info, "info", event: :ready)
      Logger.error("dynamic", metadata)
      Logger.metadata(password: "not a log call")
      Other.warning("other", ignored: 1)
      Logger.info([report_field: 1])
      """)

    assert logger_fields(ast) == [{1, :result}, {1, :generation}, {2, :event}]
  end

  defp logger_fields(ast) do
    {_, fields} =
      Macro.prewalk(ast, [], fn
        {{:., _, [{:__aliases__, _, [:Logger]}, level]}, location, args} = node, acc
        when level in [
               :debug,
               :info,
               :notice,
               :warning,
               :warn,
               :error,
               :critical,
               :alert,
               :emergency,
               :log
             ] and
               is_list(args) ->
          metadata = List.last(args)
          arity = if level == :log, do: 3, else: 2

          if length(args) == arity and is_list(metadata) and Keyword.keyword?(metadata) do
            fields = Enum.map(metadata, fn {key, _} -> {location[:line], key} end)
            {node, acc ++ fields}
          else
            {node, acc}
          end

        node, acc ->
          {node, acc}
      end)

    fields
  end

  defp format(metadata) do
    %{level: :warning, msg: {:string, "diagnostic"}, meta: metadata}
    |> Embervm.LogFormatter.format(%{})
    |> IO.iodata_to_binary()
  end

  defp decode(metadata), do: metadata |> format() |> :json.decode()
end

defmodule Embervm.OpLog.RowDecoderTest do
  use ExUnit.Case, async: false

  alias Embervm.OpLog.{Op, Postgres, RowDecoder, SQLite}
  alias Exqlite.Sqlite3

  # Each fixture states the storage positions and domain result independently.
  defp fixture(:op) do
    {[11, 12, "tenant", "principal", "workload", "task", "session", "serving",
      "stateful", "group", "denied", ~s({"legacy":null})],
     %Op{seq: 11, ts: 12, tenant: "tenant", principal: "principal", workload: "workload",
       task_id: "task", session_id: "session", serving_instance_id: "serving",
       stateful_instance_id: "stateful", group_instance_id: "group", kind: :denied,
       payload: %{"legacy" => :null}}}
  end

  defp fixture(:task) do
    {["task", "tenant", "principal", "workload", "running", 6, "key", 8, 9, 10],
     %{task_id: "task", tenant: "tenant", principal: "principal", workload: "workload",
       state: "running", attempt: 6, idempotency_key: "key", submitted_at: 8,
       updated_at: 9, expires_at: 10}}
  end

  defp fixture(:session) do
    {["session", "tenant", "principal", "workload", "running", "node", "vm", "volume",
      "base", "digest", 11, "snapshot", 13, "hash", 15, 16, 17, 18, 19, "reason",
      "lineage", "key", ~s({"operation_id":"intent","invoke_started_at":null}),
      ~s({"operation_id":"completion","invoke_started_at":24}), 25,
      ~s({"dispatch_id":"interrupted","invoke_started_at":26}), "inflight",
      ~s({"dispatch_id":"fenced","invoke_started_at":28})],
     %{session_id: "session", tenant: "tenant", principal: "principal", workload: "workload",
       state: "running", node_id: "node", vm_id: "vm", volume_node_id: "volume",
       base_snapshot_ref: "base", base_digest: "digest", generation: 11,
       snapshot_ref: "snapshot", snapshot_size_bytes: 13, token_sha256: "hash",
       created_at: 15, invoke_started_at: 16, last_invoke_at: 17, expires_at: 18,
       updated_at: 19, terminal_reason: "reason", lineage_id: "lineage", idempotency_key: "key",
       stop_intent: %{"operation_id" => "intent", "invoke_started_at" => nil},
       stop_completion: %{"operation_id" => "completion", "invoke_started_at" => 24},
       turn_seq: 25, interrupted_turn: %{"dispatch_id" => "interrupted", "invoke_started_at" => 26},
       inflight_dispatch_id: "inflight",
       recovery_fence: %{"dispatch_id" => "fenced", "invoke_started_at" => 28}}}
  end

  defp fixture(:serving_instance) do
    {["serving", "tenant", "principal", "workload", "ready", "node", "vm", "ip", 9,
      "base", "digest", 12, "snapshot", 14, 15, 16, 17, "reason"],
     %{instance_id: "serving", tenant: "tenant", principal: "principal", workload: "workload",
       state: "ready", node_id: "node", vm_id: "vm", ip: "ip", port: 9,
       base_snapshot_ref: "base", base_digest: "digest", generation: 12,
       snapshot_ref: "snapshot", snapshot_size_bytes: 14, created_at: 15,
       last_active_at: 16, updated_at: 17, terminal_reason: "reason"}}
  end

  defp fixture(:stateful_instance) do
    {["stateful", "tenant", "principal", "workload", "banked", "node", "vm", "ip", 9,
      10, "snapshot", 12, 13, 14, 15, 16, "reason"],
     %{instance_id: "stateful", tenant: "tenant", principal: "principal", workload: "workload",
       state: "banked", node_id: "node", vm_id: "vm", ip: "ip", port: 9,
       generation: 10, snapshot_ref: "snapshot", snapshot_generation: 12,
       snapshot_size_bytes: 13, created_at: 14, last_active_at: 15, updated_at: 16,
       terminal_reason: "reason"}}
  end

  defp fixture(:group_instance) do
    {["group", "tenant", "principal", "workload", "idle", "node", "subnet", "entry", 9,
      10, "set", 12, 13, 14, "reason"],
     %{instance_id: "group", tenant: "tenant", principal: "principal", workload: "workload",
       state: "idle", node_id: "node", subnet_cidr: "subnet", entry_member: "entry",
       entry_port: 9, listen_port: 10, set_id: "set", created_at: 12,
       last_active_at: 13, updated_at: 14, terminal_reason: "reason"}}
  end

  @kinds [:op, :task, :session, :serving_instance, :stateful_instance, :group_instance]
  for kind <- @kinds do
    test "#{kind} maps every ordered position" do
      {row, expected} = fixture(unquote(kind))
      assert apply(RowDecoder, unquote(kind), [row]) == expected
    end
  end

  test "nullable storage fields stay nil, including all four absent session proofs" do
    for {kind, positions, fields} <- [
      {:op, [3, 4, 5, 6, 7, 8, 9], [:principal, :workload, :task_id, :session_id,
        :serving_instance_id, :stateful_instance_id, :group_instance_id]},
      {:task, [2, 3, 6, 9], [:principal, :workload, :idempotency_key, :expires_at]},
      {:session, [2, 3, 5, 6, 7, 8, 9, 11, 12, 13, 15, 16, 17, 19, 21, 22, 23, 25, 26, 27],
       [:principal, :workload, :node_id, :vm_id, :volume_node_id, :base_snapshot_ref, :base_digest, :snapshot_ref,
        :snapshot_size_bytes, :token_sha256, :invoke_started_at, :last_invoke_at, :expires_at, :terminal_reason,
        :idempotency_key, :stop_intent, :stop_completion, :interrupted_turn, :inflight_dispatch_id,
        :recovery_fence]},
      {:serving_instance, [2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 15, 17],
       [:principal, :workload, :node_id, :vm_id, :ip, :port, :base_snapshot_ref, :base_digest, :snapshot_ref,
        :snapshot_size_bytes, :last_active_at, :terminal_reason]},
      {:stateful_instance, [2, 3, 5, 6, 7, 8, 10, 11, 12, 14, 16],
       [:principal, :workload, :node_id, :vm_id, :ip, :port, :snapshot_ref, :snapshot_generation,
        :snapshot_size_bytes, :last_active_at, :terminal_reason]},
      {:group_instance, [2, 3, 5, 6, 7, 8, 9, 10, 12, 14],
       [:principal, :workload, :node_id, :subnet_cidr, :entry_member, :entry_port, :listen_port, :set_id,
        :last_active_at, :terminal_reason]}
    ] do
      {row, expected} = fixture(kind)
      row = Enum.reduce(positions, row, &List.replace_at(&2, &1, nil))
      expected = Enum.reduce(fields, expected, &Map.put(&2, &1, nil))
      assert apply(RowDecoder, kind, [row]) == expected
    end
  end

  test "each proof normalizes only invoke_started_at JSON null" do
    {row, _} = fixture(:session)
    for {position, field} <- [{22, :stop_intent}, {23, :stop_completion},
                            {25, :interrupted_turn}, {27, :recovery_fence}] do
      decoded = RowDecoder.session(List.replace_at(row, position,
        ~s({"invoke_started_at":null,"other":null})))
      assert decoded[field] == %{"invoke_started_at" => nil, "other" => :null}
    end
  end

  test "operation decoding retains ETF bytes and normalization" do
    {row, expected} = fixture(:op)
    bytes = <<0, 255, 137, "body">>
    payload = :erlang.term_to_binary(%{body: bytes, drop: nil, child: %{state: :ready},
      list: [nil, :ready, true, false]})
    assert RowDecoder.op(List.replace_at(row, 11, payload)) ==
      %{expected | payload: %{"body" => bytes, "child" => %{"state" => "ready"},
        "list" => ["nil", "ready", true, false]}}
  end

  test "operation decoding preserves malformed payload errors and existing-atom conversion" do
    {row, _} = fixture(:op)
    assert_raise ArgumentError, fn ->
      RowDecoder.op(List.replace_at(row, 10, "unknown_row_kind_#{System.unique_integer([:positive])}"))
    end
    for bytes <- [<<131>>, <<131, 255>>] do
      assert_raise ArgumentError, fn -> RowDecoder.op(List.replace_at(row, 11, bytes)) end
    end
    for {bytes, reason} <- [{"", :unexpected_end}, {"{", :unexpected_end},
                            {"not json", {:invalid_byte, 111}}] do
      error = assert_raise ErlangError, fn -> RowDecoder.op(List.replace_at(row, 11, bytes)) end
      assert error.original == reason
    end
    assert_raise FunctionClauseError, fn -> RowDecoder.op(List.replace_at(row, 11, nil)) end
  end

  @storage [
    {:op, "ops", "seq,ts,tenant,principal,workload,task_id,session_id,serving_instance_id,stateful_instance_id,group_instance_id,kind,payload_json", :read_from},
    {:task, "tasks", "task_id,tenant,principal,workload,state,attempt,idempotency_key,submitted_at,updated_at,expires_at", :load_tasks},
    {:session, "sessions", "session_id,tenant,principal,workload,state,node_id,vm_id,volume_node_id,base_snapshot_ref,base_digest,generation,snapshot_ref,snapshot_size_bytes,token_sha256,created_at,invoke_started_at,last_invoke_at,expires_at,updated_at,terminal_reason,lineage_id,idempotency_key,stop_intent_json,stop_completion_json,turn_seq,interrupted_turn_json,inflight_dispatch_id,recovery_fence_json", :load_sessions},
    {:serving_instance, "serving_instances", "instance_id,tenant,principal,workload,state,node_id,vm_id,ip,port,base_snapshot_ref,base_digest,generation,snapshot_ref,snapshot_size_bytes,created_at,last_active_at,updated_at,terminal_reason", :load_serving_instances},
    {:stateful_instance, "stateful_instances", "instance_id,tenant,principal,workload,state,node_id,vm_id,ip,port,generation,snapshot_ref,snapshot_generation,snapshot_size_bytes,created_at,last_active_at,updated_at,terminal_reason", :load_stateful_instances},
    {:group_instance, "group_instances", "instance_id,tenant,principal,workload,state,node_id,subnet_cidr,entry_member,entry_port,listen_port,set_id,created_at,last_active_at,updated_at,terminal_reason", :load_group_instances}
  ]

  # Trace only the isolated adapter process. A copied or incorrectly wired
  # decoder can return the right shape, but must still fail this wiring check.
  defp trace_decoder(server) do
    Code.ensure_loaded!(RowDecoder)
    :erlang.trace_pattern({RowDecoder, :_, 1}, true, [:local])
    :erlang.trace(server, true, [:call, {:tracer, self()}])
    on_exit(fn -> :erlang.trace_pattern({RowDecoder, :_, 1}, false, [:local]) end)
  end

  defp read(backend, server, :read_from), do: backend.read_from(server, 0)
  defp read(backend, server, loader), do: apply(backend, loader, [server])

  test "SQLite public readers use all six decoders and preserve empty and received row order" do
    dir = Path.join(System.tmp_dir!(), "row_decoder_#{System.unique_integer([:positive])}")
    File.mkdir_p!(dir)
    path = Path.join(dir, "oplog.db")
    {:ok, server} = SQLite.start_link(name: nil, path: path)
    {:ok, conn} = Sqlite3.open(path)
    on_exit(fn ->
      Embervm.TestProcess.stop_safely(server)
      Sqlite3.close(conn)
      File.rm_rf!(dir)
    end)
    trace_decoder(server)

    for {kind, table, columns, loader} <- @storage do
      assert read(SQLite, server, loader) == {:ok, []}
      {row, expected} = fixture(kind)
      {second, second_expected} =
        if kind == :op do
          {List.replace_at(row, 0, 22), %{expected | seq: 22}}
        else
          key = if kind in [:serving_instance, :stateful_instance, :group_instance],
            do: :instance_id, else: if(kind == :task, do: :task_id, else: :session_id)
          second = List.replace_at(row, 0, "a-second")
          second_expected = Map.put(expected, key, "a-second")
          case kind do
            :task -> {List.replace_at(second, 6, "second-key"), %{second_expected | idempotency_key: "second-key"}}
            :session -> {List.replace_at(second, 21, "second-key"), %{second_expected | idempotency_key: "second-key"}}
            _ -> {second, second_expected}
          end
        end
      # Journal insertion order differs from seq order. Other readers retain
      # the adapter's received row order, with the second ID sorting first.
      inserts = if kind == :op, do: [second, row], else: [row, second]
      for values <- inserts do
        placeholders = Enum.map_join(values, ",", fn _ -> "?" end)
        {:ok, stmt} = Sqlite3.prepare(conn, "INSERT INTO #{table} (#{columns}) VALUES (#{placeholders})")
        :ok = Sqlite3.bind(stmt, values)
        :done = Sqlite3.step(conn, stmt)
        :ok = Sqlite3.release(conn, stmt)
      end
      assert read(SQLite, server, loader) == {:ok, [expected, second_expected]}
      assert_receive {:trace, ^server, :call, {RowDecoder, ^kind, [^row]}}
      assert_receive {:trace, ^server, :call, {RowDecoder, ^kind, [^second]}}
    end
  end

  test "Postgres query seams use shared journal, task and session mappings in received order" do
    for {kind, table, _, loader} <- Enum.take(@storage, 3) do
      {row, expected} = fixture(kind)
      second = List.replace_at(row, 0, if(kind == :op, do: 22, else: "a-second"))
      key = case kind do
        :op -> :seq
        :task -> :task_id
        :session -> :session_id
      end
      second_expected = Map.put(expected, key, hd(second))
      {:ok, server} = Postgres.start_link(name: nil, connection: self(),
        query_fun: fn _, sql, _ ->
          cond do
            sql =~ "SELECT value FROM meta" -> {:ok, %Postgrex.Result{rows: []}}
            sql =~ "FROM #{table}" -> {:ok, %Postgrex.Result{rows: [row, second]}}
          end
        end)
      on_exit(fn -> Embervm.TestProcess.stop_safely(server) end)
      trace_decoder(server)
      assert read(Postgres, server, loader) == {:ok, [expected, second_expected]}
      assert_receive {:trace, ^server, :call, {RowDecoder, ^kind, [^row]}}
      assert_receive {:trace, ^server, :call, {RowDecoder, ^kind, [^second]}}
    end

    {:ok, server} = Postgres.start_link(name: nil, connection: self(),
      query_fun: fn _, _, _ -> {:ok, %Postgrex.Result{rows: []}} end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(server) end)
    for {_, _, _, loader} <- Enum.take(@storage, 3) do
      assert read(Postgres, server, loader) == {:ok, []}
    end
  end
end

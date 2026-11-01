defmodule Embervm.SessionCessationEvidence do
  @moduledoc """
  Bounded proof for an original, parked-then-evicted session whose hot and durable
  rows were pruned. A retained create above the compaction marker establishes
  complete history. This is a cessation proof, never an invocation-result proof.
  """

  @history_limit 256
  @creation_limit 32_768
  @payload_limit 65_536
  @history_bytes 1_048_576
  @creation_bytes 16_777_216
  @max_integer 9_223_372_036_854_775_807

  def expected?(%{generation: generation, invoke_started_at: started, updated_at: updated}) do
    is_integer(generation) and generation == 0 and integer?(started) and
      integer?(updated) and updated >= started
  end

  def expected?(_), do: false

  # Called inside one backend read transaction. query returns raw rows; decode
  # is the adapter's existing payload codec. No payload leaves this module.
  def read(query, dialect, decode, session_id, expected, principal) do
    diagnostics = %{history_complete: false, creation_history_complete: false}

    with true <- expected?(expected),
         true <- string?(session_id),
         :ok <- authorize_history(query, dialect, session_id, principal),
         {:ok, marker_rows} <-
           query.("SELECT value FROM meta WHERE key = #{arg(dialect, 1)}", [
             "compacted_through_seq"
           ]),
         {:ok, marker} <- marker(marker_rows),
         {:ok, rows} <-
           bounded_ops(
             query,
             dialect,
             "session_id = #{arg(dialect, 1)}",
             [session_id],
             @history_limit,
             @history_bytes
           ),
         {:ok, history} <- history(rows, decode, session_id, marker, expected, principal),
         {:ok, creations} <-
           bounded_ops(
             query,
             dialect,
             "kind = 'session_created' AND seq >= #{arg(dialect, 1)}",
             [history.first_seq],
             @creation_limit,
             @creation_bytes
           ),
         :ok <- no_successor(creations, decode, history),
         {:ok, []} <-
           query.(
             "SELECT session_id FROM sessions WHERE session_id = #{arg(dialect, 1)} OR COALESCE(lineage_id, session_id) = #{arg(dialect, 1)} OR vm_id = #{arg(dialect, 2)} LIMIT 1",
             [session_id, history.vm_id]
           ),
         {:ok, []} <- query.(residency_sql(dialect), [session_id, history.vm_id]) do
      {:ok, Map.put(history, :creation_history_complete, true)}
    else
      {:error, reason, detail} -> {:error, reason, Map.merge(diagnostics, detail)}
      {:ok, [_ | _]} -> {:error, :historical_current_owner, diagnostics}
      false -> {:error, :historical_invalid_identity, diagnostics}
      _ -> {:error, :historical_store_unavailable, diagnostics}
    end
  rescue
    _ -> {:error, :historical_malformed_history, diagnostics()}
  end

  # Same bounded snapshot semantics, but the recovery caller separately checks
  # an exact retained terminal row and seals its restore authority. A final
  # fence audit is not a lifecycle event and must exactly match the projection.
  # The original history digest deliberately excludes that one audit so a lost
  # apply response can be reconciled with the original preview.
  def recovery_history(query, dialect, decode, session_id, expected, principal) do
    with true <- expected?(expected) and string?(session_id),
         :ok <- authorize_history(query, dialect, session_id, principal),
         {:ok, marker_rows} <-
           query.("SELECT value FROM meta WHERE key = #{arg(dialect, 1)}", [
             "compacted_through_seq"
           ]),
         {:ok, marker} <- marker(marker_rows),
         {:ok, rows} <-
           bounded_ops(
             query,
             dialect,
             "session_id = #{arg(dialect, 1)}",
             [session_id],
             @history_limit,
             @history_bytes
           ),
         {:ok, original, audit} <- split_recovery_audit(rows, decode),
         {:ok, proof} <-
           history(original, decode, session_id, marker, expected, principal, :recovery),
         true <- proof.interrupted and proof.dispatch_id == expected.dispatch_id,
         {:ok, creations} <-
           bounded_ops(
             query,
             dialect,
             "kind = 'session_created' AND seq >= #{arg(dialect, 1)}",
             [proof.first_seq],
             @creation_limit,
             @creation_bytes
           ),
         :ok <- no_successor(creations, decode, proof),
         {:ok, []} <- query.(residency_sql(dialect), [session_id, proof.vm_id]) do
      {:ok, Map.merge(proof, %{creation_history_complete: true, recovery_audit: audit})}
    else
      {:error, reason, detail} -> {:error, reason, Map.merge(diagnostics(), detail)}
      {:ok, [_ | _]} -> {:error, :historical_current_owner, diagnostics()}
      false -> {:error, :recovery_identity_mismatch, diagnostics()}
      _ -> {:error, :historical_store_unavailable, diagnostics()}
    end
  rescue
    _ -> {:error, :historical_malformed_history, diagnostics()}
  end

  defp split_recovery_audit(rows, decode) do
    case List.last(rows) do
      [seq, ts, owner, workload, id, "session_recovery_fenced", bytes] ->
        original = Enum.drop(rows, -1)

        audit = %{
          seq: seq,
          ts: ts,
          principal: owner,
          workload: workload,
          session_id: id,
          payload: decoded(decode, bytes)
        }

        {:ok, original, audit}

      _ when length(rows) < @history_limit ->
        {:ok, rows, nil}

      _ ->
        {:error, :historical_history_bound, %{}}
    end
  end

  defp diagnostics, do: %{history_complete: false, creation_history_complete: false}
  defp arg(:postgres, n), do: "$#{n}"
  defp arg(:sqlite, n), do: "?#{n}"

  defp marker([]), do: {:ok, 0}
  defp marker([[value]]) when is_integer(value) and value >= 0, do: {:ok, value}
  defp marker(_), do: {:error, :historical_invalid_marker, %{}}

  defp bounded_ops(query, dialect, predicate, params, limit, bytes) do
    column = if dialect == :postgres, do: "payload_blob", else: "payload_json"
    length_fun = if dialect == :postgres, do: "octet_length", else: "length"

    size =
      if dialect == :postgres,
        do: "#{length_fun}(#{column})",
        else: "length(CAST(#{column} AS BLOB))"

    with {:ok, [[count, total, largest]]} <-
           query.(
             "SELECT COUNT(*), COALESCE(SUM(#{size}), 0), COALESCE(MAX(#{size}), 0) FROM ops WHERE #{predicate}",
             params
           ),
         true <-
           is_integer(count) and count <= limit and is_integer(total) and total <= bytes and
             is_integer(largest) and largest <= @payload_limit,
         {:ok, rows} <-
           query.(
             "SELECT seq, ts, principal, workload, session_id, kind, #{column} FROM ops WHERE #{predicate} ORDER BY seq ASC LIMIT #{limit + 1}",
             params
           ),
         true <- length(rows) == count and length(rows) <= limit do
      {:ok, rows}
    else
      false -> {:error, :historical_history_bound, %{}}
      _ -> {:error, :historical_store_unavailable, %{}}
    end
  end

  defp history(rows, decode, session_id, marker, expected, principal, mode \\ :historical)

  defp history([], _decode, _session_id, _marker, _expected, _principal, _mode),
    do: {:error, :historical_history_missing, %{}}

  defp history(rows, decode, session_id, marker, expected, principal, mode) do
    with [first | rest] <- rows,
         [seq, ts, owner, workload, ^session_id, "session_created", payload] <- first,
         true <- integer?(seq) and seq > marker and integer?(ts),
         :ok <- owner_scope(principal, owner),
         true <- string?(owner) and string?(workload),
         %{"lineage_id" => ^session_id, "node_id" => node, "vm_id" => vm, "state" => "running"} <-
           decoded(decode, payload),
         true <- string?(node) and string?(vm) do
      initial = %{
        session_id: session_id,
        lineage_id: session_id,
        principal: owner,
        workload: workload,
        node_id: node,
        vm_id: vm,
        generation: 0,
        state: "running",
        first_seq: seq,
        last_seq: seq,
        last_ts: ts,
        invoke_started_at: nil,
        dispatch_id: nil,
        expected_dispatch_id: if(mode == :recovery, do: expected.dispatch_id),
        allowed_evictions:
          if(mode == :recovery, do: ["idle_ttl", "node_gone"], else: ["idle_ttl"]),
        interrupted: false,
        interrupted_identity: nil,
        last_invoke_at: nil,
        parking: false,
        parked: false,
        preceding_state: nil,
        terminal_reason: nil,
        updated_at: nil
      }

      result =
        Enum.reduce_while(rest, {:ok, initial}, fn row, {:ok, state} ->
          case event(row, decode, state) do
            {:ok, next} ->
              {:cont, {:ok, next}}

            {:error, reason} ->
              {:halt, {:error, reason, event_summary(state, length(rows), row, decode)}}
          end
        end)

      with {:ok, final} <- result,
           true <-
             final.state == "evicted" and final.invoke_started_at == expected.invoke_started_at and
               final.updated_at == expected.updated_at do
        stable =
          {1, {@history_limit, @creation_limit, @payload_limit, @history_bytes, @creation_bytes},
           rows,
           Map.take(final, [
             :session_id,
             :lineage_id,
             :principal,
             :workload,
             :node_id,
             :vm_id,
             :generation,
             :invoke_started_at,
             :last_invoke_at,
             :updated_at
           ])}

        hash =
          :crypto.hash(:sha256, :erlang.term_to_binary(stable, [:deterministic]))
          |> Base.encode16(case: :lower)

        {:ok,
         Map.merge(final, %{
           history_complete: true,
           event_count: length(rows),
           evidence_sha256: hash
         })}
      else
        false -> {:error, :historical_identity_mismatch, summary(initial, length(rows))}
        error -> error
      end
    else
      false -> {:error, :historical_unavailable, %{}}
      {:error, _, _} = error -> error
      _ -> {:error, :historical_original_create_required, %{}}
    end
  end

  defp event([seq, ts, owner, workload, id, kind, bytes], decode, state) do
    if integer?(seq) and seq > state.last_seq and integer?(ts) and ts >= state.last_ts and
         owner == state.principal and workload == state.workload and id == state.session_id and
         state.state != "evicted" do
      case transition(kind, decoded(decode, bytes), ts, state) do
        {:ok, next} -> {:ok, %{next | last_seq: seq, last_ts: ts}}
        error -> error
      end
    else
      {:error, :historical_reordered_or_later_event}
    end
  end

  defp event(_, _decode, _state), do: {:error, :historical_malformed_history}

  defp transition(
         "session_invoke_started",
         payload,
         ts,
         %{state: "running", invoke_started_at: nil, parking: false} = state
       ) do
    # One dispatch only. Older records without the later dispatch_id field are
    # identified by the original guest, original VM and monotonic start stamp.
    if payload["turn_seq"] == 1 and
         (is_nil(state.expected_dispatch_id) or
            payload["dispatch_id"] == state.expected_dispatch_id),
       do: {:ok, %{state | invoke_started_at: ts, dispatch_id: payload["dispatch_id"]}},
       else: {:error, :historical_multiple_dispatches}
  end

  defp transition(
         "session_invoked",
         payload,
         ts,
         %{state: "running", last_invoke_at: nil, invoke_started_at: started} = state
       )
       when is_integer(started) and ts >= started do
    marker = payload["interrupted_turn"]

    interrupted =
      is_map(marker) and marker["seq"] == 1 and
        marker["dispatch_id"] == state.dispatch_id and string?(marker["cli_session_id"]) and
        is_binary(marker["transcript_path"]) and byte_size(marker["transcript_path"]) in 1..4096

    if is_nil(state.expected_dispatch_id) or interrupted,
      do:
        {:ok,
         %{
           state
           | last_invoke_at: ts,
             interrupted: interrupted,
             interrupted_identity: if(interrupted, do: marker)
         }},
      else: {:error, :recovery_interruption_unavailable}
  end

  defp transition(
         "session_parking",
         %{"reason" => "idled", "volume_node_id" => node},
         _ts,
         %{state: "running", node_id: node, invoke_started_at: started} = state
       )
       when is_integer(started),
       do: {:ok, %{state | state: "parking", parking: true}}

  defp transition(
         "session_parked",
         %{"reason" => "idled", "volume_node_id" => node},
         _ts,
         %{state: "parking", node_id: node, parking: true} = state
       ),
       do: {:ok, %{state | state: "parked", parked: true}}

  defp transition(
         "session_evicted",
         %{"reason" => reason},
         ts,
         %{state: "parked", parked: true} = state
       ) do
    if reason in state.allowed_evictions do
      {:ok,
       %{
         state
         | state: "evicted",
           preceding_state: "parked",
           terminal_reason: reason,
           updated_at: ts
       }}
    else
      {:error, :historical_unsupported_transition}
    end
  end

  defp transition(_kind, _payload, _ts, _state), do: {:error, :historical_unsupported_transition}

  defp no_successor(rows, decode, history) do
    Enum.reduce_while(rows, :ok, fn row, :ok ->
      case row do
        [seq, _ts, _owner, _workload, id, "session_created", bytes] ->
          case decoded(decode, bytes) do
            %{"lineage_id" => lineage} when is_binary(lineage) and lineage != "" ->
              if (lineage == history.lineage_id or id == history.session_id) and
                   seq != history.first_seq,
                 do:
                   {:halt, {:error, :historical_successor, summary(history, history.event_count)}},
                 else: {:cont, :ok}

            _ ->
              {:halt,
               {:error, :historical_creation_coverage, summary(history, history.event_count)}}
          end

        _ ->
          {:halt, {:error, :historical_creation_coverage, summary(history, history.event_count)}}
      end
    end)
  end

  defp residency_sql(dialect) do
    extract = fn path ->
      if dialect == :postgres,
        do: "record_json::jsonb #>> '{#{Enum.join(path, ",")}}'",
        else: "json_extract(record_json, '$.#{Enum.join(path, ".")}')"
    end

    # Any retained alias refuses, including a terminal intent whose remote
    # restore has not yet been reconciled. Compaction never deletes its owner.
    "SELECT id FROM residency_intents WHERE session_id = #{arg(dialect, 1)} OR " <>
      Enum.map_join(
        [["lineage_id"], ["identity", "session_id"]],
        " OR ",
        &"#{extract.(&1)} = #{arg(dialect, 1)}"
      ) <>
      " OR " <>
      Enum.map_join(
        [["restore", "vm_id"], ["identity", "vm_id"]],
        " OR ",
        &"#{extract.(&1)} = #{arg(dialect, 2)}"
      ) <> " LIMIT 1"
  end

  def summary(state, count) do
    %{
      history_complete: Map.get(state, :history_complete, false),
      creation_history_complete: false,
      event_count: count,
      terminal_state: if(state.state == "evicted", do: "evicted"),
      preceding_state: state.preceding_state || state.state,
      terminal_reason: state.terminal_reason
    }
  end

  def related_intent?(row, proof) do
    row["session_id"] == proof.session_id or row["lineage_id"] == proof.lineage_id or
      get_in(row, ["identity", "session_id"]) == proof.session_id or
      get_in(row, ["identity", "vm_id"]) == proof.vm_id or
      get_in(row, ["restore", "vm_id"]) == proof.vm_id
  end

  defp authorize_history(query, dialect, id, principal) do
    case query.(
           "SELECT principal FROM ops WHERE session_id = #{arg(dialect, 1)} AND kind = 'session_created' ORDER BY seq ASC LIMIT 2",
           [id]
         ) do
      {:ok, [[owner]]} -> owner_scope(principal, owner)
      {:ok, _} -> {:error, :historical_not_found, %{}}
      _ -> {:error, :historical_store_unavailable, %{}}
    end
  end

  defp owner_scope(:admin, _), do: :ok
  defp owner_scope(owner, owner) when is_binary(owner), do: :ok
  defp owner_scope(_, _), do: {:error, :historical_not_found, %{}}

  defp decoded(_decode, <<131, 80, _::binary>>),
    do: raise(ArgumentError, "compressed journal payload")

  defp decoded(decode, bytes) when is_binary(bytes) and byte_size(bytes) <= @payload_limit,
    do: decode.(bytes)

  defp event_summary(state, count, [_seq, _ts, _owner, _workload, _id, kind, bytes], decode) do
    reason = decoded(decode, bytes)["reason"]

    allowed = [
      "idled",
      "idle_ttl",
      "brick_gone",
      "vm_and_snapshot_vanished",
      "snapshot_vanished",
      "disk_pressure",
      "node_gone"
    ]

    summary(state, count)
    |> Map.put(:event_kind, if(kind in Enum.map(Embervm.OpLog.kinds(), &to_string/1), do: kind))
    |> Map.put(:event_reason, if(reason in allowed, do: reason))
  end

  defp event_summary(state, count, _row, _decode), do: summary(state, count)

  def public(history, observed_at) do
    Map.take(history, [
      :session_id,
      :lineage_id,
      :principal,
      :workload,
      :generation,
      :invoke_started_at,
      :updated_at,
      :last_invoke_at
    ])
    |> Map.merge(%{
      state: "evicted",
      historical_cessation: %{
        schema: 1,
        source: "retained_control_plane_journal",
        lineage_id: history.lineage_id,
        node_id: history.node_id,
        vm_id: history.vm_id,
        observed_at_unix_ms: observed_at,
        evidence_sha256: history.evidence_sha256,
        complete: true,
        no_successor: true,
        no_pending_restore: true,
        authority_scope: "single_writer_dispatch",
        coverage:
          "Complete retained original and creation history; absent durable and hot lineage; no dispatch-capable pending restore or residency aliases."
      }
    })
  end

  defp string?(v), do: is_binary(v) and byte_size(v) in 1..512
  defp integer?(v), do: is_integer(v) and v > 0 and v <= @max_integer
end

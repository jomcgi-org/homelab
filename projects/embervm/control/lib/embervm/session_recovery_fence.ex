defmodule Embervm.SessionRecoveryFence do
  @moduledoc """
  Exact retained-session recovery: prove a first interrupted dispatch was parked,
  then irrevocably exclude restoration of its terminal lineage. The fence is an
  audit projection, not a new lifecycle or an assertion about invocation results.
  """
  alias Embervm.{OpLog.Op, SessionCessationEvidence}
  @reason "factory_unknown_dispatch_recovery"
  @base_keys ~w(operation generation invoke_started_at updated_at dispatch_id reason request_key)

  def valid_request?(request) when is_map(request) do
    keys =
      if request["operation"] == "apply",
        do: @base_keys ++ ["expected_preview_sha256"],
        else: @base_keys

    Enum.sort(Map.keys(request)) == Enum.sort(keys) and
      request["operation"] in ["preview", "apply"] and
      request["reason"] == @reason and key?(request["request_key"]) and
      key?(request["dispatch_id"]) and
      SessionCessationEvidence.expected?(expected(request)) and
      (request["operation"] != "apply" or hash?(request["expected_preview_sha256"]))
  end

  def valid_request?(_), do: false

  def preview_request(request),
    do: request |> Map.put("operation", "preview") |> Map.delete("expected_preview_sha256")

  def expected(request),
    do: %{
      generation: request["generation"],
      invoke_started_at: request["invoke_started_at"],
      updated_at: request["updated_at"],
      dispatch_id: request["dispatch_id"]
    }

  # The adapter owns one transaction around the whole operation, including the
  # append and projection. The manager checks all transient authorities before
  # invoking an apply; this backend rechecks history and durable ownership.
  def run(query, dialect, decode, id, request, principal, now, append) do
    with true <- valid_request?(request) and is_binary(principal),
         {:ok, proof} <-
           SessionCessationEvidence.recovery_history(
             query,
             dialect,
             decode,
             id,
             expected(request),
             principal
           ),
         {:ok, owner} <- exact_owner(query, dialect, proof),
         :ok <- exact_interruption(owner.interrupted_turn, proof),
         proposal <- proposal(proof, request),
         {:ok, fenced} <-
           validate_saved_fence(owner.recovery_fence, proof.recovery_audit, proposal, proof),
         :ok <- expected_digest(request, proposal),
         {:ok, fence_seq} <- maybe_apply(request, owner, proof, proposal, fenced, now, append) do
      {:ok,
       proof
       |> Map.merge(%{
         tenant: owner.tenant,
         preview_sha256: proposal["preview_sha256"],
         evidence_sha256: proposal["evidence_sha256"],
         recovery_fence: proposal,
         fenced: fenced or request["operation"] == "apply",
         fence_seq: fence_seq
       })}
    else
      false -> {:error, :recovery_invalid_request, %{}}
      {:error, _, _} = error -> error
      _ -> {:error, :recovery_store_unavailable, %{}}
    end
  rescue
    _ -> {:error, :recovery_malformed_evidence, %{}}
  end

  defp arg(:postgres, n), do: "$#{n}"
  defp arg(:sqlite, n), do: "?#{n}"

  defp exact_owner(query, dialect, proof) do
    sql =
      "SELECT session_id, COALESCE(lineage_id, session_id), tenant, principal, workload, state, generation, " <>
        "invoke_started_at, last_invoke_at, updated_at, inflight_dispatch_id, turn_seq, terminal_reason, " <>
        "node_id, vm_id, volume_node_id, stop_intent_json, stop_completion_json, interrupted_turn_json, recovery_fence_json " <>
        "FROM sessions WHERE session_id = #{arg(dialect, 1)} OR COALESCE(lineage_id, session_id) = #{arg(dialect, 1)} OR vm_id = #{arg(dialect, 2)} LIMIT 2"

    case query.(sql, [proof.session_id, proof.vm_id]) do
      {:ok,
       [
         [
           id,
           id,
           tenant,
           principal,
           workload,
           "evicted",
           0,
           started,
           last,
           updated,
           dispatch,
           1,
           terminal,
           nil,
           nil,
           node,
           nil,
           nil,
           interrupted_json,
           fence_json
         ]
       ]}
      when id == proof.session_id and principal == proof.principal and workload == proof.workload and
             started == proof.invoke_started_at and last == proof.last_invoke_at and
             updated == proof.updated_at and
             dispatch == proof.dispatch_id and terminal == proof.terminal_reason and
             node == proof.node_id ->
        with {:ok, interruption} <- decode_record(interrupted_json),
             {:ok, fence} <- decode_record(fence_json) do
          {:ok, %{tenant: tenant, interrupted_turn: interruption, recovery_fence: fence}}
        end

      {:ok, _} ->
        {:error, :recovery_owner_changed, %{}}

      _ ->
        {:error, :recovery_store_unavailable, %{}}
    end
  end

  defp decode_record(nil), do: {:ok, nil}

  defp decode_record(bytes) when is_binary(bytes) and byte_size(bytes) <= 16_384 do
    case Jason.decode(bytes) do
      {:ok, value} when is_map(value) -> {:ok, value}
      _ -> {:error, :recovery_malformed_evidence, %{}}
    end
  end

  defp decode_record(_), do: {:error, :recovery_malformed_evidence, %{}}

  defp exact_interruption(
         %{
           "seq" => 1,
           "dispatch_id" => dispatch,
           "cli_session_id" => cli,
           "transcript_path" => path
         } = marker,
         proof
       )
       when dispatch == proof.dispatch_id and is_binary(cli) and byte_size(cli) in 1..512 and
              is_binary(path) and byte_size(path) in 1..4096 do
    if marker == proof.interrupted_identity,
      do: :ok,
      else: {:error, :recovery_identity_mismatch, %{}}
  end

  defp exact_interruption(_, _), do: {:error, :recovery_interruption_unavailable, %{}}

  defp proposal(proof, request) do
    evidence =
      digest(
        {:factory_unknown_dispatch_recovery, 1, proof.evidence_sha256, proof.dispatch_id,
         proof.terminal_reason}
      )

    identity =
      proof
      |> Map.take([
        :session_id,
        :lineage_id,
        :principal,
        :workload,
        :generation,
        :invoke_started_at,
        :last_invoke_at,
        :updated_at,
        :dispatch_id,
        :node_id,
        :vm_id,
        :terminal_reason
      ])
      |> Map.new(fn {k, v} -> {to_string(k), v} end)

    base =
      Map.merge(identity, %{
        "schema" => 1,
        "turn_seq" => 1,
        "request_key" => request["request_key"],
        "reason" => @reason,
        "evidence_sha256" => evidence
      })

    Map.put(base, "preview_sha256", digest(base))
  end

  defp validate_saved_fence(nil, nil, _proposal, _proof), do: {:ok, false}

  defp validate_saved_fence(saved, audit, proposal, proof) when is_map(saved) and is_map(audit) do
    if saved == proposal and audit.payload == %{"recovery_fence" => saved} and
         audit.principal == proof.principal and audit.workload == proof.workload and
         audit.session_id == proof.session_id and audit.seq > proof.last_seq and
         audit.ts >= proof.updated_at,
       do: {:ok, true},
       else: {:error, :recovery_fence_conflict, %{}}
  end

  defp validate_saved_fence(_, _, _, _), do: {:error, :recovery_fence_conflict, %{}}

  defp expected_digest(%{"operation" => "apply", "expected_preview_sha256" => hash}, %{
         "preview_sha256" => hash
       }),
       do: :ok

  defp expected_digest(%{"operation" => "preview"}, _), do: :ok
  defp expected_digest(_, _), do: {:error, :recovery_preview_changed, %{}}

  defp maybe_apply(_request, _owner, proof, _proposal, true, _now, _append),
    do: {:ok, proof.recovery_audit.seq}

  defp maybe_apply(%{"operation" => "preview"}, _owner, _proof, _proposal, false, _now, _append),
    do: {:ok, nil}

  defp maybe_apply(_request, owner, proof, proposal, false, now, append) do
    if is_integer(now) and now >= proof.updated_at do
      op = %Op{
        kind: :session_recovery_fenced,
        tenant: owner.tenant,
        principal: proof.principal,
        workload: proof.workload,
        session_id: proof.session_id,
        ts: now,
        payload: %{recovery_fence: proposal}
      }

      case append.(op) do
        {:ok, seq} when is_integer(seq) and seq > proof.last_seq -> {:ok, seq}
        _ -> {:error, :recovery_store_unavailable, %{}}
      end
    else
      {:error, :recovery_clock_invalid, %{}}
    end
  end

  def hot_owner?(row, proof) do
    row.session_id == proof.session_id and row.lineage_id == proof.lineage_id and
      row.principal == proof.principal and row.workload == proof.workload and
      row.state == :evicted and
      row.generation == 0 and row.invoke_started_at == proof.invoke_started_at and
      row.last_invoke_at == proof.last_invoke_at and row.updated_at == proof.updated_at and
      row.inflight_dispatch_id == proof.dispatch_id and row.turn_seq == 1 and
      row.terminal_reason == proof.terminal_reason and
      row.node_id == nil and row.vm_id == nil and row.volume_node_id == proof.node_id and
      row.interrupted_turn == proof.interrupted_identity
  end

  def public(proof, observed_at) do
    proof
    |> Map.take([
      :session_id,
      :lineage_id,
      :principal,
      :workload,
      :generation,
      :invoke_started_at,
      :last_invoke_at,
      :updated_at,
      :dispatch_id,
      :preview_sha256,
      :evidence_sha256,
      :fenced
    ])
    |> Map.merge(%{
      schema: 1,
      state: "evicted",
      turn_seq: 1,
      observed_at_unix_ms: observed_at,
      recovery_fence: %{
        schema: 1,
        source: "retained_control_plane_journal",
        request_key: proof.recovery_fence["request_key"],
        reason: @reason,
        node_id: proof.node_id,
        vm_id: proof.vm_id,
        terminal_reason: proof.terminal_reason,
        lineage_id: proof.lineage_id,
        authority_scope: "single_writer_dispatch",
        complete: true,
        no_successor: true,
        no_pending_restore: true,
        restore_excluded: proof.fenced
      }
    })
  end

  defp digest(value),
    do:
      :crypto.hash(:sha256, :erlang.term_to_binary(value, [:deterministic]))
      |> Base.encode16(case: :lower)

  defp key?(value),
    do:
      is_binary(value) and byte_size(value) in 1..256 and
        Regex.match?(~r/\A[A-Za-z0-9._:-]+\z/, value)

  defp hash?(value), do: is_binary(value) and Regex.match?(~r/\A[0-9a-f]{64}\z/, value)
end

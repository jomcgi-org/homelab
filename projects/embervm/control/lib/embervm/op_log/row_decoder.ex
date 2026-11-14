defmodule Embervm.OpLog.RowDecoder do
  @moduledoc false

  alias Embervm.OpLog.Codec
  alias Embervm.OpLog.Op

  def op([
         seq,
         ts,
         tenant,
         principal,
         workload,
         task_id,
         session_id,
         serving_instance_id,
         stateful_instance_id,
         group_instance_id,
         kind,
         payload_blob
       ]) do
    %Op{
      seq: seq,
      ts: ts,
      tenant: tenant,
      principal: principal,
      workload: workload,
      task_id: task_id,
      session_id: session_id,
      serving_instance_id: serving_instance_id,
      stateful_instance_id: stateful_instance_id,
      group_instance_id: group_instance_id,
      kind: String.to_existing_atom(kind),
      payload: Codec.decode_payload(payload_blob)
    }
  end

  def task([
         task_id,
         tenant,
         principal,
         workload,
         state,
         attempt,
         idempotency_key,
         submitted_at,
         updated_at,
         expires_at
       ]) do
    %{
      task_id: task_id,
      tenant: tenant,
      principal: principal,
      workload: workload,
      state: state,
      attempt: attempt,
      idempotency_key: idempotency_key,
      submitted_at: submitted_at,
      updated_at: updated_at,
      expires_at: expires_at
    }
  end

  def session([
          session_id,
          tenant,
          principal,
          workload,
          state,
          node_id,
          vm_id,
          volume_node_id,
          base_snapshot_ref,
          base_digest,
          generation,
          snapshot_ref,
          snapshot_size_bytes,
          token_sha256,
          created_at,
          invoke_started_at,
          last_invoke_at,
          expires_at,
          updated_at,
          terminal_reason,
          lineage_id,
          idempotency_key,
          stop_intent_json,
          stop_completion_json,
          turn_seq,
          interrupted_turn_json,
          inflight_dispatch_id,
          recovery_fence_json
        ]) do
    %{
      session_id: session_id,
      tenant: tenant,
      principal: principal,
      workload: workload,
      state: state,
      node_id: node_id,
      vm_id: vm_id,
      volume_node_id: volume_node_id,
      base_snapshot_ref: base_snapshot_ref,
      base_digest: base_digest,
      generation: generation,
      snapshot_ref: snapshot_ref,
      snapshot_size_bytes: snapshot_size_bytes,
      token_sha256: token_sha256,
      created_at: created_at,
      invoke_started_at: invoke_started_at,
      last_invoke_at: last_invoke_at,
      expires_at: expires_at,
      updated_at: updated_at,
      terminal_reason: terminal_reason,
      lineage_id: lineage_id,
      idempotency_key: idempotency_key,
      stop_intent: Embervm.SessionStopProof.decode(stop_intent_json),
      stop_completion: Embervm.SessionStopProof.decode(stop_completion_json),
      turn_seq: turn_seq,
      interrupted_turn: Embervm.SessionStopProof.decode(interrupted_turn_json),
      inflight_dispatch_id: inflight_dispatch_id,
      recovery_fence: Embervm.SessionStopProof.decode(recovery_fence_json)
    }
  end

  def serving_instance([
         instance_id,
         tenant,
         principal,
         workload,
         state,
         node_id,
         vm_id,
         ip,
         port,
         base_snapshot_ref,
         base_digest,
         generation,
         snapshot_ref,
         snapshot_size_bytes,
         created_at,
         last_active_at,
         updated_at,
         terminal_reason
       ]) do
    %{
      instance_id: instance_id,
      tenant: tenant,
      principal: principal,
      workload: workload,
      state: state,
      node_id: node_id,
      vm_id: vm_id,
      ip: ip,
      port: port,
      base_snapshot_ref: base_snapshot_ref,
      base_digest: base_digest,
      generation: generation,
      snapshot_ref: snapshot_ref,
      snapshot_size_bytes: snapshot_size_bytes,
      created_at: created_at,
      last_active_at: last_active_at,
      updated_at: updated_at,
      terminal_reason: terminal_reason
    }
  end

  def stateful_instance([
         instance_id,
         tenant,
         principal,
         workload,
         state,
         node_id,
         vm_id,
         ip,
         port,
         generation,
         snapshot_ref,
         snapshot_generation,
         snapshot_size_bytes,
         created_at,
         last_active_at,
         updated_at,
         terminal_reason
       ]) do
    %{
      instance_id: instance_id,
      tenant: tenant,
      principal: principal,
      workload: workload,
      state: state,
      node_id: node_id,
      vm_id: vm_id,
      ip: ip,
      port: port,
      generation: generation,
      snapshot_ref: snapshot_ref,
      snapshot_generation: snapshot_generation,
      snapshot_size_bytes: snapshot_size_bytes,
      created_at: created_at,
      last_active_at: last_active_at,
      updated_at: updated_at,
      terminal_reason: terminal_reason
    }
  end

  def group_instance([
         instance_id,
         tenant,
         principal,
         workload,
         state,
         node_id,
         subnet_cidr,
         entry_member,
         entry_port,
         listen_port,
         set_id,
         created_at,
         last_active_at,
         updated_at,
         terminal_reason
       ]) do
    %{
      instance_id: instance_id,
      tenant: tenant,
      principal: principal,
      workload: workload,
      state: state,
      node_id: node_id,
      subnet_cidr: subnet_cidr,
      entry_member: entry_member,
      entry_port: entry_port,
      listen_port: listen_port,
      set_id: set_id,
      created_at: created_at,
      last_active_at: last_active_at,
      updated_at: updated_at,
      terminal_reason: terminal_reason
    }
  end
end

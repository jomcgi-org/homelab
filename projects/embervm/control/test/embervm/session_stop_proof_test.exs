defmodule Embervm.SessionStopProofTest do
  use ExUnit.Case, async: true
  alias Embervm.SessionStopProof, as: Proof

  defp expected do
    %{"session_id" => "s-1", "generation" => 2, "invoke_started_at" => nil,
      "vm_id" => "vm-1", "node_id" => "node-1", "instance_id" => "node-1/pod-1",
      "pod_uid" => "pod-1", "boot_id" => "boot-1"}
  end

  test "null invoke identity survives JSON projection and HTTP input" do
    assert Proof.decode(Proof.encode(expected())) == expected()
    assert Proof.precondition?(Proof.from_json(Map.put(expected(), "invoke_started_at", :null)))
    refute Proof.precondition?(Map.delete(expected(), "invoke_started_at"))
    refute Proof.precondition?(Map.put(expected(), "boot_id", ""))
    refute Proof.precondition?(Map.put(expected(), "instance_id", "node-1/sibling"))
  end

  test "native proof must match every immutable identity and carry a positive timestamp" do
    intent = Proof.new_intent(expected(), 100)
    response = %{teardown_confirmed: true, completion: %{
      operation_id: intent["operation_id"], vm_id: "vm-1", boot_id: "boot-1",
      session_id: "s-1", node: "node-1", pod_uid: "pod-1", completed_at_unix_ms: 101}}
    assert {:ok, completion} = Proof.validate(intent, response)
    assert completion == Map.put(intent, "completed_at_unix_ms", 101)

    for field <- [:operation_id, :vm_id, :boot_id, :session_id, :node, :pod_uid] do
      bad = put_in(response, [:completion, field], "wrong")
      assert {:error, :invalid_stop_completion} = Proof.validate(intent, bad)
    end

    assert {:error, _} = Proof.validate(intent, %{teardown_confirmed: true})
    assert {:error, _} = Proof.validate(intent, %{response | teardown_confirmed: false})
    assert {:error, _} = Proof.validate(intent, put_in(response, [:completion, :completed_at_unix_ms], 0))
  end

  test "a node clock behind the control plane does not invalidate exact durable proof" do
    intent = Proof.new_intent(expected(), 10_000)
    response = %{teardown_confirmed: true, completion: %{
      operation_id: intent["operation_id"], vm_id: "vm-1", boot_id: "boot-1",
      session_id: "s-1", node: "node-1", pod_uid: "pod-1", completed_at_unix_ms: 9_000}}
    assert {:ok, completion} = Proof.validate(intent, response)
    row = %{session_id: "s-1", generation: 2, invoke_started_at: nil,
      state: :destroyed, stop_intent: intent, stop_completion: completion}
    assert Proof.completion(row) == completion
    assert {:error, _} = Proof.validate(intent, put_in(response, [:completion, :completed_at_unix_ms], true))
    assert {:error, _} = Proof.validate(Map.put(intent, "requested_at_unix_ms", 0), response)
  end

  test "terminal labels and mismatched or uncommitted completion expose no proof" do
    intent = Proof.new_intent(expected(), 100)
    completion = Map.put(intent, "completed_at_unix_ms", 101)
    row = %{session_id: "s-1", generation: 2, invoke_started_at: nil,
      state: :destroyed, stop_intent: intent, stop_completion: completion}
    assert Proof.completion(row) == completion
    assert is_nil(Proof.completion(%{row | state: :destroying}))
    assert is_nil(Proof.completion(%{row | stop_intent: nil}))
    assert is_nil(Proof.completion(%{row | stop_completion: nil}))
    assert is_nil(Proof.completion(%{row | invoke_started_at: 102}))
    assert is_nil(Proof.completion(%{row | generation: 3}))
  end

  test "inventory identity requires a unique exact VM, daemon instance and boot" do
    session = %{state: :running, session_id: "s-1", generation: 2, invoke_started_at: nil,
      vm_id: "vm-1", node_id: "node-1"}
    fact = %{configured_id: "node-1", instance_id: "node-1/pod-1", pod_uid: "pod-1",
      boot_id: "boot-1", session_vms: [%{session_id: "s-1", vm_id: "vm-1"}]}
    assert Proof.identity(session, [fact]) == expected()
    assert is_nil(Proof.identity(session, []))
    assert is_nil(Proof.identity(session, [Map.delete(fact, :boot_id)]))
    assert is_nil(Proof.identity(session, [fact, fact]))
    assert is_nil(Proof.identity(%{session | vm_id: "vm-new"}, [fact]))
  end
end

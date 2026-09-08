defmodule Embervm.SessionStopProof do
  @moduledoc """
  Exact stop identity and durable proof validation. Inventory can name a current
  owner; it can never prove that an invocation or its VM has stopped.
  """

  @identity_keys ~w(session_id generation invoke_started_at vm_id node_id instance_id pod_uid boot_id)
  @string_keys ~w(session_id vm_id node_id instance_id pod_uid boot_id)

  def precondition?(value) when is_map(value) do
    Enum.sort(Map.keys(value)) == Enum.sort(@identity_keys) and
      Enum.all?(@string_keys, &(is_binary(value[&1]) and value[&1] != "")) and
      is_integer(value["generation"]) and value["generation"] >= 0 and
      (is_nil(value["invoke_started_at"]) or
         (is_integer(value["invoke_started_at"]) and value["invoke_started_at"] >= 0)) and
      value["instance_id"] == value["node_id"] <> "/" <> value["pod_uid"]
  end

  def precondition?(_), do: false

  def identity(%{state: :running} = session, facts) do
    candidates =
      for fact <- facts,
          vm <- Map.get(fact, :session_vms, []) || [],
          vm.session_id == session.session_id,
          vm.vm_id == session.vm_id,
          fact.configured_id == session.node_id do
        %{
          "session_id" => session.session_id,
          "generation" => session.generation,
          "invoke_started_at" => session.invoke_started_at,
          "vm_id" => vm.vm_id,
          "node_id" => fact.configured_id,
          "instance_id" => Map.get(fact, :instance_id),
          "pod_uid" => Map.get(fact, :pod_uid),
          "boot_id" => Map.get(fact, :boot_id)
        }
      end

    case candidates do
      [candidate] -> if precondition?(candidate), do: candidate
      _ -> nil
    end
  end

  def identity(_session, _facts), do: nil

  def row_matches?(session, expected) do
    precondition?(expected) and session.state == :running and
      session.session_id == expected["session_id"] and
      session.generation == expected["generation"] and
      session.invoke_started_at == expected["invoke_started_at"] and
      session.vm_id == expected["vm_id"] and session.node_id == expected["node_id"]
  end

  def new_intent(expected, now) do
    Map.merge(expected, %{
      "operation_id" => "stop-" <> Base.url_encode64(:crypto.strong_rand_bytes(24), padding: false),
      "requested_at_unix_ms" => now
    })
  end

  def precondition(intent) when is_map(intent), do: Map.take(intent, @identity_keys)
  def precondition(_), do: nil

  def valid_intent?(intent) when is_map(intent) do
    precondition?(precondition(intent)) and
      is_binary(intent["operation_id"]) and intent["operation_id"] != "" and
      is_integer(intent["requested_at_unix_ms"]) and intent["requested_at_unix_ms"] > 0
  end

  def valid_intent?(_), do: false

  def validate(intent, %{teardown_confirmed: true, completion: completion})
      when is_map(completion) do
    pairs = [operation_id: "operation_id", vm_id: "vm_id", boot_id: "boot_id",
      session_id: "session_id", node: "node_id", pod_uid: "pod_uid"]
    completed_at = Map.get(completion, :completed_at_unix_ms)

    # The persisted operation is the causal link. These two wall clocks come
    # from different hosts, so their relative values cannot establish ordering.

    if valid_intent?(intent) and
         Enum.all?(pairs, fn {field, key} -> Map.get(completion, field) == intent[key] end) and
         is_integer(completed_at) and completed_at > 0 do
      {:ok, Map.put(intent, "completed_at_unix_ms", completed_at)}
    else
      {:error, :invalid_stop_completion}
    end
  end

  def validate(_intent, _response), do: {:error, :invalid_stop_completion}

  def completion(session) do
    intent = Map.get(session, :stop_intent)
    completion = Map.get(session, :stop_completion)

    if session.state == :destroyed and valid_intent?(intent) and is_map(completion) and
         Map.delete(completion, "completed_at_unix_ms") == intent and
         is_integer(completion["completed_at_unix_ms"]) and
         completion["completed_at_unix_ms"] > 0 and
         session.session_id == intent["session_id"] and
         session.generation == intent["generation"] and
         session.invoke_started_at == intent["invoke_started_at"], do: completion
  end

  def encode(nil), do: nil
  def encode(value), do: value |> :json.encode(&encode_value/2) |> IO.iodata_to_binary()
  defp encode_value(nil, _encoder), do: "null"
  defp encode_value(value, encoder), do: :json.encode_value(value, encoder)
  def decode(nil), do: nil
  def decode(value), do: value |> :json.decode() |> from_json()
  def from_json(%{"invoke_started_at" => :null} = value), do: Map.put(value, "invoke_started_at", nil)
  def from_json(value), do: value
end

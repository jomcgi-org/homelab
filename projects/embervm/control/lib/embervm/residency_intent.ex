defmodule Embervm.ResidencyIntent do
  @moduledoc """
  Durable demand for an existing session, independent of its lifecycle and of
  factory work. Records contain identity and admission metadata, never prompts
  or capabilities. SessionStore serializes these decisions with invoke-start.
  """

  @active ["queued", "admitting", "ready", "cancelling"]
  @terminal ["consumed", "cancelled", "expired", "failed"]

  def active?(intent), do: intent["state"] in @active
  def terminal?(intent), do: intent["state"] in @terminal

  def new(session, attrs, now) do
    with key when is_binary(key) and byte_size(key) in 1..128 <- attrs["key"],
         generation when is_integer(generation) and generation >= 0 <- attrs["generation"],
         true <- generation == (session.generation || 0),
         deadline when is_integer(deadline) and deadline > now <- attrs["deadline"],
         task_deadline when is_integer(task_deadline) <- attrs["task_deadline"],
         true <- deadline <= task_deadline and deadline <= session.expires_at,
         first_wait when is_integer(first_wait) and first_wait <= now <- Map.get(attrs, "first_wait_at", now) do
      fingerprint = :crypto.hash(:sha256, :erlang.term_to_binary(
        {session.session_id, generation, deadline, task_deadline, first_wait})) |> Base.encode16(case: :lower)

      {:ok, %{
        "id" => "ri-" <> Base.url_encode64(:crypto.strong_rand_bytes(18), padding: false),
        "key" => key, "fingerprint" => fingerprint,
        "session_id" => session.session_id, "principal" => session.principal,
        "workload" => session.workload, "lineage_id" => session.lineage_id,
        "generation" => generation, "deadline" => deadline,
        "task_deadline" => task_deadline, "first_wait_at" => first_wait,
        "created_at" => now, "updated_at" => now, "state" => "queued",
        "reason" => "capacity", "version" => 0, "attempt_epoch" => 0,
        "residency_epoch" => nil, "restore" => nil, "dispatch_id" => nil,
        "retry_after_ms" => 5000
      }}
    else
      false -> {:error, if(attrs["generation"] != (session.generation || 0), do: :stale_generation, else: :invalid_deadline)}
      _ -> {:error, :invalid_residency_request}
    end
  end

  def replay?(intent, session, attrs) do
    first_wait = Map.get(attrs, "first_wait_at", intent["first_wait_at"])
    fingerprint = :crypto.hash(:sha256, :erlang.term_to_binary(
      {session.session_id, attrs["generation"], attrs["deadline"], attrs["task_deadline"], first_wait})) |> Base.encode16(case: :lower)
    intent["fingerprint"] == fingerprint
  end

  def change(intent, version, updates, now) do
    next = Map.get(updates, "state", intent["state"])
    allowed = case intent["state"] do
      "queued" -> ["queued", "admitting", "ready", "cancelled", "expired", "failed"]
      "admitting" -> ["admitting", "queued", "ready", "cancelling", "failed"]
      "ready" -> ["ready", "queued", "consumed", "cancelled", "expired", "failed"]
      "cancelling" -> ["cancelling", "cancelled", "expired", "failed"]
      _ -> []
    end

    if version == intent["version"] and next in allowed do
      {:ok, intent |> Map.merge(updates) |> Map.put("version", version + 1) |> Map.put("updated_at", now)}
    else
      {:error, :stale_residency_observation}
    end
  end

  def cancel(intent, now, reason \\ "cancelled") do
    cond do
      intent["state"] == "consumed" -> {:error, :already_consumed}
      terminal?(intent) -> {:ok, intent}
      intent["state"] in ["admitting", "cancelling"] ->
        change(intent, intent["version"], %{"state" => "cancelling", "reason" => reason}, now)
      true -> change(intent, intent["version"], %{"state" => reason, "reason" => reason}, now)
    end
  end

  def identity(session, dial_id) do
    %{"session_id" => session.session_id, "generation" => session.generation || 0,
      "node_id" => session.node_id, "vm_id" => session.vm_id, "dial_id" => dial_id}
  end

  def handoff(intent, session, epoch, dispatch_id, identity, now) do
    cond do
      intent["state"] == "consumed" -> {:error, :already_consumed}
      not is_binary(dispatch_id) or dispatch_id == "" -> {:error, :invalid_dispatch}
      intent["session_id"] != session.session_id -> {:error, :not_found}
      intent["state"] != "ready" -> {:not_dispatched, intent["reason"]}
      now >= intent["deadline"] or now >= session.expires_at -> {:not_dispatched, "expired"}
      intent["generation"] != (session.generation || 0) -> {:not_dispatched, "stale_generation"}
      session.state != :running or intent["residency_epoch"] != epoch or intent["identity"] != identity ->
        {:not_dispatched, "residency_changed"}
      true -> change(intent, intent["version"], %{"state" => "consumed", "reason" => "handoff_accepted", "dispatch_id" => dispatch_id}, now)
    end
  end

  # Bounded round-robin across principals, FIFO among fitting candidates within
  # each principal. A bypass never changes first_wait_at or the deadline.
  def candidates(intents, after_principal, limit) do
    groups = intents |> Enum.filter(&(&1["state"] == "queued"))
      |> Enum.sort_by(&{&1["first_wait_at"], &1["queue_order"] || &1["created_at"], &1["id"]})
      |> Enum.group_by(& &1["principal"])
    principals = Map.keys(groups) |> Enum.sort()
    {before, after_keys} = Enum.split_while(principals, &(&1 <= (after_principal || "")))
    ordered = after_keys ++ before
    Stream.unfold(groups, fn remaining ->
      if map_size(remaining) == 0 do
        nil
      else
        {round, rest} = Enum.reduce(ordered, {[], remaining}, fn principal, {items, rest} ->
          case Map.get(rest, principal) do
            [head | tail] ->
              rest = if tail == [], do: Map.delete(rest, principal), else: Map.put(rest, principal, tail)
              {[head | items], rest}
            _ -> {items, rest}
          end
        end)
        {Enum.reverse(round), rest}
      end
    end) |> Stream.flat_map(& &1) |> Enum.take(limit)
  end
end

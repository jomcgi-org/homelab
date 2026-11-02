defmodule Embervm.WakeLimit do
  @moduledoc """
  The per-principal sliding-window wake-rate limit shared by the wake managers.

  Operates on a manager's state map through its `wake_max`, `wake_window_ms`,
  `wake_events` (principal => wake timestamps) and `clock` fields. A wake is
  allowed while the count of timestamps inside the window is below `wake_max`; a
  `wake_max` that is not a positive integer disables the limit (never
  rate-limit), matching the "0 = off" config idiom.
  """

  @spec allowed?(map(), term()) :: boolean()
  def allowed?(%{wake_max: max}, _principal) when not is_integer(max) or max <= 0, do: true

  def allowed?(state, principal) do
    now = state.clock.()
    length(recent(state, principal, now)) < state.wake_max
  end

  @doc "Record a wake for `principal` now, dropping its timestamps outside the window."
  @spec record(map(), term()) :: map()
  def record(state, principal) do
    now = state.clock.()
    recent = recent(state, principal, now)
    %{state | wake_events: Map.put(state.wake_events, principal, [now | recent])}
  end

  defp recent(state, principal, now) do
    cutoff = now - state.wake_window_ms

    state.wake_events
    |> Map.get(principal, [])
    |> Enum.filter(&(&1 > cutoff))
  end
end

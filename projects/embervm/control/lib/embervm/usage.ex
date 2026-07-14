defmodule Embervm.Usage do
  @moduledoc """
  The metering formula, in exactly one place. A task's raw resource facts
  (`cpu_ms`, `peak_rss_mib`, `wall_ms`, sampled host-side by the daemon and
  carried on `AssignResponse.usage`) reduce to the two billed quantities the
  control plane charges and exposes: vCPU-seconds and GB-seconds.

    * `vcpu_seconds = cpu_ms / 1000`
    * `gb_seconds   = (peak_rss_mib / 1024) * (wall_ms / 1000)`

  `Embervm.TaskStore` computes these once at completion and stores BOTH the raw
  counters and the billed pair in the `:succeeded`/`:failed` op payload: the raw
  counters are kept per task so the billing basis can be rebased later (e.g. to
  allocated `memMib` rather than observed peak RSS) without losing history, while
  the op-log's `usage` projection accumulates the billed pair. Keeping the formula
  here means the projection and the quota cache never disagree about what a task
  cost.
  """

  @type stats :: %{cpu_ms: integer(), peak_rss_mib: integer(), wall_ms: integer()}
  @type billed :: %{vcpu_seconds: float(), gb_seconds: float()}

  @doc """
  Normalizes an `AssignResponse.usage` value (a `UsageStats` struct, or `nil`
  when no usage was reported) to a plain map of the raw counters, defaulting any
  absent field to 0. Returns `nil` for a missing usage struct: a transport- or
  timeout-level failure never produces usage, and `nil` is the signal to charge
  nothing (see `Embervm.Dispatcher`).
  """
  @spec from_proto(nil | map()) :: stats() | nil
  def from_proto(nil), do: nil

  def from_proto(usage) when is_map(usage) do
    %{
      cpu_ms: int(Map.get(usage, :cpu_ms)),
      peak_rss_mib: int(Map.get(usage, :peak_rss_mib)),
      wall_ms: int(Map.get(usage, :wall_ms))
    }
  end

  @doc "The billed pair (vCPU-seconds, GB-seconds) from raw stats."
  @spec billed(stats()) :: billed()
  def billed(%{cpu_ms: cpu_ms, peak_rss_mib: peak_rss_mib, wall_ms: wall_ms}) do
    %{vcpu_seconds: vcpu_seconds(cpu_ms), gb_seconds: gb_seconds(peak_rss_mib, wall_ms)}
  end

  @spec vcpu_seconds(number()) :: float()
  def vcpu_seconds(cpu_ms), do: cpu_ms / 1000

  @spec gb_seconds(number(), number()) :: float()
  def gb_seconds(peak_rss_mib, wall_ms), do: peak_rss_mib / 1024 * (wall_ms / 1000)

  @doc """
  Whether all raw counters are zero. proto3 int64 defaults to 0, so a daemon
  that never populated `UsageStats` yields all-zero usage that would silently
  disable metering and quota; callers log-once on this so the gap is visible.
  """
  @spec all_zero?(stats()) :: boolean()
  def all_zero?(%{cpu_ms: 0, peak_rss_mib: 0, wall_ms: 0}), do: true
  def all_zero?(_), do: false

  defp int(n) when is_integer(n), do: n
  defp int(_), do: 0
end

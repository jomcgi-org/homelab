defmodule Embervm.PrimedOp do
  @moduledoc """
  Builds the `:primed` op, shared by every lane that primes a VM.

  ONE builder on purpose. There are three append sites (the dispatcher's
  cold-miss path, the PoolManager's warm-pool refill, and the SessionManager's
  session prime), and a conformance checker joins `:primed` to `:assigned` on
  `(vm_id, node_id)`. Three hand-written payloads drift: an earlier version of
  this instrumentation had one site recording the brick `dial_id` as the node
  while another recorded `node_id`, which silently breaks that join.

  TIMESTAMPS ARE TAKEN HERE, and the caller may NOT inject a clock. That is a
  deliberate restriction, not an oversight, because the obvious injection is a
  trap: the two calling modules use the name `clock` for opposite things.

      SessionManager  clock            -> System.system_time    (WALL)
      SessionManager  monotonic_clock  -> System.monotonic_time  (MONOTONIC)
      PoolManager     clock            -> System.monotonic_time  (MONOTONIC)

  `System.monotonic_time/1` has an arbitrary origin and is not a Unix epoch
  value, so passing the wrong one writes a nonsense `ts` into the durable
  op-log. It still appends, so nothing fails: the trace validator simply orders
  events by a garbage field. Taking the wall clock here makes that unreachable.

  Tests that need a deterministic timestamp should assert on the payload and
  treat `ts` as opaque, or freeze time at the boundary rather than threading a
  clock through this call.
  """

  alias Embervm.OpLog.Op

  @spec build(String.t(), String.t(), String.t(), String.t(), atom()) :: Op.t()
  def build(tenant, workload, vm_id, node_id, lane) do
    %Op{
      kind: :primed,
      tenant: tenant,
      workload: workload,
      ts: System.system_time(:millisecond),
      payload: %{lane: lane, workload: workload, vm_id: vm_id, node_id: node_id}
    }
  end
end

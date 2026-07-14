defmodule Embervm.Trigger.CronTest do
  @moduledoc """
  Acceptance for the cron `Embervm.TriggerAdapter`: a due trigger fires as an
  ordinary submit with a `system:cron:<workload>` principal, and a misfire during
  downtime is skipped (next-fire is always forward from now). The clock and the
  submit sink are injected so the whole fire path is deterministic.
  """
  use ExUnit.Case, async: true

  alias Embervm.Trigger.Cron, as: CronTrigger
  alias Embervm.WorkloadCatalog

  defp dt(h, mi, s), do: DateTime.new!(Date.new!(2026, 7, 13), Time.new!(h, mi, s), "Etc/UTC")

  # A settable clock: reconcile/tick read whatever the agent currently holds.
  defp clock_agent(initial) do
    {:ok, agent} = Agent.start_link(fn -> initial end)
    {agent, fn -> Agent.get(agent, & &1) end}
  end

  defp set_clock(agent, dt), do: Agent.update(agent, fn _ -> dt end)

  defp recorder do
    {:ok, agent} = Agent.start_link(fn -> [] end)
    submit = fn attrs -> Agent.update(agent, &[attrs | &1]) end
    {agent, submit}
  end

  defp catalog_with_trigger(cron_expr, payload) do
    table = WorkloadCatalog.table()
    wl = "wl-cron-#{System.unique_integer([:positive])}"

    WorkloadCatalog.upsert(table, wl, %{
      name: wl,
      namespace: "embervm",
      invoke_path: "/run",
      triggers: [%{cron: cron_expr, payload: payload}]
    })

    on_exit(fn -> WorkloadCatalog.drop(table, wl) end)
    {table, wl}
  end

  test "a due trigger fires a submit with the system:cron principal and JSON body" do
    {table, wl} = catalog_with_trigger("*/1 * * * *", %{"foo" => "bar"})
    {clock_a, clock} = clock_agent(dt(10, 0, 30))
    {rec, submit} = recorder()

    {:ok, cron} =
      CronTrigger.start_link(
        name: nil,
        catalog_table: table,
        clock: clock,
        submit_fun: submit,
        start_ticking: false
      )

    :ok = CronTrigger.reconcile(cron)

    # Not yet due at 10:00:45 (next fire is 10:01:00).
    set_clock(clock_a, dt(10, 0, 45))
    assert {:ok, 0} = CronTrigger.tick(cron)
    assert Agent.get(rec, & &1) == []

    # Due at 10:01:30.
    set_clock(clock_a, dt(10, 1, 30))
    assert {:ok, 1} = CronTrigger.tick(cron)

    [attrs] = Agent.get(rec, & &1)
    assert attrs.principal == "system:cron:" <> wl
    assert attrs.workload == wl
    assert attrs.request.path == "/run"
    body = Base.decode64!(attrs.request.body_b64)
    assert body =~ "foo"
  end

  test "misfires during downtime are skipped, not replayed" do
    # Start the clock well past several would-be fires; reconcile computes the
    # next fire FORWARD from now, so the missed ticks are never scheduled.
    {table, _wl} = catalog_with_trigger("*/1 * * * *", nil)
    {clock_a, clock} = clock_agent(dt(10, 0, 30))
    {rec, submit} = recorder()

    {:ok, cron} =
      CronTrigger.start_link(
        name: nil,
        catalog_table: table,
        clock: clock,
        submit_fun: submit,
        start_ticking: false
      )

    :ok = CronTrigger.reconcile(cron)

    # Jump the clock forward by an hour without ticking (simulated downtime).
    # Only the ONE next-fire scheduled at reconcile is pending, so a single tick
    # fires exactly once, not 60 times for the skipped minutes (skipped, not
    # replayed), and recomputes the next fire forward from now.
    set_clock(clock_a, dt(11, 0, 30))
    assert {:ok, fired} = CronTrigger.tick(cron)
    assert fired == 1
    assert length(Agent.get(rec, & &1)) == 1
  end
end

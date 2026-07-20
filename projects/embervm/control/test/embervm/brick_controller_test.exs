defmodule Embervm.BrickControllerTest do
  # Brick-capacity PR-3a: the controller reconciles each size-class brick
  # Deployment's replica count to its desired value and flags a class fleet-full
  # when desired outruns registered past the dwell. Drives reconcile
  # deterministically through injected scale_fun/registered_fun/clock +
  # reconcile_now (start_on: false, a long interval so the real timer never races).
  use ExUnit.Case, async: true

  alias Embervm.BrickController

  # A controllable clock + a scale-call recorder, both backed by Agents.
  defp new_clock do
    {:ok, pid} = Agent.start_link(fn -> 0 end)
    on_exit(fn -> if Process.alive?(pid), do: Agent.stop(pid) end)
    {fn -> Agent.get(pid, & &1) end, fn ms -> Agent.update(pid, &(&1 + ms)) end}
  end

  defp new_recorder do
    {:ok, pid} = Agent.start_link(fn -> [] end)
    on_exit(fn -> if Process.alive?(pid), do: Agent.stop(pid) end)

    record = fn ns, name, replicas ->
      Agent.update(pid, &[{ns, name, replicas} | &1])
      :ok
    end

    calls = fn -> pid |> Agent.get(& &1) |> Enum.reverse() end
    {record, calls}
  end

  defp start(opts) do
    defaults = [
      name: nil,
      namespace: "embervm",
      deployment_prefix: "embervm-embervm-noded-brick-",
      interval_ms: 3_600_000,
      fleet_full_after_ms: 300_000,
      reconcile_on_start: false,
      registered_fun: fn -> %{} end
    ]

    {:ok, pid} = BrickController.start_link(Keyword.merge(defaults, opts))
    on_exit(fn -> if Process.alive?(pid), do: GenServer.stop(pid) end)
    pid
  end

  test "reconcile scales every configured class to its desired count" do
    {record, calls} = new_recorder()

    pid =
      start(
        classes: [%{name: "2gi", desired: 1}, %{name: "16gi", desired: 2}],
        scale_fun: record
      )

    BrickController.reconcile_now(pid)

    assert calls.() == [
             {"embervm", "embervm-embervm-noded-brick-2gi", 1},
             {"embervm", "embervm-embervm-noded-brick-16gi", 2}
           ]
  end

  test "empty class list is inert: no scale calls, no flags" do
    {record, calls} = new_recorder()
    pid = start(classes: [], scale_fun: record)

    BrickController.reconcile_now(pid)

    assert calls.() == []
    assert BrickController.flagged(pid) == MapSet.new()
  end

  test "a scale error never crashes the loop" do
    pid =
      start(
        classes: [%{name: "2gi", desired: 1}],
        scale_fun: fn _ns, _name, _r -> {:error, {:apiserver_status, 404}} end
      )

    # Two reconciles despite the persistent error prove the GenServer stays alive.
    assert BrickController.reconcile_now(pid) == :ok
    assert BrickController.reconcile_now(pid) == :ok
    assert Process.alive?(pid)
  end

  test "flags a class fleet-full only after desired>registered past the dwell" do
    {clock, advance} = new_clock()

    pid =
      start(
        classes: [%{name: "2gi", desired: 3}],
        # Only 1 of the 3 desired bricks ever registers (node full).
        registered_fun: fn -> %{"2gi" => 1} end,
        scale_fun: fn _ns, _name, _r -> :ok end,
        fleet_full_after_ms: 300_000,
        clock: clock
      )

    # First reconcile starts the over-window; not yet flagged.
    BrickController.reconcile_now(pid)
    refute BrickController.fleet_full?(pid, "2gi")

    # Still inside the dwell.
    advance.(299_000)
    BrickController.reconcile_now(pid)
    refute BrickController.fleet_full?(pid, "2gi")

    # Past the dwell -> flagged.
    advance.(2_000)
    BrickController.reconcile_now(pid)
    assert BrickController.fleet_full?(pid, "2gi")
  end

  test "a flagged class clears once registered catches up to desired" do
    {clock, advance} = new_clock()
    {:ok, reg} = Agent.start_link(fn -> %{"2gi" => 1} end)
    on_exit(fn -> if Process.alive?(reg), do: Agent.stop(reg) end)

    pid =
      start(
        classes: [%{name: "2gi", desired: 2}],
        registered_fun: fn -> Agent.get(reg, & &1) end,
        scale_fun: fn _ns, _name, _r -> :ok end,
        fleet_full_after_ms: 100,
        clock: clock
      )

    BrickController.reconcile_now(pid)
    advance.(200)
    BrickController.reconcile_now(pid)
    assert BrickController.fleet_full?(pid, "2gi")

    # The second brick registers; next reconcile clears the flag.
    Agent.update(reg, fn _ -> %{"2gi" => 2} end)
    BrickController.reconcile_now(pid)
    refute BrickController.fleet_full?(pid, "2gi")
  end
end

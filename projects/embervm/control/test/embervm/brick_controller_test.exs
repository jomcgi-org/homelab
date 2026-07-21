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

  # -- desired/2 (the pure autoscale decision, Axis C) ------------------------

  defp base_signals(overrides \\ %{}) do
    Map.merge(
      %{
        min: 0,
        max: 4,
        denials: 0,
        up_threshold: 3,
        fleet_full_now: false,
        fleet_full_recent: false,
        idle_dwell_ok: false,
        up_cooldown_ok: true,
        down_cooldown_ok: true
      },
      overrides
    )
  end

  test "desired/2 steps up one under denial pressure, clamped to max" do
    assert BrickController.desired(1, base_signals(%{denials: 3})) == {2, :denial_pressure}
    assert BrickController.desired(4, base_signals(%{denials: 3})) == {4, :at_max}
  end

  test "desired/2 refuses up while fleet-full or inside the up cooldown" do
    assert BrickController.desired(1, base_signals(%{denials: 3, fleet_full_now: true})) ==
             {1, :fleet_full_wait}

    assert BrickController.desired(1, base_signals(%{denials: 3, up_cooldown_ok: false})) ==
             {1, :up_cooldown}
  end

  test "desired/2 jumps to the min floor and steps down from over max" do
    assert BrickController.desired(0, base_signals(%{min: 1})) == {1, :min_floor}
    assert BrickController.desired(6, base_signals(%{max: 4})) == {5, :over_max}
  end

  test "desired/2 steps down only on a clean idle dwell" do
    assert BrickController.desired(2, base_signals(%{idle_dwell_ok: true})) == {1, :idle_drain}

    # Any of: denials in the window, a recent fleet-full episode, the down
    # cooldown, or sitting at min already, holds the count steady.
    assert BrickController.desired(2, base_signals(%{idle_dwell_ok: true, denials: 1})) ==
             {2, :steady}

    assert BrickController.desired(2, base_signals(%{idle_dwell_ok: true, fleet_full_recent: true})) ==
             {2, :steady}

    assert BrickController.desired(2, base_signals(%{idle_dwell_ok: true, down_cooldown_ok: false})) ==
             {2, :steady}

    assert BrickController.desired(1, base_signals(%{min: 1, idle_dwell_ok: true})) ==
             {1, :steady}
  end

  # -- observe mode (phase 1: decisions logged, never acted) -------------------

  test "observe mode still scales statically and only LOGS the autoscale target" do
    {record, calls} = new_recorder()
    {clock, advance} = new_clock()

    pid =
      start(
        mode: :observe,
        classes: [%{name: "2gi", desired: 1, min: 0, max: 4}],
        scale_fun: record,
        scale_get_fun: fn _ns, _name -> {:ok, 1} end,
        facts_fun: fn -> [] end,
        registered_fun: fn -> %{"2gi" => 1} end,
        up_threshold: 1,
        clock: clock
      )

    # A denial for a 2gi-sized need crosses the (test) threshold of 1.
    BrickController.note_denial(pid, 512)
    advance.(1)

    log =
      ExUnit.CaptureLog.capture_log(fn ->
        BrickController.reconcile_now(pid)
      end)

    assert log =~ "brick autoscale: would scale class 2gi from 1 to 2 (reason=denial_pressure)"
    # The ACTING write stayed the static desired (1), not the target (2).
    assert calls.() == [{"embervm", "embervm-embervm-noded-brick-2gi", 1}]
  end

  # -- up mode (phase 2: scale-up acts, scale-down stays observe-only) ---------

  test "up mode acts on denial pressure and writes the target" do
    {record, calls} = new_recorder()
    {clock, advance} = new_clock()

    pid =
      start(
        mode: :up,
        classes: [%{name: "2gi", desired: 1, min: 0, max: 4}],
        scale_fun: record,
        scale_get_fun: fn _ns, _name -> {:ok, 1} end,
        facts_fun: fn -> [] end,
        registered_fun: fn -> %{"2gi" => 1} end,
        up_threshold: 1,
        clock: clock
      )

    BrickController.note_denial(pid, 512)
    advance.(1)

    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "brick autoscale: scaling class 2gi from 1 to 2 (reason=denial_pressure)"
    assert calls.() == [{"embervm", "embervm-embervm-noded-brick-2gi", 2}]
  end

  test "up mode asserts the live current and leaves a down decision as a log" do
    {record, calls} = new_recorder()
    {clock, advance} = new_clock()

    pid =
      start(
        mode: :up,
        # Static desired is 1 but the live Deployment sits at 3 (a prior
        # scale-up): the acting baseline is the LIVE count, and the idle-drain
        # decision must NOT act in :up mode.
        classes: [%{name: "2gi", desired: 1, min: 0, max: 4}],
        scale_fun: record,
        scale_get_fun: fn _ns, _name -> {:ok, 3} end,
        facts_fun: fn ->
          [%{size_class: "2gi", live_vms: 0, draining: false}]
        end,
        registered_fun: fn -> %{"2gi" => 3} end,
        down_idle_ms: 100,
        clock: clock
      )

    # First reconcile starts the idle dwell; past it, the down decision fires.
    BrickController.reconcile_now(pid)
    advance.(200)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "brick autoscale: would scale class 2gi from 3 to 2 (reason=idle_drain)"
    # Both ticks wrote the LIVE current (3), never the static desired (1).
    assert calls.() == [
             {"embervm", "embervm-embervm-noded-brick-2gi", 3},
             {"embervm", "embervm-embervm-noded-brick-2gi", 3}
           ]
  end

  test "a denial is attributed to the smallest class that fits the need" do
    {clock, advance} = new_clock()

    pid =
      start(
        mode: :observe,
        classes: [
          %{name: "2gi", desired: 0, min: 0, max: 2},
          %{name: "16gi", desired: 0, min: 0, max: 2}
        ],
        scale_fun: fn _ns, _name, _r -> :ok end,
        scale_get_fun: fn _ns, _name -> {:ok, 0} end,
        facts_fun: fn -> [] end,
        registered_fun: fn -> %{} end,
        up_threshold: 1,
        clock: clock
      )

    # 4096 MiB does not fit the 2gi class: the denial lands on 16gi.
    BrickController.note_denial(pid, 4096)
    advance.(1)

    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "would scale class 16gi from 0 to 1"
    refute log =~ "would scale class 2gi"
  end
end

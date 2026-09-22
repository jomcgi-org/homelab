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
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)
    {fn -> Agent.get(pid, & &1) end, fn ms -> Agent.update(pid, &(&1 + ms)) end}
  end

  defp new_recorder do
    {:ok, pid} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)

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
      registered_fun: fn -> %{} end,
      catalog_fun: fn -> [] end
    ]

    {:ok, pid} = BrickController.start_link(Keyword.merge(defaults, opts))
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)
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

  test "catalog floors raise the static target from zero replicas" do
    {record, calls} = new_recorder()

    pid =
      start(
        classes: [
          %{
            name: "2gi",
            desired: 0,
            min: 0,
            max: 3,
            usable_mib: 1_792,
            mem_reject_floor_mib: 512,
            slots: 8
          }
        ],
        catalog_fun: fn -> [%{name: "warm", floor: 2, mem_mib: 600}] end,
        scale_fun: record,
        registered_fun: fn -> %{} end
      )

    BrickController.reconcile_now(pid)

    assert calls.() == [{"embervm", "embervm-embervm-noded-brick-2gi", 1}]
  end

  test "manual minimum and desired counts are never lowered by a computed floor" do
    {record, calls} = new_recorder()

    pid =
      start(
        classes: [
          %{
            name: "16gi",
            desired: 3,
            min: 2,
            max: 4,
            usable_mib: 16_000,
            mem_reject_floor_mib: 512,
            slots: 8
          }
        ],
        catalog_fun: fn -> [%{name: "warm", floor: 1, mem_mib: 4_000}] end,
        scale_fun: record,
        registered_fun: fn -> %{"16gi" => 3} end
      )

    BrickController.reconcile_now(pid)

    assert calls.() == [{"embervm", "embervm-embervm-noded-brick-16gi", 3}]
  end

  test "floor overflow alerts before scale and clears on catalog recovery" do
    {record, calls} = new_recorder()
    {:ok, catalog} = Agent.start_link(fn -> [%{name: "busy", floor: 3, mem_mib: 800}] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(catalog) end)

    pid =
      start(
        classes: [
          %{
            name: "2gi",
            desired: 0,
            min: 0,
            max: 2,
            usable_mib: 1_000,
            mem_reject_floor_mib: 512,
            slots: 8
          }
        ],
        catalog_fun: fn -> Agent.get(catalog, & &1) end,
        scale_fun: record,
        registered_fun: fn -> %{} end
      )

    first = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)
    second = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert first =~ "embervm brick floor overflow"
    refute second =~ "embervm brick floor overflow"
    assert BrickController.floor_overflow?(pid, "2gi")
    assert calls.() == []

    Agent.update(catalog, fn _ -> [%{name: "busy", floor: 2, mem_mib: 200}] end)
    recovered = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert recovered =~ "embervm brick floor overflow cleared"
    refute BrickController.floor_overflow?(pid, "2gi")
    assert calls.() == [{"embervm", "embervm-embervm-noded-brick-2gi", 1}]
  end

  test "floor overflow suppresses only its class and unaffected state recovers" do
    {record, calls} = new_recorder()
    {clock, advance} = new_clock()

    {:ok, catalog} =
      Agent.start_link(fn ->
        [
          %{name: "small", floor: 2, mem_mib: 400},
          %{name: "large", floor: 1, mem_mib: 1_200}
        ]
      end)

    on_exit(fn -> Embervm.TestProcess.stop_safely(catalog) end)

    pid =
      start(
        classes: [
          %{
            name: "1gi",
            desired: 0,
            min: 0,
            max: 1,
            usable_mib: 1_000,
            mem_reject_floor_mib: 512,
            slots: 8
          },
          %{
            name: "2gi",
            desired: 0,
            min: 0,
            max: 2,
            usable_mib: 2_000,
            mem_reject_floor_mib: 512,
            slots: 8
          }
        ],
        catalog_fun: fn -> Agent.get(catalog, & &1) end,
        scale_fun: record,
        registered_fun: fn -> %{} end,
        fleet_full_after_ms: 100,
        clock: clock
      )

    BrickController.reconcile_now(pid)
    assert BrickController.floor_overflow?(pid, "1gi")
    assert calls.() == [{"embervm", "embervm-embervm-noded-brick-2gi", 1}]

    advance.(200)
    BrickController.reconcile_now(pid)
    assert BrickController.fleet_full?(pid, "2gi")

    Agent.update(catalog, fn _ ->
      [
        %{name: "small", floor: 1, mem_mib: 400},
        %{name: "large", floor: 1, mem_mib: 1_200}
      ]
    end)

    recovered = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert recovered =~ "embervm brick floor overflow cleared"
    refute BrickController.floor_overflow?(pid, "1gi")

    assert calls.() == [
             {"embervm", "embervm-embervm-noded-brick-2gi", 1},
             {"embervm", "embervm-embervm-noded-brick-2gi", 1},
             {"embervm", "embervm-embervm-noded-brick-1gi", 1},
             {"embervm", "embervm-embervm-noded-brick-2gi", 1}
           ]
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
    on_exit(fn -> Embervm.TestProcess.stop_safely(reg) end)

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

  # -- full mode (phase 3: drain-aware scale-down acts) ------------------------

  defp new_annotator do
    {:ok, pid} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)

    annotate = fn ns, pod, annotations ->
      Agent.update(pid, &[{ns, pod, annotations} | &1])
      :ok
    end

    calls = fn -> pid |> Agent.get(& &1) |> Enum.reverse() end
    {annotate, calls}
  end

  defp full_mode_opts(facts, scale_recorder, annotate) do
    [
      mode: :full,
      classes: [%{name: "2gi", desired: 2, min: 0, max: 4}],
      scale_fun: scale_recorder,
      scale_get_fun: fn _ns, _name -> {:ok, 2} end,
      facts_fun: fn -> facts end,
      pods_fun: fn _ns, _selector ->
        {:ok, [%{name: "brick-a", uid: "uid-a"}, %{name: "brick-b", uid: "uid-b"}]}
      end,
      annotate_fun: annotate,
      registered_fun: fn -> %{"2gi" => 2} end,
      down_idle_ms: 100
    ]
  end

  test "full mode scale-down annotates the idle victim then shrinks" do
    {record, calls} = new_recorder()
    {annotate, annotated} = new_annotator()
    {clock, advance} = new_clock()

    facts = [
      %{size_class: "2gi", pod_uid: "uid-a", live_vms: 1, draining: false},
      %{
        size_class: "2gi",
        pod_uid: "uid-b",
        live_vms: 0,
        draining: false,
        stateful_bundles: [%{exported: true}]
      }
    ]

    pid = start(full_mode_opts(facts, record, annotate) ++ [clock: clock])

    BrickController.reconcile_now(pid)
    advance.(200)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "brick autoscale: scaling class 2gi from 2 to 1 (reason=idle_drain)"
    # The idle brick (uid-b), not the busy one, got the negative deletion cost.
    assert annotated.() == [
             {"embervm", "brick-b", %{"controller.kubernetes.io/pod-deletion-cost" => "-1000"}}
           ]

    assert List.last(calls.()) == {"embervm", "embervm-embervm-noded-brick-2gi", 1}
  end

  test "full mode refuses to strand un-exported warmth and skips the shrink" do
    {record, calls} = new_recorder()
    {annotate, annotated} = new_annotator()
    {clock, advance} = new_clock()

    facts = [
      %{size_class: "2gi", pod_uid: "uid-a", live_vms: 1, draining: false},
      # Idle, but its banked bundle has no current store copy: not a safe victim.
      %{
        size_class: "2gi",
        pod_uid: "uid-b",
        live_vms: 0,
        draining: false,
        stateful_bundles: [%{exported: false}]
      }
    ]

    pid = start(full_mode_opts(facts, record, annotate) ++ [clock: clock])

    BrickController.reconcile_now(pid)
    advance.(200)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "brick autoscale: skipping scale-down of class 2gi (reason=no_safe_victim)"
    assert annotated.() == []
    # Both ticks re-asserted the live current (2); the shrink never happened.
    assert calls.() == [
             {"embervm", "embervm-embervm-noded-brick-2gi", 2},
             {"embervm", "embervm-embervm-noded-brick-2gi", 2}
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

  test "usable capacity includes the admission floor" do
    {clock, advance} = new_clock()

    pid =
      start(
        mode: :observe,
        classes: [
          %{name: "2gi", desired: 0, min: 0, max: 2, usable_mib: 1_536},
          %{name: "4gi", desired: 0, min: 0, max: 2, usable_mib: 3_584}
        ],
        scale_fun: fn _ns, _name, _r -> :ok end,
        scale_get_fun: fn _ns, _name -> {:ok, 0} end,
        facts_fun: fn -> [%{size_class: "2gi", mem_reject_floor_mib: 512}] end,
        registered_fun: fn -> %{} end,
        up_threshold: 1,
        clock: clock
      )

    BrickController.note_denial(pid, 1_536)
    advance.(1)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "would scale class 4gi from 0 to 1"
    refute log =~ "would scale class 2gi"
  end

  test "regression: a registered floor of zero falls back to 512 instead of selecting 1536" do
    {clock, advance} = new_clock()

    pid =
      start(
        mode: :observe,
        classes: [
          %{name: "2gi", desired: 0, min: 0, max: 2, usable_mib: 1_536},
          %{name: "4gi", desired: 0, min: 0, max: 2, usable_mib: 3_584}
        ],
        scale_fun: fn _ns, _name, _r -> :ok end,
        scale_get_fun: fn _ns, _name -> {:ok, 0} end,
        facts_fun: fn -> [%{size_class: "2gi", mem_reject_floor_mib: 0}] end,
        registered_fun: fn -> %{} end,
        up_threshold: 1,
        clock: clock
      )

    BrickController.note_denial(pid, 1_536)
    advance.(1)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "would scale class 4gi from 0 to 1"
    refute log =~ "would scale class 2gi"
  end

  test "a registered positive floor overrides the 512 fallback" do
    {clock, advance} = new_clock()

    pid =
      start(
        mode: :observe,
        classes: [
          %{name: "2gi", desired: 0, min: 0, max: 2, usable_mib: 1_536},
          %{name: "4gi", desired: 0, min: 0, max: 2, usable_mib: 3_584}
        ],
        scale_fun: fn _ns, _name, _r -> :ok end,
        scale_get_fun: fn _ns, _name -> {:ok, 0} end,
        facts_fun: fn -> [%{size_class: "2gi", mem_reject_floor_mib: 256}] end,
        registered_fun: fn -> %{} end,
        up_threshold: 1,
        clock: clock
      )

    # 1280 + 256 fits 1536, while 1280 + 512 does not.
    BrickController.note_denial(pid, 1_280)
    advance.(1)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "would scale class 2gi from 0 to 1"
    refute log =~ "would scale class 4gi"
  end

  test "denial attribution uses declared usable capacity instead of the label" do
    {clock, advance} = new_clock()

    pid =
      start(
        mode: :observe,
        classes: [
          %{name: "2gi", desired: 0, min: 0, max: 2, usable_mib: 8_000},
          %{name: "8gi", desired: 0, min: 0, max: 2, usable_mib: 9_000}
        ],
        scale_fun: fn _ns, _name, _r -> :ok end,
        scale_get_fun: fn _ns, _name -> {:ok, 0} end,
        facts_fun: fn -> [] end,
        registered_fun: fn -> %{} end,
        up_threshold: 1,
        clock: clock
      )

    BrickController.note_denial(pid, 4_000)
    advance.(1)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "would scale class 2gi from 0 to 1"
    refute log =~ "would scale class 8gi"
  end

  test "old class config falls back to the nameplate capacity" do
    {clock, advance} = new_clock()

    pid =
      start(
        mode: :observe,
        classes: [%{name: "2gi", desired: 0, min: 0, max: 2}, %{name: "4gi", desired: 0, min: 0, max: 2}],
        scale_fun: fn _ns, _name, _r -> :ok end,
        scale_get_fun: fn _ns, _name -> {:ok, 0} end,
        facts_fun: fn -> [] end,
        registered_fun: fn -> %{} end,
        up_threshold: 1,
        clock: clock
      )

    BrickController.note_denial(pid, 1_536)
    advance.(1)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "would scale class 2gi from 0 to 1"
  end

  test "floor attribution uses the largest registered floor and defaults to 512" do
    {clock, advance} = new_clock()

    opts = [
      mode: :observe,
      classes: [%{name: "2gi", desired: 0, min: 0, max: 2, usable_mib: 2_048}, %{name: "4gi", desired: 0, min: 0, max: 2, usable_mib: 3_584}],
      scale_fun: fn _ns, _name, _r -> :ok end,
      scale_get_fun: fn _ns, _name -> {:ok, 0} end,
      registered_fun: fn -> %{} end,
      up_threshold: 1,
      clock: clock
    ]

    pid = start(opts ++ [facts_fun: fn -> [%{size_class: "2gi", mem_reject_floor_mib: 100}, %{size_class: "2gi", mem_reject_floor_mib: 600}] end])
    BrickController.note_denial(pid, 1_500)
    advance.(1)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)
    assert log =~ "would scale class 4gi from 0 to 1"

    pid = start(opts ++ [facts_fun: fn -> [] end])
    BrickController.note_denial(pid, 1_536)
    advance.(1)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)
    assert log =~ "would scale class 2gi from 0 to 1"
  end

  test "a denial no class can serve is logged and traced" do
    pid = start(classes: [%{name: "2gi", desired: 0, usable_mib: 1_536}], facts_fun: fn -> [] end)

    log =
      ExUnit.CaptureLog.capture_log(fn ->
        BrickController.note_denial(pid, 1_536)
        BrickController.reconcile_now(pid)
      end)

    assert log =~ "embervm brick denial cannot be served"
  end

  test "capacity drift warns only when the mismatch transitions" do
    facts = fn -> [%{size_class: "2gi", mem_budget_mib: 1_400}] end

    pid =
      start(
        classes: [%{name: "2gi", desired: 0, usable_mib: 1_536}],
        facts_fun: facts,
        scale_fun: fn _ns, _name, _r -> :ok end
      )

    first = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)
    second = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert first =~ "embervm brick capacity drift"
    refute second =~ "embervm brick capacity drift"
  end

  test "a bound raises one ceiling step at threshold and replicas grow on the next tick" do
    {clock, advance} = new_clock()
    {:ok, current} = Agent.start_link(fn -> 0 end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(current) end)
    {record, calls} = new_recorder()

    scale = fn ns, name, replicas ->
      :ok = record.(ns, name, replicas)
      Agent.update(current, fn _ -> replicas end)
      :ok
    end

    pid =
      start(
        mode: :up,
        classes: [
          %{
            name: "4gi",
            desired: 0,
            min: 0,
            max: 0,
            ceiling_bound: 2,
            usable_mib: 3_840,
            mem_reject_floor_mib: 512,
            slots: 8
          }
        ],
        scale_fun: scale,
        scale_get_fun: fn _ns, _name -> {:ok, Agent.get(current, & &1)} end,
        facts_fun: fn -> [] end,
        up_threshold: 2,
        clock: clock
      )

    BrickController.note_denial(pid, "semgrep", 1_536)
    BrickController.reconcile_now(pid)
    assert Enum.map(calls.(), &elem(&1, 2)) == [0]

    BrickController.note_denial(pid, "semgrep", 1_536)
    advance.(1)
    BrickController.reconcile_now(pid)

    assert BrickController.capacity_health(pid).ceilings == [
             %{size_class: "4gi", bootstrap_max: 0, operative_ceiling: 1, ceiling_bound: 2}
           ]

    # The threshold tick only raises authorization and reasserts zero.
    assert Enum.map(calls.(), &elem(&1, 2)) == [0, 0]

    BrickController.reconcile_now(pid)
    assert Enum.map(calls.(), &elem(&1, 2)) == [0, 0, 1]
  end

  test "missing and zero bounds preserve fixed ceilings in every mode" do
    for {mode, bound} <- [{:observe, nil}, {:up, 0}, {:full, 0}] do
      {record, calls} = new_recorder()

      class =
        %{name: "2gi", desired: 0, min: 0, max: 0, usable_mib: 1_792}
        |> then(fn class -> if is_nil(bound), do: class, else: Map.put(class, :ceiling_bound, bound) end)

      pid =
        start(
          mode: mode,
          classes: [class],
          scale_fun: record,
          scale_get_fun: fn _ns, _name -> {:ok, 0} end,
          facts_fun: fn -> [] end,
          up_threshold: 1
        )

      BrickController.note_denial(pid, "small", 512)
      BrickController.reconcile_now(pid)

      assert Enum.map(calls.(), &elem(&1, 2)) == [0]
      assert BrickController.capacity_health(pid).ceilings == []
    end
  end

  test "observe reports a bound move without changing the operative ceiling" do
    pid =
      start(
        mode: :observe,
        classes: [
          %{name: "4gi", desired: 0, max: 0, ceiling_bound: 2, usable_mib: 3_840}
        ],
        scale_fun: fn _ns, _name, _replicas -> :ok end,
        scale_get_fun: fn _ns, _name -> {:ok, 0} end,
        facts_fun: fn -> [] end,
        up_threshold: 1
      )

    BrickController.note_denial(pid, "semgrep", 1_536)
    log = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert log =~ "ceiling would raise"
    assert hd(BrickController.capacity_health(pid).ceilings).operative_ceiling == 0

    {record, calls} = new_recorder()

    off =
      start(
        mode: :off,
        classes: [
          %{name: "4gi", desired: 0, max: 0, ceiling_bound: 2, usable_mib: 3_840}
        ],
        scale_fun: record,
        scale_get_fun: fn _ns, _name -> flunk("off mode must not read live scale") end,
        facts_fun: fn -> [] end,
        up_threshold: 1
      )

    BrickController.note_denial(off, "semgrep", 1_536)
    BrickController.reconcile_now(off)
    assert Enum.map(calls.(), &elem(&1, 2)) == [0]
    assert hd(BrickController.capacity_health(off).ceilings).operative_ceiling == 0
  end

  test "live reject floors select 4gi and 8gi while oversized demand only latches health" do
    classes = [
      %{
        name: "4gi",
        desired: 1,
        max: 1,
        ceiling_bound: 1,
        usable_mib: 3_584,
        mem_reject_floor_mib: 256
      },
      %{
        name: "8gi",
        desired: 1,
        max: 1,
        ceiling_bound: 1,
        usable_mib: 7_936,
        mem_reject_floor_mib: 256
      },
      %{
        name: "16gi",
        desired: 0,
        max: 0,
        ceiling_bound: 0,
        usable_mib: 16_128,
        mem_reject_floor_mib: 256
      }
    ]

    {record, calls} = new_recorder()

    pid =
      start(
        mode: :observe,
        classes: classes,
        scale_fun: record,
        scale_get_fun: fn _ns, _name -> {:ok, 1} end,
        facts_fun: fn ->
          [
            %{size_class: "4gi", mem_reject_floor_mib: 2_048},
            %{size_class: "8gi", mem_reject_floor_mib: 512}
          ]
        end,
        up_threshold: 1
      )

    BrickController.note_denial(pid, "semgrep", 1_536)
    BrickController.reconcile_now(pid)

    semgrep =
      Enum.find(BrickController.capacity_health(pid).conditions, &(&1.workload == "semgrep"))

    assert semgrep.size_class == "4gi"

    BrickController.note_denial(pid, "claude-runtime", 4_096)
    BrickController.reconcile_now(pid)

    claude =
      Enum.find(
        BrickController.capacity_health(pid).conditions,
        &(&1.workload == "claude-runtime")
      )

    assert claude.size_class == "8gi"

    before_oversized = calls.()
    BrickController.note_denial(pid, "oversized", 8_000)
    BrickController.reconcile_now(pid)

    condition =
      Enum.find(BrickController.capacity_health(pid).conditions, &(&1.workload == "oversized"))

    assert condition.workload == "oversized"
    assert condition.reason == :no_fitting_class

    assert calls.()
           |> Enum.drop(length(before_oversized))
           |> Enum.map(&elem(&1, 2)) == [1, 1, 0]
  end

  test "ceiling exhaustion latches every workload contributing denial pressure" do
    pid =
      start(
        mode: :up,
        classes: [
          %{name: "8gi", desired: 1, max: 1, ceiling_bound: 1, usable_mib: 7_936}
        ],
        scale_fun: fn _ns, _name, _replicas -> :ok end,
        scale_get_fun: fn _ns, _name -> {:ok, 1} end,
        facts_fun: fn -> [] end,
        registered_fun: fn -> %{"8gi" => 1} end,
        up_threshold: 2
      )

    BrickController.note_denial(pid, "runtime-a", 4_096)
    BrickController.note_denial(pid, "runtime-b", 4_096)
    BrickController.reconcile_now(pid)

    assert BrickController.capacity_health(pid).conditions
           |> Enum.map(&{&1.workload, &1.reason, &1.size_class})
           |> Enum.sort() ==
             [
               {"runtime-a", :ceiling_exhausted, "8gi"},
               {"runtime-b", :ceiling_exhausted, "8gi"}
             ]
  end

  test "ceiling exhaustion is transition-only and clears after the quiet window" do
    {clock, advance} = new_clock()
    {:ok, transitions} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(transitions) end)
    {:ok, read_result} = Agent.start_link(fn -> {:ok, 1} end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(read_result) end)

    condition_fun = fn workload, condition ->
      Agent.update(transitions, &(&1 ++ [{workload, condition}]))
    end

    pid =
      start(
        mode: :up,
        classes: [
          %{name: "8gi", desired: 1, max: 1, ceiling_bound: 1, usable_mib: 7_936}
        ],
        scale_fun: fn _ns, _name, _replicas -> :ok end,
        scale_get_fun: fn _ns, _name -> Agent.get(read_result, & &1) end,
        facts_fun: fn -> [] end,
        registered_fun: fn -> %{"8gi" => 1} end,
        condition_fun: condition_fun,
        up_threshold: 1,
        up_window_ms: 60_000,
        clock: clock
      )

    BrickController.note_denial(pid, "claude-runtime", 4_096)
    first = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)
    second = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert first =~ "workload capacity unavailable"
    refute second =~ "workload capacity unavailable"
    refute BrickController.capacity_health(pid).ok
    assert length(Agent.get(transitions, & &1)) == 1

    # A continuing denial refreshes the latch even when Kubernetes cannot
    # provide a scale read. Recovery retries successfully without a duplicate
    # Workload transition.
    advance.(60_000)
    Agent.update(read_result, fn _ -> {:error, :timeout} end)
    BrickController.note_denial(pid, "claude-runtime", 4_096)
    BrickController.reconcile_now(pid)
    refute BrickController.capacity_health(pid).ok
    assert length(Agent.get(transitions, & &1)) == 1

    Agent.update(read_result, fn _ -> {:ok, 1} end)
    BrickController.reconcile_now(pid)
    assert length(Agent.get(transitions, & &1)) == 1

    advance.(60_000)
    recovered = ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

    assert recovered =~ "workload capacity recovered"
    assert BrickController.capacity_health(pid).ok
    assert [{"claude-runtime", %{status: "False"}}, {"claude-runtime", %{status: "True"}}] =
             Agent.get(transitions, &Enum.map(&1, fn {w, c} -> {w, Map.take(c, [:status])} end))
  end

  test "an unreadable scale is never patched and a failed patch retries without cooldown" do
    {record, calls} = new_recorder()

    unreadable =
      start(
        mode: :full,
        classes: [%{name: "2gi", desired: 0, min: 0, max: 2}],
        scale_fun: record,
        scale_get_fun: fn _ns, _name -> {:error, :timeout} end,
        facts_fun: fn -> [] end
      )

    BrickController.reconcile_now(unreadable)
    assert calls.() == []

    {:ok, attempts} = Agent.start_link(fn -> 0 end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(attempts) end)

    patch = fn _ns, _name, replicas ->
      attempt = Agent.get_and_update(attempts, &{&1, &1 + 1})
      if attempt == 0, do: {:error, :timeout}, else: :ok
    end

    retrying =
      start(
        mode: :up,
        classes: [%{name: "2gi", desired: 1, min: 0, max: 2}],
        scale_fun: patch,
        scale_get_fun: fn _ns, _name -> {:ok, 1} end,
        facts_fun: fn -> [] end,
        up_threshold: 1
      )

    BrickController.note_denial(retrying, "small", 512)
    BrickController.reconcile_now(retrying)
    BrickController.reconcile_now(retrying)
    assert Agent.get(attempts, & &1) == 2
  end

  test "an unreadable scale preserves an existing fleet-full latch" do
    {:ok, read_result} = Agent.start_link(fn -> {:ok, 1} end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(read_result) end)
    {record, calls} = new_recorder()

    pid =
      start(
        mode: :up,
        classes: [%{name: "2gi", desired: 1, min: 0, max: 2}],
        scale_fun: record,
        scale_get_fun: fn _ns, _name -> Agent.get(read_result, & &1) end,
        registered_fun: fn -> %{} end,
        facts_fun: fn -> [] end,
        fleet_full_after_ms: 0
      )

    BrickController.reconcile_now(pid)
    assert BrickController.fleet_full?(pid, "2gi")

    Agent.update(read_result, fn _ -> {:error, :timeout} end)
    BrickController.reconcile_now(pid)

    assert BrickController.fleet_full?(pid, "2gi")
    assert Enum.map(calls.(), &elem(&1, 2)) == [1]
  end

  test "a failed scale-down patch retries without committing cooldown" do
    {clock, advance} = new_clock()
    {:ok, attempts} = Agent.start_link(fn -> %{down: 0, total: 0} end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(attempts) end)

    patch = fn _ns, _name, replicas ->
      down_attempt =
        Agent.get_and_update(attempts, fn state ->
          next = %{
            state
            | total: state.total + 1,
              down: state.down + if(replicas == 0, do: 1, else: 0)
          }

          {state.down, next}
        end)

      if replicas == 0 and down_attempt == 0, do: {:error, :timeout}, else: :ok
    end

    pid =
      start(
        mode: :full,
        classes: [%{name: "2gi", desired: 0, min: 0, max: 2}],
        scale_fun: patch,
        scale_get_fun: fn _ns, _name -> {:ok, 1} end,
        facts_fun: fn ->
          [%{size_class: "2gi", pod_uid: "uid-a", live_vms: 0, draining: false}]
        end,
        pods_fun: fn _ns, _selector -> {:ok, [%{name: "brick-a", uid: "uid-a"}]} end,
        annotate_fun: fn _ns, _pod, _annotations -> :ok end,
        registered_fun: fn -> %{"2gi" => 1} end,
        down_idle_ms: 1,
        down_cooldown_ms: 60_000,
        clock: clock
      )

    BrickController.reconcile_now(pid)
    advance.(1)
    BrickController.reconcile_now(pid)
    BrickController.reconcile_now(pid)

    assert Agent.get(attempts, & &1) == %{down: 2, total: 3}
  end

  test "raised ceilings retire at the exact one-hour zero boundary and reconstruct safely" do
    {clock, advance} = new_clock()
    {:ok, current} = Agent.start_link(fn -> 2 end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(current) end)

    opts = [
      mode: :full,
      classes: [%{name: "4gi", desired: 0, min: 0, max: 1, ceiling_bound: 3}],
      scale_fun: fn _ns, _name, replicas ->
        Agent.update(current, fn _ -> replicas end)
        :ok
      end,
      scale_get_fun: fn _ns, _name -> {:ok, Agent.get(current, & &1)} end,
      facts_fun: fn -> [] end,
      registered_fun: fn -> %{} end,
      ceiling_idle_ms: 3_600_000,
      clock: clock
    ]

    pid = start(opts)
    BrickController.reconcile_now(pid)
    assert hd(BrickController.capacity_health(pid).ceilings).operative_ceiling == 2

    Agent.update(current, fn _ -> 0 end)
    BrickController.reconcile_now(pid)
    advance.(3_599_999)
    BrickController.reconcile_now(pid)
    assert hd(BrickController.capacity_health(pid).ceilings).operative_ceiling == 2

    advance.(1)
    BrickController.reconcile_now(pid)
    assert hd(BrickController.capacity_health(pid).ceilings).operative_ceiling == 1

    # A fresh controller with two live replicas reconstructs two, never the
    # lower bootstrap value and never an assumed zero.
    Agent.update(current, fn _ -> 2 end)
    restarted = start(opts)
    BrickController.reconcile_now(restarted)
    assert hd(BrickController.capacity_health(restarted).ceilings).operative_ceiling == 2
  end

  test "nonzero replica counts and denials reset the ceiling idle boundary" do
    for reset <- [:nonzero, :denial] do
      {clock, advance} = new_clock()
      {:ok, current} = Agent.start_link(fn -> 2 end)
      on_exit(fn -> Embervm.TestProcess.stop_safely(current) end)

      pid =
        start(
          mode: :full,
          classes: [
            %{
              name: "4gi",
              desired: 0,
              min: 0,
              max: 1,
              ceiling_bound: 3,
              usable_mib: 3_840
            }
          ],
          scale_fun: fn _ns, _name, replicas ->
            Agent.update(current, fn _ -> replicas end)
            :ok
          end,
          scale_get_fun: fn _ns, _name -> {:ok, Agent.get(current, & &1)} end,
          facts_fun: fn -> [] end,
          registered_fun: fn -> %{} end,
          up_threshold: 10,
          up_window_ms: 60_000,
          ceiling_idle_ms: 3_600_000,
          clock: clock
        )

      BrickController.reconcile_now(pid)
      Agent.update(current, fn _ -> 0 end)
      BrickController.reconcile_now(pid)
      advance.(1_800_000)

      case reset do
        :nonzero ->
          Agent.update(current, fn _ -> 1 end)
          BrickController.reconcile_now(pid)
          Agent.update(current, fn _ -> 0 end)
          BrickController.reconcile_now(pid)

        :denial ->
          BrickController.note_denial(pid, "semgrep", 1_536)
          BrickController.reconcile_now(pid)
          advance.(60_000)
          BrickController.reconcile_now(pid)
      end

      # The original boundary has passed, but the reset began a fresh hour.
      advance.(1_800_000)
      BrickController.reconcile_now(pid)
      assert hd(BrickController.capacity_health(pid).ceilings).operative_ceiling == 2

      advance.(1_800_000)
      BrickController.reconcile_now(pid)
      assert hd(BrickController.capacity_health(pid).ceilings).operative_ceiling == 1
    end
  end
end

defmodule Embervm.BrickControllerScaleFromZeroTest do
  # Regression for the 2026-09-30 GKE scale-to-zero deadlock. With
  # `bricks.autoscale.mode: full` and minReplicas 0, the controller idle-drained
  # 2gi, 4gi, then 8gi to zero. Every claude-runtime (4096 MiB) create then found
  # an EMPTY brick universe, the scheduler returned :no_bricks without recording
  # demand, and note_denial/2 (the only scale-up signal) was never called again,
  # so nothing scaled 8gi back up until a manual `kubectl scale`.
  #
  # The fix routes :no_bricks through note_empty_demand/2 as UNCONFIRMED demand,
  # which the controller promotes to a denial only when every class that fits
  # the need reads zero live replicas. These tests drive that end to end against
  # an in-memory Deployment /scale, and pin that a blind boot (replicas exist,
  # the registry has not synced) still records nothing.
  use ExUnit.Case, async: true

  alias Embervm.BrickController

  @prefix "embervm-embervm-noded-brick-"
  @need 4_096

  # Production GKE shape: only 8gi (usable 7936) holds need 4096 plus the 512
  # admission floor; 4gi usable 3840 does not.
  @classes [
    %{name: "2gi", desired: 0, min: 0, max: 4, usable_mib: 1_792, mem_reject_floor_mib: 512, slots: 8},
    %{name: "4gi", desired: 0, min: 0, max: 2, usable_mib: 3_840, mem_reject_floor_mib: 512, slots: 8},
    %{name: "8gi", desired: 0, min: 0, max: 2, usable_mib: 7_936, mem_reject_floor_mib: 512, slots: 16}
  ]

  # claude-runtime is cold-created per session: a zero warm floor, so the
  # catalog portfolio derives no effective minimum for any class.
  @catalog [%{name: "claude-runtime", floor: 0, mem_mib: @need}]

  defp new_clock do
    {:ok, pid} = Agent.start_link(fn -> 0 end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)
    {fn -> Agent.get(pid, & &1) end, fn ms -> Agent.update(pid, &(&1 + ms)) end}
  end

  # An in-memory stand-in for the brick Deployments' /scale subresource: the
  # controller's writes are what its next live read returns.
  defp new_deployments(initial) do
    {:ok, pid} = Agent.start_link(fn -> initial end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)

    scale = fn _ns, @prefix <> class, replicas ->
      Agent.update(pid, &Map.put(&1, class, replicas))
      :ok
    end

    get = fn _ns, @prefix <> class -> {:ok, Agent.get(pid, &Map.get(&1, class, 0))} end
    replicas = fn -> Agent.get(pid, & &1) end
    {scale, get, replicas}
  end

  defp new_facts(initial) do
    {:ok, pid} = Agent.start_link(fn -> initial end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)
    {fn -> Agent.get(pid, & &1) end, fn facts -> Agent.update(pid, fn _ -> facts end) end}
  end

  defp idle_brick(class, uid) do
    %{size_class: class, pod_uid: uid, live_vms: 0, draining: false, mem_reject_floor_mib: 512}
  end

  defp start(opts) do
    defaults = [
      name: nil,
      mode: :full,
      classes: @classes,
      namespace: "embervm",
      deployment_prefix: @prefix,
      interval_ms: 3_600_000,
      reconcile_on_start: false,
      registered_fun: fn -> %{} end,
      catalog_fun: fn -> @catalog end,
      pods_fun: fn _ns, _selector ->
        {:ok, [%{name: "brick-2gi", uid: "uid-2gi"}, %{name: "brick-8gi", uid: "uid-8gi"}]}
      end,
      annotate_fun: fn _ns, _pod, _annotations -> :ok end,
      down_idle_ms: 100
    ]

    {:ok, pid} = BrickController.start_link(Keyword.merge(defaults, opts))
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)
    pid
  end

  defp tick(pid), do: ExUnit.CaptureLog.capture_log(fn -> BrickController.reconcile_now(pid) end)

  defp empty_misses(pid, count) do
    for _ <- 1..count, do: BrickController.note_empty_demand(pid, "claude-runtime", @need)
  end

  test "a class drained to zero scales back up on empty-universe demand" do
    {clock, advance} = new_clock()
    {scale, get, replicas} = new_deployments(%{"2gi" => 0, "4gi" => 0, "8gi" => 1})
    {facts, set_facts} = new_facts([idle_brick("8gi", "uid-8gi")])

    pid = start(clock: clock, scale_fun: scale, scale_get_fun: get, facts_fun: facts)

    # Drain: the last 8gi brick sits idle past the dwell and is removed.
    tick(pid)
    advance.(200)
    log = tick(pid)
    assert log =~ "brick autoscale: scaling class 8gi from 1 to 0 (reason=idle_drain)"
    assert replicas.() == %{"2gi" => 0, "4gi" => 0, "8gi" => 0}

    # The brick deregisters: the scheduler's universe is now empty, so every
    # create is a :no_bricks miss. Nothing moves without demand.
    set_facts.([])
    advance.(1_000)
    tick(pid)
    assert replicas.()["8gi"] == 0

    # Demand: three refused creates (the up threshold) inside the window.
    empty_misses(pid, 3)
    advance.(1)
    log = tick(pid)

    assert log =~ "brick autoscale: scaling class 8gi from 0 to 1 (reason=denial_pressure)"
    assert replicas.() == %{"2gi" => 0, "4gi" => 0, "8gi" => 1}
  end

  test "blind boot: empty-universe misses record no demand while a fitting brick exists" do
    {clock, advance} = new_clock()
    # A fresh controller after a CP restart: the 8gi brick is running but has
    # not dialed home, so the registry and capacity facts are still empty.
    {scale, get, replicas} = new_deployments(%{"2gi" => 0, "4gi" => 0, "8gi" => 1})

    pid = start(clock: clock, scale_fun: scale, scale_get_fun: get, facts_fun: fn -> [] end)

    empty_misses(pid, 10)
    advance.(1)
    log = tick(pid)

    assert log =~ "ignoring 10 empty-universe placement miss(es) (reason=replicas_pending)"
    refute log =~ "from 1 to 2"
    assert replicas.()["8gi"] == 1

    # The dropped misses are gone for good, not carried into a later window.
    advance.(1_000)
    log = tick(pid)
    refute log =~ "denial_pressure"
    assert replicas.()["8gi"] == 1
  end

  test "blind boot: a larger fitting class still starting suppresses the smaller one's scale-up" do
    {clock, advance} = new_clock()
    classes = @classes ++ [%{name: "16gi", desired: 0, min: 1, max: 2, usable_mib: 16_000, mem_reject_floor_mib: 512, slots: 16}]
    {scale, get, replicas} = new_deployments(%{"2gi" => 0, "4gi" => 0, "8gi" => 0, "16gi" => 1})

    pid =
      start(classes: classes, clock: clock, scale_fun: scale, scale_get_fun: get, facts_fun: fn -> [] end)

    empty_misses(pid, 3)
    advance.(1)
    log = tick(pid)

    # 16gi can hold the need and is about to register: that is blindness.
    assert log =~ "reason=replicas_pending"
    refute log =~ "scaling class 8gi"
    assert replicas.()["8gi"] == 0
  end

  test "an unreadable replica count never confirms empty-universe demand" do
    {clock, advance} = new_clock()

    pid =
      start(
        clock: clock,
        scale_fun: fn _ns, _name, _replicas -> :ok end,
        scale_get_fun: fn _ns, _name -> {:error, :timeout} end,
        facts_fun: fn -> [] end
      )

    empty_misses(pid, 3)
    advance.(1)
    log = tick(pid)

    assert log =~ "reason=scale_read_failed"
    refute log =~ "denial_pressure"
  end

  test "a class no workload needs still drains to zero and stays there" do
    {clock, advance} = new_clock()
    {scale, get, replicas} = new_deployments(%{"2gi" => 1, "4gi" => 0, "8gi" => 0})
    {facts, set_facts} = new_facts([idle_brick("2gi", "uid-2gi")])

    pid = start(clock: clock, scale_fun: scale, scale_get_fun: get, facts_fun: facts)

    tick(pid)
    advance.(200)
    log = tick(pid)
    assert log =~ "brick autoscale: scaling class 2gi from 1 to 0 (reason=idle_drain)"
    assert replicas.()["2gi"] == 0

    # Demand for a 4096 MiB workload against the now-empty fleet brings back
    # only the class that fits it; the unneeded 2gi class stays at zero.
    set_facts.([])
    empty_misses(pid, 3)
    advance.(1)
    log = tick(pid)

    assert log =~ "scaling class 8gi from 0 to 1 (reason=denial_pressure)"
    refute log =~ "scaling class 2gi from 0"
    assert replicas.() == %{"2gi" => 0, "4gi" => 0, "8gi" => 1}
  end

  test "mode off discards empty-universe misses" do
    {clock, advance} = new_clock()
    {scale, get, replicas} = new_deployments(%{"2gi" => 0, "4gi" => 0, "8gi" => 0})

    pid =
      start(mode: :off, clock: clock, scale_fun: scale, scale_get_fun: get, facts_fun: fn -> [] end)

    empty_misses(pid, 3)
    advance.(1)
    tick(pid)

    assert replicas.() == %{"2gi" => 0, "4gi" => 0, "8gi" => 0}
  end
end

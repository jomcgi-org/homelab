Code.require_file("support/multi_brick_sim.exs", __DIR__)

defmodule Embervm.MultiBrickSchedulingSimTest do
  @moduledoc """
  Deterministic multi-brick scheduling simulation for the control plane.

  The real SessionManager, Dispatcher, Scheduler, Inflight, Brick, Retry and
  BrickController run against a fleet of fake bricks that admit through a port
  of noded's predicate (see `test/embervm/support/multi_brick_sim.exs`). Every
  step settles before the next runs and every invariant is checked after every
  step, so a failing seed prints the step trace that reproduces it.

  Knobs (environment): `EMBERVM_SIM_SEEDS=3,17` replays named seeds,
  `EMBERVM_SIM_SEED_COUNT=1000` widens the sweep, `EMBERVM_SIM_VERBOSE=1`
  prints every run's step log and counters.
  """
  # async: false: the run sets EMBERVM_PLACEMENT_RETRY, which both managers'
  # workers read at call time, and traces the session manager's sends.
  use ExUnit.Case, async: false

  alias Embervm.MultiBrickSim, as: Sim
  alias Embervm.MultiBrickSim.Fleet

  @incident_bricks [{"4gi", %{}}, {"4gi", %{}}, {"8gi", %{}}]
  # The hub's classes on 2026-10-10: two 4gi bricks at their max, one 8gi of three.
  @incident_classes %{"4gi" => 2, "8gi" => 3}

  @default_seed_count 200
  @steps_per_seed 40

  defp land_all(sim) do
    case Fleet.snapshot(sim.fleet).tickets |> Map.keys() |> Enum.sort() do
      [] -> sim
      [id | _] -> sim |> Sim.step({:land, id}) |> land_all()
    end
  end

  defp vms_by_class(sim) do
    for {_dial, b} <- Fleet.snapshot(sim.fleet).bricks, b.status == :up do
      {b.class, map_size(b.vms)}
    end
    |> Enum.sort()
  end

  defp burst(n), do: List.duplicate({:create, "claude-runtime"}, n)

  describe "2026-10-10 replay" do
    test "a 20-create burst on one stale report places 1 per 4gi and 2 per 8gi and spills demand to 8gi" do
      Sim.with_sim([seed: 20_261_010, bricks: @incident_bricks, class_max: @incident_classes], fn sim ->
        # Ten creates, all in flight at once, against the one report each brick
        # made when it registered. Inflight claims are all that spreads them.
        sim = Sim.run(sim, burst(10))
        assert map_size(Fleet.snapshot(sim.fleet).tickets) == 4
        assert sim.stats.no_capacity == 6

        # The four boots land, the settle holds lapse, and still no brick reports:
        # the next ten creates are placed on facts that say every brick is empty.
        # noded refuses each one on every brick it is walked to.
        sim = land_all(sim)
        sim = Sim.step(sim, {:advance, 6_000})
        sim = Sim.run(sim, burst(10))

        assert vms_by_class(sim) == [{"4gi", 1}, {"4gi", 1}, {"8gi", 2}]
        assert sim.stats.placed == 4
        assert sim.stats.no_capacity == 16
        assert sim.stats.refusals == 30

        # Each of the 16 denials (the router's 429) recorded one demand note,
        # every one attributed to 8gi because 4gi is at its max of 2. One tick
        # scales 8gi and the new brick takes the next create.
        assert length(Map.get(:sys.get_state(sim.ctx.bc).denials, "8gi", [])) == 16
        assert Map.get(:sys.get_state(sim.ctx.bc).denials, "4gi", []) == []
        sim = Sim.step(sim, :tick)
        assert sim.registrations == ["8gi"]
        sim = Sim.step(sim, {:register, "8gi"})
        sim = Sim.step(sim, :report_all)
        sim = Sim.step(sim, {:create, "claude-runtime"})
        sim = land_all(sim)

        assert vms_by_class(sim) == [{"4gi", 1}, {"4gi", 1}, {"8gi", 1}, {"8gi", 2}]
        sim = Sim.quiesce(sim)
        assert sim.violations == []
        sim
      end)
    end

    test "with the frontier walk off the same burst still places 4 and records demand once per denial" do
      Sim.with_sim([seed: 20_261_011, bricks: @incident_bricks, class_max: @incident_classes, retry: false], fn sim ->
        sim = Sim.run(sim, burst(10))
        sim = land_all(sim)
        sim = Sim.step(sim, {:advance, 6_000})
        sim = Sim.run(sim, burst(10))

        assert vms_by_class(sim) == [{"4gi", 1}, {"4gi", 1}, {"8gi", 2}]
        assert sim.stats.placed == 4
        assert sim.stats.no_capacity == 16
        # One attempt per stale-placed create: the refusal is the denial.
        assert sim.stats.refusals == 10
        sim = Sim.quiesce(sim)
        assert sim.violations == []
        sim
      end)
    end
  end

  describe "defense in depth" do
    test "either guard alone keeps the incident burst within every brick's budget" do
      for {guard, opts} <- [
            # The CP's claims spread the burst; noded checks without its ticket.
            cp_debit_only: [brick_defaults: %{ledger: false}],
            # Every create herds onto one head brick; noded's ticket refuses the
            # excess and the frontier walk finds the rest.
            noded_ticket_only: [debit: false]
          ] do
        Sim.with_sim([seed: 6_906, bricks: @incident_bricks, class_max: @incident_classes] ++ opts, fn sim ->
          sim = sim |> Sim.run(burst(10)) |> land_all() |> Sim.step({:advance, 6_000}) |> Sim.run(burst(10))

          assert vms_by_class(sim) == [{"4gi", 1}, {"4gi", 1}, {"8gi", 2}], "#{guard}: #{Sim.summary(sim)}"
          assert sim.stats.no_capacity == 16, "#{guard}: #{Sim.summary(sim)}"
          sim
        end)
      end
    end
  end

  describe "double counting under prompt reports" do
    # A brick that reports its ledger already subtracts its own in-flight
    # tickets from mem_admittable_mib, so the control plane must not charge its
    # claim for the same boot again (Inflight.debit_mib/3). Before that fix only
    # 3 of these 10 creates placed, and 7 were spurious :no_capacity denials
    # that also reached the autoscaler as false demand.
    test "a burst with a report after every admission fills each brick to what noded admits" do
      Sim.with_sim([seed: 20_261_012, bricks: @incident_bricks, class_max: @incident_classes], fn sim ->
        sim =
          Enum.reduce(1..10, sim, fn _, s -> s |> Sim.step({:create, "claude-runtime"}) |> Sim.step(:report_all) end)

        assert map_size(Fleet.snapshot(sim.fleet).tickets) == 4, Sim.summary(sim)
        assert sim.stats.spurious_no_capacity == 0, Sim.summary(sim)
        sim
      end)
    end
  end

  describe "randomized interleavings" do
    test "every invariant holds across seeded mixes of creates, tasks, reports, drains and scale-ups" do
      totals =
        Enum.reduce(seeds(), %{digest: 0}, fn seed, acc ->
          sim =
            Sim.with_sim(random_config(seed), fn sim ->
              sim = Enum.reduce(1..@steps_per_seed, sim, fn _, s -> Sim.random_step(s) end)
              sim = Sim.quiesce(sim)
              assert sim.violations == []
              sim
            end)

          # A digest of every step log, so two runs can be compared for replay.
          acc = %{acc | digest: :erlang.phash2({acc.digest, sim.log})}
          Map.merge(acc, sim.stats, fn _k, a, b -> a + b end)
        end)

      # The sweep is not vacuous: across the default seeds every operation ran,
      # noded refused and the control plane rerouted, denials reached the
      # autoscaler and it scaled.
      if System.get_env("EMBERVM_SIM_SEEDS") == nil do
        for kind <-
              [:create, :land, :report, :report_all, :advance, :bank, :destroy] ++
                [:task_submit, :task_complete, :task_retry, :tick, :register, :drain, :vanish] do
          assert Map.get(totals, {:op, kind}, 0) > 0, "no #{kind} step ran: #{inspect(totals)}"
        end

        for key <- [:placed, :no_capacity, :refusals, :rerouted, :scale_ups, :tasks_interrupted] do
          assert Map.get(totals, key, 0) > 0, "no #{key} across the sweep: #{inspect(totals)}"
        end
      end

      if System.get_env("EMBERVM_SIM_VERBOSE"), do: IO.puts("sweep totals: #{inspect(totals)}")
    end
  end

  describe "negative controls: the harness has teeth" do
    test "without the CP's pending debit and noded's in-flight ticket the burst overcommits a brick" do
      violations =
        Sim.with_sim(
          [
            seed: 6_905,
            bricks: @incident_bricks,
            class_max: @incident_classes,
            debit: false,
            brick_defaults: %{ledger: false},
            on_violation: :collect
          ],
          fn sim -> sim |> Sim.run(burst(20)) |> Map.fetch!(:violations) end
        )

      assert [{step, {:no_overcommit, dial, %{committed: committed, budget: budget}}} | _] =
               Enum.filter(violations, &match?({_, {:no_overcommit, _, _}}, &1))

      # The herd lands on one brick on the second create of the burst.
      assert step == 2
      assert dial =~ "pod-"
      assert committed > budget
    end

    test "the observed admission model the deploy values still select re-finds the 2026-10-10 OOM" do
      violations =
        Sim.with_sim(
          [
            seed: 4_185,
            bricks: [{"8gi", %{}}],
            class_max: %{"8gi" => 3},
            brick_defaults: %{model: :observed},
            on_violation: :collect
          ],
          fn sim ->
            # Each guest lands with a few MiB resident and the brick reports; the
            # settle hold lapses before anything faults in. Then every guest
            # touches its RAM.
            sim =
              Enum.reduce(1..4, sim, fn _, s ->
                s
                |> Sim.step({:create, "claude-runtime"})
                |> land_all()
                |> Sim.step(:report_all)
                |> Sim.step({:advance, 6_000, {0, 0}})
              end)

            sim |> Sim.step({:advance, 1_000, {100, 100}}) |> Map.fetch!(:violations)
          end
        )

      kinds = violations |> Enum.map(fn {_step, v} -> elem(v, 0) end) |> Enum.uniq()
      assert :no_overcommit in kinds
      assert :resident_within_budget in kinds
    end
  end

  # -- seeds -------------------------------------------------------------------

  defp seeds do
    case System.get_env("EMBERVM_SIM_SEEDS") do
      nil ->
        1..String.to_integer(System.get_env("EMBERVM_SIM_SEED_COUNT", "#{@default_seed_count}"))

      list ->
        list |> String.split(",", trim: true) |> Enum.map(&(&1 |> String.trim() |> String.to_integer()))
    end
  end

  # The fleet a seed starts with: zero to two 2gi and 4gi bricks and up to one
  # 8gi (at least two bricks), some daemons that predate the admission ledger
  # (the CP's fallback predicate), the frontier walk on for three in four, and
  # reports anywhere from rare (stale facts) to frequent.
  defp random_config(seed) do
    rng = :rand.seed_s(:exsss, {seed, 3, 5})
    {two, rng} = :rand.uniform_s(3, rng)
    {four, rng} = :rand.uniform_s(3, rng)
    {eight, rng} = :rand.uniform_s(2, rng)
    {walk, rng} = :rand.uniform_s(4, rng)
    {reports, rng} = :rand.uniform_s(3, rng)

    classes =
      List.duplicate("2gi", two - 1) ++ List.duplicate("4gi", four - 1) ++ List.duplicate("8gi", eight - 1)

    classes = if length(classes) < 2, do: classes ++ ["4gi"], else: classes

    {bricks, _rng} =
      Enum.map_reduce(classes, rng, fn class, r ->
        {roll, r} = :rand.uniform_s(100, r)
        {{class, %{reports_ledger: roll > 15}}, r}
      end)

    [
      seed: seed,
      bricks: bricks,
      class_max: %{"2gi" => 3, "4gi" => 3, "8gi" => 2},
      retry: walk > 1,
      report_weight: Enum.at([3, 12, 30], reports - 1)
    ]
  end
end

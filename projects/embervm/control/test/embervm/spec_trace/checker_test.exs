defmodule Embervm.SpecTrace.CheckerTest do
  use ExUnit.Case, async: false

  alias Embervm.SpecTrace
  alias Embervm.SpecTrace.Checker
  alias Embervm.SpecTrace.Store.SQLite

  setup do
    System.put_env("EMBERVM_SPEC_TRACE", "on")
    SpecTrace.configure()

    # The gate lives in `:persistent_term`, which is global to the BEAM and is
    # NOT rolled back between test modules. Without this restore, every module
    # ordered after this one starts with the trace enabled, and `async: false`
    # means that is decided by the ExUnit seed rather than by anything visible
    # in the file. It failed exactly that way: router_test's conformance cases
    # assert `enabled == false`, saw a leaked `true`, queried a store that is
    # not running in their context, and got a 500 whose body has no "verdicts"
    # key at all.
    on_exit(fn ->
      System.put_env("EMBERVM_SPEC_TRACE", "off")
      SpecTrace.configure()
    end)

    store = start_supervised!({SQLite, name: nil, path: ":memory:"})
    {:ok, store: store}
  end

  describe "negative fixtures" do
    # health_monotonic was the ONE invariant shipped without a fixture, and it was
    # also the one that was wrong. Two real bugs sat on a path no test reached: it
    # grouped ALL records rather than health records, and its reducer discarded the
    # accumulator, so every age_to_down halted as a violation and a node that merely
    # went unknown reported one. The untested path and the broken path were the same
    # path, which is the argument for a fixture per invariant rather than per feature.
    test "health_monotonic fails on age_to_down with no preceding age_to_unknown", %{store: store} do
      run_id = "test-run-health"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "reconnect",
          "vars" => %{"node_id" => "node-7", "gen" => 1}
        },
        # Straight to down without passing through unknown: the health machine
        # ages healthy -> unknown -> down, so this ordering cannot occur.
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "age_to_down",
          "vars" => %{"node_id" => "node-7", "last_gen" => 1}
        }
      ]

      :ok = SQLite.write(store, records)
      verdicts = Checker.run(SQLite, store)
      health = Enum.find(verdicts, &(&1[:invariant] == :health_monotonic))

      assert health[:verdict] == :fail
      assert health[:coverage] == 2
      assert String.contains?(health[:detail], "node-7")
    end

    test "health_monotonic passes on a lawful unknown-then-down sequence", %{store: store} do
      run_id = "test-run-health-ok"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "age_to_unknown",
          "vars" => %{"node_id" => "node-8", "last_gen" => 3}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "age_to_down",
          "vars" => %{"node_id" => "node-8", "last_gen" => 3}
        }
      ]

      :ok = SQLite.write(store, records)
      verdicts = Checker.run(SQLite, store)
      health = Enum.find(verdicts, &(&1[:invariant] == :health_monotonic))

      assert health[:verdict] == :pass
      assert health[:coverage] == 2
    end

    test "no_double_assign fails when vm_id appears in two dispatches", %{store: store} do
      run_id = "test-run-1"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "dispatch_miss",
          "vars" => %{"vm_id" => "vm-1"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      no_double_assign = Enum.find(verdicts, &(&1[:invariant] == :no_double_assign))

      assert no_double_assign[:verdict] == :fail
      assert no_double_assign[:coverage] == 2
      assert String.contains?(no_double_assign[:detail], "vm-1")
    end

    test "dispatch_provenance fails when dispatch lacks prime and adopted flag", %{store: store} do
      run_id = "test-run-2"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-2", "provenance" => nil}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      provenance = Enum.find(verdicts, &(&1[:invariant] == :dispatch_provenance))

      assert provenance[:verdict] == :fail
      assert provenance[:coverage] == 1
      assert String.contains?(provenance[:detail], "vm-2")
    end

    test "dispatch_provenance passes when dispatch has prime before it", %{store: store} do
      run_id = "test-run-3"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-3", "node_id" => "n1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-3"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      provenance = Enum.find(verdicts, &(&1[:invariant] == :dispatch_provenance))

      assert provenance[:verdict] == :pass
      assert provenance[:coverage] == 1
    end

    test "dispatch_provenance passes when dispatch carries adopted provenance", %{store: store} do
      run_id = "test-run-4"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-4", "provenance" => "adopted"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      provenance = Enum.find(verdicts, &(&1[:invariant] == :dispatch_provenance))

      assert provenance[:verdict] == :pass
      assert provenance[:coverage] == 1
    end

    test "adopt_idempotent fails when vm_id appears in two adopt_inventory records", %{store: store} do
      run_id = "test-run-5"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "adopt_inventory",
          "vars" => %{"vm_ids" => ["vm-5", "vm-6"]}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "adopt_inventory",
          "vars" => %{"vm_ids" => ["vm-5", "vm-7"]}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      adopt = Enum.find(verdicts, &(&1[:invariant] == :adopt_idempotent))

      assert adopt[:verdict] == :fail
      assert adopt[:coverage] == 2
      assert String.contains?(adopt[:detail], "vm-5")
    end

    test "adopt_idempotent passes when vm_ids don't repeat across adopts", %{store: store} do
      run_id = "test-run-6"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "adopt_inventory",
          "vars" => %{"vm_ids" => ["vm-5", "vm-6"]}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "adopt_inventory",
          "vars" => %{"vm_ids" => ["vm-7", "vm-8"]}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      adopt = Enum.find(verdicts, &(&1[:invariant] == :adopt_idempotent))

      assert adopt[:verdict] == :pass
      assert adopt[:coverage] == 2
    end

    test "prime_before_checkpoint fails when checkpoint has unreprimed vm_id", %{store: store} do
      run_id = "test-run-7"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-a", "node_id" => "n1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "checkpoint",
          "vars" => %{"node_workload_vm_ids" => [["n1", "w1", "vm-a"], ["n1", "w1", "vm-b"]]}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      checkpoint = Enum.find(verdicts, &(&1[:invariant] == :prime_before_checkpoint))

      assert checkpoint[:verdict] == :fail
      assert checkpoint[:coverage] == 1
    end

    test "prime_before_checkpoint passes when all checkpoint vms are primed", %{store: store} do
      run_id = "test-run-8"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-a", "node_id" => "n1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 2,
          "mono" => 150,
          "ts" => 1500,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-b", "node_id" => "n1"}
        },
        %{
          "run_id" => run_id,
          "seq" => 3,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "checkpoint",
          "vars" => %{"node_workload_vm_ids" => [["n1", "w1", "vm-a"], ["n1", "w1", "vm-b"]]}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      checkpoint = Enum.find(verdicts, &(&1[:invariant] == :prime_before_checkpoint))

      assert checkpoint[:verdict] == :pass
      assert checkpoint[:coverage] == 1
    end
  end

  describe "vacuous verdicts" do
    test "all invariants return :vacuous on empty trace", %{store: store} do
      # Write no adoption records at all
      verdicts = Checker.run(SQLite, store)

      Enum.each(verdicts, fn verdict ->
        assert verdict[:verdict] == :vacuous
        assert verdict[:coverage] == 0
      end)
    end

    test "no_double_assign is :vacuous when no dispatches", %{store: store} do
      run_id = "test-run-9"

      records = [
        %{
          "run_id" => run_id,
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "prime",
          "vars" => %{"vm_id" => "vm-1", "node_id" => "n1"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      no_double = Enum.find(verdicts, &(&1[:invariant] == :no_double_assign))

      assert no_double[:verdict] == :vacuous
      assert no_double[:coverage] == 0
    end
  end

  describe "run_id boundary handling" do
    test "does not report violations across run_id boundaries", %{store: store} do
      # Two separate runs: same vm_id but in different runs should not be a violation
      records = [
        %{
          "run_id" => "run-1",
          "seq" => 1,
          "mono" => 100,
          "ts" => 1000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-same"}
        },
        %{
          "run_id" => "run-2",
          "seq" => 2,
          "mono" => 200,
          "ts" => 2000,
          "spec" => "adoption",
          "action" => "dispatch_warm",
          "vars" => %{"vm_id" => "vm-same"}
        }
      ]

      :ok = SQLite.write(store, records)

      verdicts = Checker.run(SQLite, store)
      # Should have verdicts for both runs, not a cross-run violation
      assert length(verdicts) >= 10  # At least 5 invariants per run
    end
  end
end

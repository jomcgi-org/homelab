defmodule Embervm.SpecTrace.ReachabilityTest do
  use ExUnit.Case, async: false

  alias Embervm.SpecTrace
  alias Embervm.SpecTrace.Checker
  alias Embervm.SpecTrace.Store.SQLite

  @registry %{
    no_double_assign: %{
      pass: [{"adoption", "dispatch_warm", %{"vm_id" => "vm-pass"}}],
      fail: [
        {"adoption", "dispatch_warm", %{"vm_id" => "vm-fail"}},
        {"adoption", "dispatch_miss", %{"vm_id" => "vm-fail"}}
      ],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    dispatch_provenance: %{
      pass: [
        {"adoption", "prime", %{"vm_id" => "vm-pass"}},
        {"adoption", "dispatch_warm", %{"vm_id" => "vm-pass"}}
      ],
      fail: [{"adoption", "dispatch_warm", %{"vm_id" => "vm-fail"}}],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    adopt_idempotent: %{
      pass: [
        {"adoption", "adopt_inventory", %{"vm_ids" => ["vm-one"]}},
        {"adoption", "adopt_inventory", %{"vm_ids" => ["vm-two"]}}
      ],
      fail: [
        {"adoption", "adopt_inventory", %{"vm_ids" => ["vm-repeat"]}},
        {"adoption", "adopt_inventory", %{"vm_ids" => ["vm-repeat"]}}
      ],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    health_monotonic: %{
      pass: [
        {"adoption", "age_to_unknown", %{"node_id" => "node-pass"}},
        {"adoption", "age_to_down", %{"node_id" => "node-pass"}}
      ],
      fail: [{"adoption", "age_to_down", %{"node_id" => "node-fail"}}],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    prime_before_checkpoint: %{
      pass: [
        {"adoption", "prime", %{"vm_id" => "vm-pass"}},
        {"adoption", "checkpoint", %{"node_workload_vm_ids" => %{"node:workload" => ["vm-pass"]}}}
      ],
      fail: [
        {"adoption", "checkpoint", %{"node_workload_vm_ids" => %{"node:workload" => ["vm-fail"]}}}
      ],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    destroy_intent_precedes_record: %{
      pass: [
        {"adoption", "begin_destroy", %{"session_id" => "session-pass"}},
        {"adoption", "confirm_destroy", %{"session_id" => "session-pass", "had_vm" => true}}
      ],
      fail: [{"adoption", "confirm_destroy", %{"session_id" => "session-fail", "had_vm" => true}}],
      vacuous: [{"adoption", "prime", %{"vm_id" => "vm-prime"}}]
    },
    no_destroy_before_confirm: %{
      pass: [
        {"adoption", "confirm_destroy", %{"gate" => true, "node_confirmed" => true, "had_vm" => true}}
      ],
      fail: [
        {"adoption", "confirm_destroy", %{"gate" => true, "node_confirmed" => false, "had_vm" => true}}
      ],
      vacuous: [
        {"adoption", "confirm_destroy", %{"gate" => false, "node_confirmed" => false, "had_vm" => true}}
      ]
    }
  }

  setup do
    System.put_env("EMBERVM_SPEC_TRACE", "on")
    SpecTrace.configure()

    on_exit(fn ->
      System.put_env("EMBERVM_SPEC_TRACE", "off")
      SpecTrace.configure()
      :persistent_term.erase({SpecTrace, :run_id})
      :persistent_term.erase({SpecTrace, :dropped})
    end)

    store = start_supervised!({SQLite, name: nil, path: ":memory:"})
    writer = start_supervised!({SpecTrace.Writer, store_mod: SQLite, store: store})
    {:ok, store: store, writer: writer}
  end

  test "registry covers every checker invariant" do
    assert MapSet.new(Map.keys(@registry)) == MapSet.new(Checker.invariants())
  end

  for {invariant, verdicts} <- @registry,
      {expected_verdict, scenarios} <- verdicts do
    test "#{invariant} reaches #{expected_verdict}", %{store: store, writer: writer} do
      scenarios = unquote(Macro.escape(scenarios))

      Enum.each(scenarios, fn {spec, action, vars} ->
        assert :ok = SpecTrace.emit(spec, action, vars)
      end)

      assert :ok = SpecTrace.drain(writer)

      verdict =
        Checker.run(SQLite, store)
        |> Enum.find(&(&1.invariant == unquote(invariant)))

      assert verdict.verdict == unquote(expected_verdict)
    end
  end
end

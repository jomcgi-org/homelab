defmodule Embervm.BrickControllerDecisionSpanTest do
  @moduledoc """
  The `embervm.brick.decision` span: one per class per reconcile tick, so every
  "decided not to act" branch of the brick autoscaler is queryable. Before it,
  only replica changes were logged, and a class pinned at max, waiting on a
  fleet-full flag, or sitting under the denial threshold was invisible.

  `async: false` because `Embervm.TestSpanExporter` swaps the global batch
  processor's exporter. Class names are unique per test so a span from another
  test can never satisfy an assertion here.
  """
  use ExUnit.Case, async: false

  alias Embervm.{BrickController, TestSpanExporter}

  @span "embervm.brick.decision"

  defp start(opts) do
    defaults = [
      name: nil,
      namespace: "embervm",
      deployment_prefix: "embervm-embervm-noded-brick-",
      interval_ms: 3_600_000,
      fleet_full_after_ms: 300_000,
      reconcile_on_start: false,
      registered_fun: fn -> %{} end,
      catalog_fun: fn -> [] end,
      facts_fun: fn -> [] end,
      scale_fun: fn _ns, _name, _replicas -> :ok end
    ]

    {:ok, pid} = BrickController.start_link(Keyword.merge(defaults, opts))
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)
    pid
  end

  # A class whose declared usable capacity attributes a 512 MiB denial to it
  # (512 need + the 512 default admission floor fits 1792).
  defp class(name, overrides) do
    Map.merge(%{name: name, desired: 1, min: 0, max: 4, usable_mib: 1_792}, overrides)
  end

  defp decision_span(fun, class_name) do
    {_result, spans} = TestSpanExporter.capture(fun, [@span])

    spans
    |> TestSpanExporter.named(@span)
    |> Enum.map(&TestSpanExporter.attributes/1)
    |> Enum.find(&(&1["ember.size_class"] == class_name)) ||
      flunk("no #{@span} span for class #{class_name}")
  end

  test "at max under denial pressure: a span records the refusal to scale" do
    pid =
      start(
        mode: :up,
        classes: [class("span-at-max", %{max: 2})],
        scale_get_fun: fn _ns, _name -> {:ok, 2} end,
        registered_fun: fn -> %{"span-at-max" => 2} end,
        up_threshold: 1
      )

    BrickController.note_denial(pid, "wl", 512)

    attrs = decision_span(fn -> BrickController.reconcile_now(pid) end, "span-at-max")

    assert attrs["ember.reason"] == "at_max"
    assert attrs["ember.brick.mode"] == "up"
    assert attrs["ember.brick.current"] == 2
    assert attrs["ember.brick.target"] == 2
    assert attrs["ember.brick.max"] == 2
    assert attrs["ember.brick.denials_in_window"] == 1
    assert attrs["ember.brick.up_threshold"] == 1
    assert attrs["ember.brick.pressure"] == true
    assert attrs["ember.brick.acted"] == false
    assert attrs["ember.brick.outcome"] == "written"
    assert attrs["ember.brick.fleet_full_transition"] == "none"
  end

  test "below-threshold pressure: denials are visible even though nothing moves" do
    pid =
      start(
        mode: :up,
        classes: [class("span-below", %{})],
        scale_get_fun: fn _ns, _name -> {:ok, 1} end,
        registered_fun: fn -> %{"span-below" => 1} end,
        up_threshold: 3
      )

    BrickController.note_denial(pid, "wl", 512)

    attrs = decision_span(fn -> BrickController.reconcile_now(pid) end, "span-below")

    assert attrs["ember.reason"] == "steady"
    assert attrs["ember.brick.denials_in_window"] == 1
    assert attrs["ember.brick.up_threshold"] == 3
    assert attrs["ember.brick.pressure"] == false
    assert attrs["ember.brick.current"] == 1
    assert attrs["ember.brick.target"] == 1
    assert attrs["ember.brick.acted"] == false
  end

  test "denial pressure that scales records the action" do
    pid =
      start(
        mode: :up,
        classes: [class("span-up", %{})],
        scale_get_fun: fn _ns, _name -> {:ok, 1} end,
        registered_fun: fn -> %{"span-up" => 2} end,
        up_threshold: 1
      )

    BrickController.note_denial(pid, "wl", 512)

    attrs = decision_span(fn -> BrickController.reconcile_now(pid) end, "span-up")

    assert attrs["ember.reason"] == "denial_pressure"
    assert attrs["ember.brick.current"] == 1
    assert attrs["ember.brick.target"] == 2
    assert attrs["ember.brick.written"] == 2
    assert attrs["ember.brick.acted"] == true
    assert attrs["ember.brick.outcome"] == "written"
  end

  test "an unreadable scale emits a read_failed span with no write" do
    pid =
      start(
        mode: :up,
        classes: [class("span-unreadable", %{})],
        scale_get_fun: fn _ns, _name -> {:error, :timeout} end
      )

    attrs = decision_span(fn -> BrickController.reconcile_now(pid) end, "span-unreadable")

    assert attrs["ember.reason"] == "read_failed"
    assert attrs["ember.brick.outcome"] == "read_failed"
    assert attrs["ember.brick.acted"] == false
    refute Map.has_key?(attrs, "ember.brick.current")
    refute Map.has_key?(attrs, "ember.brick.written")
  end

  test "the fleet-full flag transition is on the decision span" do
    pid =
      start(
        mode: :up,
        classes: [class("span-full", %{})],
        scale_get_fun: fn _ns, _name -> {:ok, 1} end,
        registered_fun: fn -> %{} end,
        fleet_full_after_ms: 0
      )

    attrs = decision_span(fn -> BrickController.reconcile_now(pid) end, "span-full")

    assert attrs["ember.brick.fleet_full"] == true
    assert attrs["ember.brick.fleet_full_transition"] == "flagged"
  end

  # -- the pure attribute builder ---------------------------------------------

  test "decision_attributes/1 drops nils and stringifies reason atoms, keeping booleans" do
    attrs =
      BrickController.decision_attributes(%{
        size_class: "2gi",
        mode: :off,
        current: nil,
        target: 1,
        written: 1,
        min: 0,
        max: 4,
        denials_in_window: nil,
        up_threshold: 3,
        reason: :static,
        skip_reason: nil,
        outcome: :written,
        acted?: false,
        fleet_full: false,
        fleet_full_transition: :none
      })

    assert attrs == %{
             "ember.size_class" => "2gi",
             "ember.brick.mode" => "off",
             "ember.brick.target" => 1,
             "ember.brick.written" => 1,
             "ember.brick.min" => 0,
             "ember.brick.max" => 4,
             "ember.brick.up_threshold" => 3,
             "ember.reason" => "static",
             "ember.brick.outcome" => "written",
             "ember.brick.acted" => false,
             "ember.brick.fleet_full" => false,
             "ember.brick.fleet_full_transition" => "none"
           }
  end

  test "decision_attributes/1 derives pressure and carries a skipped scale-down" do
    attrs =
      BrickController.decision_attributes(%{
        size_class: "2gi",
        mode: :full,
        current: 2,
        target: 1,
        denials_in_window: 0,
        up_threshold: 3,
        reason: :idle_drain,
        skip_reason: :no_safe_victim,
        outcome: :written,
        acted?: false
      })

    assert attrs["ember.brick.pressure"] == false
    assert attrs["ember.reason"] == "idle_drain"
    assert attrs["ember.brick.skip_reason"] == "no_safe_victim"
    assert attrs["ember.brick.acted"] == false
  end
end

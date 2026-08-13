defmodule Embervm.SpecTraceSitesTest do
  @moduledoc """
  Layer-1 guard for SpecTrace emission sites (#4770), mirroring the
  `op_kind_sites` test in `spec_vocabulary_test.exs`.

  The property is that every spec action the manifest CLAIMS is emitted has a
  real emission site in the module named. Three earlier versions of the
  equivalent op-log guard were wrong in instructive ways, and this test is
  written to avoid all three:

    * A bare-atom substring match is too LOOSE: it matches comments, metrics
      keys and any unrelated atom. This matches an emission CONTEXT,
      `SpecTrace.emit(:spec, :action`, not the atom alone.
    * Declaring the transport module (`spec_trace.ex`) would make the guard
      UNFALSIFIABLE, since every action passes through it. The first run of
      this test caught exactly that, which is why the manifest now names
      `primed_op.ex` and `dispatcher.ex`.
    * Asserting the manifest equals a hardcoded literal validates NOTHING: it
      duplicates the manifest and fails on every legitimate addition. The
      manifest is checked against the CODE, never against a copy of itself.

  Known limitation, stated rather than implied: this proves a site EXISTS, not
  that the path RUNS. #4765 was an append on a code path production never took,
  and no static check can see that. The runtime half belongs to the harness
  coverage assertion.
  """
  use ExUnit.Case, async: true

  @specs_dir System.get_env(
               "EMBERVM_SPECS_DIR",
               Path.expand("../../../specs", __DIR__)
             )

  @lib_dir Path.expand("../../lib", __DIR__)

  test "every declared SpecTrace emission site exists and emits its action" do
    {vocabulary, _binding} = Code.eval_file(Path.join(@specs_dir, "vocabulary.exs"))
    sites = Map.fetch!(vocabulary, :spec_trace_sites)

    # Guard the guard: an empty manifest would make every assertion below
    # vacuous, which is the failure mode this whole programme keeps finding.
    assert map_size(sites) > 0,
           "spec_trace_sites is empty, so this test asserts nothing"

    for {action, basename} <- sites do
      matches = Path.wildcard(Path.join([@lib_dir, "**", basename]))

      assert matches != [],
             "spec_trace_sites names #{basename} as the emission site for " <>
               "#{inspect(action)}, but no such file exists under #{@lib_dir}."

      source = Enum.map_join(matches, "\n", &File.read!/1)

      assert Regex.match?(~r/SpecTrace\.emit\(\s*:\w+\s*,\s*:#{action}\b/, source),
             "#{inspect(action)} is declared to be emitted by #{basename}, but that " <>
               "file contains no `SpecTrace.emit(:spec, :#{action}, ...)` call. Either " <>
               "add the emission, or point the manifest at the module that really " <>
               "emits it (NOT spec_trace.ex, which is the transport every action " <>
               "passes through)."
    end
  end

end

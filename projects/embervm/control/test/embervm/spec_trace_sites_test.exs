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

  # The exclusion registry has to be load-bearing or it is decoration, which is
  # the declared-but-unwired shape this repo keeps rediscovering. #4800 will
  # assert the full identity against the spec's prose map:
  #
  #   keys(spec_trace_sites) + keys(spec_trace_excluded) == actions(adoption.tla)
  #
  # That needs a prose-map parser. Until then, assert the half that needs no
  # parser and that catches the realistic drift: an exclusion that has quietly
  # become a site, and an exclusion with no stated reason.
  test "every declared exclusion is disjoint from the sites and carries a reason" do
    {vocabulary, _binding} = Code.eval_file(Path.join(@specs_dir, "vocabulary.exs"))
    sites = Map.fetch!(vocabulary, :spec_trace_sites)
    excluded = Map.fetch!(vocabulary, :spec_trace_excluded)

    assert map_size(excluded) > 0,
           "spec_trace_excluded is empty, so this test asserts nothing"

    overlap = MapSet.intersection(MapSet.new(Map.keys(sites)), MapSet.new(Map.keys(excluded)))

    assert MapSet.size(overlap) == 0,
           "#{inspect(MapSet.to_list(overlap))} is both a declared emission site and a " <>
             "declared exclusion. An exclusion that gained a code site is stale: remove " <>
             "it from spec_trace_excluded, or the manifest claims the action is both " <>
             "observed and deliberately unobserved."

    for {action, reason} <- excluded do
      assert is_binary(reason) and String.length(reason) > 40,
             "exclusion #{inspect(action)} needs a reason stating WHY it is out of scope. " <>
               "An exclusion without a reason is indistinguishable from an omission, which " <>
               "is the thing this registry exists to prevent."
    end
  end

  # #4800. The registry was a LIST OF WHAT WAS BUILT, never a diff against the
  # spec, so it was satisfied by any subset. Nine of adoption.tla's actions were
  # registered and four were not, the two destroy invariants had no data source
  # at all, and every guard passed: the checker over the other nine was green and
  # the endpoint returned 200. A third of a spec unobserved, end to end silent.
  #
  # This closes it by parsing the spec's own prose map and asserting the identity
  # in BOTH directions.
  test "every action in adoption.tla's prose map is observed or deliberately excluded" do
    {vocabulary, _binding} = Code.eval_file(Path.join(@specs_dir, "vocabulary.exs"))
    sites = Map.fetch!(vocabulary, :spec_trace_sites)
    excluded = Map.fetch!(vocabulary, :spec_trace_excluded)
    site_notes = Map.fetch!(vocabulary, :spec_trace_site_notes)

    prose_actions = parse_prose_actions(Path.join(@specs_dir, "adoption.tla"))

    assert MapSet.size(prose_actions) > 10,
           "parsed only #{MapSet.size(prose_actions)} actions from adoption.tla's prose map. " <>
             "The parser has almost certainly broken against a reformat, and a parser that " <>
             "silently matches nothing makes this whole assertion vacuous, which is the " <>
             "exact failure this test exists to prevent. Parsed: " <>
             "#{inspect(MapSet.to_list(prose_actions))}"

    # A site is either the snake_case of a prose action, or carries a note saying
    # what it is instead (a synthetic observation, or a refinement of an action).
    observed = MapSet.new(Map.keys(sites)) |> MapSet.difference(MapSet.new(Map.keys(site_notes)))
    accounted = MapSet.union(observed, MapSet.new(Map.keys(excluded)))

    unobserved = MapSet.difference(prose_actions, accounted)

    assert MapSet.size(unobserved) == 0,
           "adoption.tla models #{inspect(MapSet.to_list(unobserved))} and nothing observes " <>
             "them. Either add an emission site, or add an entry to spec_trace_excluded " <>
             "saying why the action cannot or should not be observed. An action modeled by " <>
             "the spec and reachable by no record is a third of a spec passing silently."

    unmodeled = MapSet.difference(accounted, prose_actions)

    assert MapSet.size(unmodeled) == 0,
           "#{inspect(MapSet.to_list(unmodeled))} is registered as an emission site or an " <>
             "exclusion but matches no action in adoption.tla's prose map. Either it is a " <>
             "synthetic observation, in which case put it in spec_trace_site_notes with an " <>
             "explanation, or the prose map and the code have drifted apart."

    for {site, note} <- site_notes do
      assert Map.has_key?(sites, site),
             "#{inspect(site)} has a site note but is not a declared emission site. A note " <>
               "explaining a site that does not exist is stale."

      assert is_binary(note) and String.length(note) > 40,
             "site note for #{inspect(site)} needs to say what it is if not a prose action."
    end
  end

  # The prose map's entries look like:
  #
  #   (*   AdoptInventory(n)  ~ Dispatcher sweep reconciling node inventory ... *)
  #
  # Two names can share one line ("CrashCP / RestartCP ~ ..."), so match every
  # CamelCase token before the tilde rather than only the first.
  defp parse_prose_actions(spec_path) do
    spec_path
    |> File.read!()
    |> String.split("\n")
    |> Enum.filter(&String.match?(&1, ~r/^\(\*\s+[A-Z][A-Za-z]*.*~/))
    |> Enum.flat_map(fn line ->
      [names | _] = String.split(line, "~", parts: 2)
      Regex.scan(~r/\b([A-Z][A-Za-z]+)\b/, names) |> Enum.map(fn [_, name] -> name end)
    end)
    |> Enum.map(&snake/1)
    |> MapSet.new()
  end

  defp snake(camel) do
    camel
    |> String.replace(~r/([a-z0-9])([A-Z])/, "\\1_\\2")
    |> String.downcase()
  end
end

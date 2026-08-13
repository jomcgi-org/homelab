defmodule Embervm.SpecVocabularyTest do
  @moduledoc """
  Layer-1 vocabulary sync for the adoption spec (ADR embervm/006, layer 1).

  The adoption.tla model claims to model a subset of three implementation
  surfaces: node.proto RPC verbs, NodeRegistry health states, and OpLog kinds.
  Drift defense is deterministic: the manifest projects/embervm/specs/vocabulary.exs
  partitions each live enum into `modeled` and `excluded` buckets, and these tests
  assert the partition stays exact as the implementation evolves. A verb, state,
  or op kind added to the code that lands in neither bucket fails CI and forces a
  human decision (model it in adoption.tla, or exclude it in vocabulary.exs with a
  reason), which is exactly how the spec is kept from rotting into a write-only
  artifact.

  A fourth test cross-checks freshness: every op-log kind the manifest claims the
  spec suite MODELS must appear verbatim in SOME spec `.tla` file. The modeled op
  kinds map to durable taskState / vmState VALUES a spec writes literally
  (submitted, assigned, succeeded, primed in adoption.tla; the session bank/relight
  kinds in bank_relight.tla), so a verbatim-string check is the right invariant
  there and keeps the manifest honest against the actual models. The scan is over
  the whole specs/ directory (adoption.tla and bank_relight.tla today) because the
  ADR 006 pilot now carries two protocols: protocol 1 (VM lifecycle + adoption) and
  protocol 2 (session bank/relight generation pairing). (The proto verbs and health
  states are documented in the spec by the prose map's own action names, e.g.
  RecvStatus for the status verbs, not by the raw RPC-verb string, so the verbatim
  check is scoped to op kinds. The partition tests above are the drift guard for
  those two surfaces.)
  """
  use ExUnit.Case, async: true

  alias Embervm.NodeRegistry
  alias Embervm.OpLog

  # File.cwd!() during `mix test` is control/. The defaults resolve the sibling
  # specs/ and proto/ trees in a plain repo checkout; inside the Bazel sandbox the
  # env vars point at the copies mix_test.sh stages (the spec_files filegroup and
  # node.proto), so the test is hermetic there. Same optional-env shape as
  # MIX_REBAR3_SRC: absent leaves the checkout-relative defaults.
  @specs_dir System.get_env(
               "EMBERVM_SPECS_DIR",
               Path.expand("../../../specs", __DIR__)
             )
  @proto_path System.get_env(
                "EMBERVM_NODE_PROTO",
                Path.expand("../../../proto/embervm/node/v1/node.proto", __DIR__)
              )

  @vocabulary (fn ->
                 {vocab, _binding} =
                   Code.eval_file(Path.join(@specs_dir, "vocabulary.exs"))

                 vocab
               end).()

  # Every rpc verb declared in node.proto: `  rpc VerbName(Request) returns ...`.
  defp actual_proto_rpcs do
    @proto_path
    |> File.read!()
    |> then(&Regex.scan(~r/^\s*rpc\s+(\w+)\s*\(/m, &1))
    |> Enum.map(fn [_full, verb] -> String.to_atom(verb) end)
    |> MapSet.new()
  end

  defp partition(surface) do
    %{modeled: modeled, excluded: excluded} = Map.fetch!(@vocabulary, surface)
    {MapSet.new(modeled), MapSet.new(excluded)}
  end

  defp assert_partitions(surface, actual) do
    {modeled, excluded} = partition(surface)
    declared = MapSet.union(modeled, excluded)

    assert MapSet.disjoint?(modeled, excluded),
           "#{surface}: a name is in BOTH modeled and excluded: " <>
             "#{inspect(MapSet.intersection(modeled, excluded) |> MapSet.to_list())}. " <>
             "Put it in exactly one bucket in projects/embervm/specs/vocabulary.exs."

    unclassified = MapSet.difference(actual, declared)

    assert MapSet.size(unclassified) == 0,
           "#{surface}: new name(s) #{inspect(MapSet.to_list(unclassified))} exist in the " <>
             "implementation but are classified in neither modeled nor excluded. " <>
             "Classify each in projects/embervm/specs/vocabulary.exs: model it in " <>
             "adoption.tla and add it to `modeled`, or add it to `excluded` with a reason " <>
             "(per ADR embervm/006, the adoption pilot models only the VM-lifecycle + " <>
             "adoption protocol; serving/stateful/group/session/continuity are out of scope)."

    stale = MapSet.difference(declared, actual)

    assert MapSet.size(stale) == 0,
           "#{surface}: name(s) #{inspect(MapSet.to_list(stale))} appear in vocabulary.exs " <>
             "but no longer exist in the implementation. Remove them from " <>
             "projects/embervm/specs/vocabulary.exs."
  end

  test "proto_rpcs: modeled + excluded exactly partition node.proto's rpc verbs" do
    assert_partitions(:proto_rpcs, actual_proto_rpcs())
  end

  test "health_states: modeled + excluded exactly partition NodeRegistry.health_states/0" do
    assert_partitions(:health_states, MapSet.new(NodeRegistry.health_states()))
  end

  test "op_kinds: modeled + excluded exactly partition OpLog.kinds/0" do
    assert_partitions(:op_kinds, MapSet.new(OpLog.kinds()))
  end

  test "freshness: every modeled op kind's name appears verbatim in some spec .tla" do
    # Scan the whole spec suite (adoption.tla + bank_relight.tla today): the ADR 006
    # pilot now carries two protocols, so a modeled op kind may be documented in
    # either spec's prose map or actions. A single concatenated corpus is the right
    # granularity, since the manifest models the vocabulary across the suite, not
    # per file.
    corpus =
      @specs_dir
      |> Path.join("*.tla")
      |> Path.wildcard()
      |> Enum.map_join("\n", &File.read!/1)

    {modeled, _excluded} = partition(:op_kinds)

    for kind <- modeled do
      assert String.contains?(corpus, Atom.to_string(kind)),
             "op kind #{inspect(kind)} is marked modeled in vocabulary.exs but its name does " <>
               "not appear in any projects/embervm/specs/*.tla. Either no spec models it (move " <>
               "it to `excluded` in projects/embervm/specs/vocabulary.exs with a reason) or a " <>
               "spec's prose map / actions must be updated to mention it (ADR embervm/006 " <>
               "layer 1: the manifest must not claim to model what the specs do not)."
    end
  end

  # Strip `#` comments and @doc/@moduledoc heredocs before looking for an atom.
  # Both are why the naive greps failed: `:primed` appeared in a base_builder
  # comment, and async_writer's @moduledoc names four kinds it does not append.
  defp executable_source(path) do
    path
    |> File.read!()
    |> then(&Regex.replace(~r/@(?:moduledoc|doc|typedoc)\s+"""(?:.|\n)*?"""/, &1, ""))
    |> then(&Regex.replace(~r/@(?:moduledoc|doc|typedoc)\s+"(?:[^"\\]|\\.)*"/, &1, ""))
    |> String.split("\n")
    |> Enum.reject(&String.starts_with?(String.trim_leading(&1), "#"))
    |> Enum.join("\n")
  end

  # Layer 1's #4756 guard: every MODELED op kind must be appended by the module
  # vocabulary.exs names. See the op_kind_sites comment there for why this is a
  # declared registry and not a grep (both greps were tried; one was vacuous,
  # the other failed 8 kinds that are genuinely emitted).
  test "every modeled op kind has a declared, real append site" do
    lib_dir = Path.expand("../../lib", __DIR__)
    {modeled, _excluded} = partition(:op_kinds)
    sites = Map.fetch!(@vocabulary, :op_kind_sites)
    declared = MapSet.new(Map.keys(sites))

    # The registry must PARTITION the modeled kinds: a kind added to `modeled`
    # with no declared site fails here rather than being silently unchecked.
    assert MapSet.equal?(declared, modeled),
           "op_kind_sites must name an append site for exactly the modeled op " <>
             "kinds. Missing: #{inspect(MapSet.to_list(MapSet.difference(modeled, declared)))}. " <>
             "Extra: #{inspect(MapSet.to_list(MapSet.difference(declared, modeled)))}."

    for {kind, basename} <- sites do
      matches = Path.wildcard(Path.join([lib_dir, "**", basename]))

      assert matches != [],
             "op_kind_sites names #{basename} as the append site for " <>
               "#{inspect(kind)}, but no such file exists under #{lib_dir}."

      source = Enum.map_join(matches, "\n", &executable_source/1)

      assert Regex.match?(~r/:#{kind}\b/, source),
             "modeled op kind #{inspect(kind)} is declared to be appended by " <>
               "#{basename}, but that file never mentions it outside comments " <>
               "and docs. Either add the append, or move the kind to `excluded` " <>
               "in vocabulary.exs with a reason (see #4756)."
    end
  end
end

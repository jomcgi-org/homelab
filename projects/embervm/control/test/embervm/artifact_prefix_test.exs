defmodule Embervm.ArtifactPrefixTest do
  use ExUnit.Case, async: true

  alias Embervm.ArtifactPrefix

  test "prefixes match noded artifactPrefix for every principal kind" do
    assert ArtifactPrefix.prefix(:ARTIFACT_KIND_SESSION, "sbx", "s1", "intel", nil) ==
             "session/intel/sbx/s1"

    assert ArtifactPrefix.prefix(
             :ARTIFACT_KIND_SESSION_WORKSPACE,
             "sbx",
             "ignored",
             "intel",
             "lineage-1"
           ) == "session-workspace/sbx/lineage-1"

    assert ArtifactPrefix.prefix(:ARTIFACT_KIND_STATEFUL, "pg", "r1", "intel", nil) ==
             "stateful/intel/pg/r1"

    assert ArtifactPrefix.prefix(:ARTIFACT_KIND_VOLUME, "pg", "", "intel", nil) ==
             "volume/pg"

    assert ArtifactPrefix.prefix(:ARTIFACT_KIND_GROUP_SET, "grp", "set1", "intel", nil) ==
             "group_set/intel/grp/set1"

    assert ArtifactPrefix.prefix(:ARTIFACT_KIND_SERVING, "hot", "sv1", "intel", nil) ==
             "serving/intel/hot/sv1"
  end

  test "unknown kinds and missing vendor bindings are refused" do
    assert ArtifactPrefix.prefix("base", "echo", "b1", "intel", nil) == nil
    assert ArtifactPrefix.prefix(:ARTIFACT_KIND_SESSION, "sbx", "s1", "", nil) == nil
  end
end

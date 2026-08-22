defmodule Embervm.K8sTest do
  @moduledoc """
  Unit tests for the parts of `Embervm.K8s` that are testable without a live
  apiserver: TokenReview response parsing and the NDJSON line framing that
  reassembles watch events across TCP chunk boundaries. The streaming and
  request plumbing itself is exercised in-cluster (the deploy-verify step), not
  here.
  """
  use ExUnit.Case, async: true

  alias Embervm.K8s
  alias Embervm.Auth.Identity

  describe "parse_review/2" do
    test "returns the complete bound pod identity" do
      body =
        review_body(%{
          "authentication.kubernetes.io/pod-uid" => ["pod-uid-1"],
          "authentication.kubernetes.io/pod-name" => ["embervm-brick-1"],
          "authentication.kubernetes.io/node-name" => ["node-4"]
        })

      assert {:ok,
              %Identity{
                username: "system:serviceaccount:embervm:noded",
                pod_uid: "pod-uid-1",
                pod_name: "embervm-brick-1",
                node_name: "node-4"
              }} = K8s.parse_review(201, body)
    end

    test "returns nil bound fields when extra is absent" do
      body = review_body(:absent)

      assert {:ok,
              %Identity{
                username: "system:serviceaccount:embervm:noded",
                pod_uid: nil,
                pod_name: nil,
                node_name: nil
              }} = K8s.parse_review(200, body)
    end

    test "returns nil pod_uid when other extras are present" do
      body =
        review_body(%{
          "authentication.kubernetes.io/pod-name" => ["embervm-brick-1"],
          "authentication.kubernetes.io/node-name" => ["node-4"]
        })

      assert {:ok,
              %Identity{
                username: "system:serviceaccount:embervm:noded",
                pod_uid: nil,
                pod_name: "embervm-brick-1",
                node_name: "node-4"
              }} = K8s.parse_review(201, body)
    end
  end

  describe "frame_ndjson/2" do
    test "splits complete lines and returns no remainder when the chunk ends on a newline" do
      assert {["a", "b"], ""} = K8s.frame_ndjson("", "a\nb\n")
    end

    test "keeps a trailing partial line as the remainder" do
      assert {["a"], "b"} = K8s.frame_ndjson("", "a\nb")
    end

    test "prepends the prior buffer so a line split across two chunks reassembles" do
      # First chunk ends mid-line; its remainder is fed back in as the buffer.
      {lines1, rest1} = K8s.frame_ndjson("", ~s({"type":"ADD))
      assert lines1 == []
      assert rest1 == ~s({"type":"ADD)

      {lines2, rest2} = K8s.frame_ndjson(rest1, ~s(ED"}\n))
      assert lines2 == [~s({"type":"ADDED"})]
      assert rest2 == ""
    end

    test "handles multiple complete lines in one chunk with a trailing partial" do
      assert {["one", "two"], "thr"} = K8s.frame_ndjson("", "one\ntwo\nthr")
    end

    test "an empty chunk with a buffered partial yields no lines and preserves the buffer" do
      assert {[], "partial"} = K8s.frame_ndjson("partial", "")
    end

    test "a blank line survives framing (the caller skips it, not the framer)" do
      assert {["", "x"], ""} = K8s.frame_ndjson("", "\nx\n")
    end
  end

  # EndpointSlice discovery (endpoints_from_slices/2) was RETIRED in R0 PR-2:
  # noded instances dial home to /v1/nodes/register instead of being listed. The
  # registration path is exercised in node_registry_test.exs and router_test.exs.

  describe "workloads_path/0" do
    # The informer read the CLUSTER-WIDE collection while WorkloadWatcher keys
    # its catalog on name alone, so two control planes collapsed same-named
    # Workloads into one entry, last write wins, and each patched .status onto
    # the other's CR. Namespaces already prevent that; the control plane was the
    # only thing opting out of them.
    #
    # This asserts the mechanism rather than the symptom: a path without a
    # /namespaces/<ns>/ segment is the bug, whatever names happen to be in use.
    test "reads the collection scoped to a namespace, never cluster-wide" do
      path = K8s.workloads_path()

      assert path =~ ~r{^/apis/embervm\.dev/v1alpha1/namespaces/[^/]+/workloads$},
             "expected a namespaced collection path, got #{inspect(path)}. A path " <>
               "without a /namespaces/<ns>/ segment lists every Workload in the " <>
               "cluster, which is what let a dev control plane patch status onto " <>
               "production's CRs."

      refute path == "/apis/embervm.dev/v1alpha1/workloads"
    end

    test "uses this pod's own namespace" do
      # Outside a cluster the ServiceAccount namespace file is absent and
      # namespace/0 falls back to "default", which is the value under test here.
      # The point is that the path is DERIVED from it rather than hardcoded.
      assert K8s.workloads_path() == "/apis/embervm.dev/v1alpha1/namespaces/#{K8s.namespace()}/workloads"
    end
  end

  defp review_body(extra) do
    user = %{"username" => "system:serviceaccount:embervm:noded"}
    user = if extra == :absent, do: user, else: Map.put(user, "extra", extra)

    :json.encode(%{"status" => %{"authenticated" => true, "user" => user}})
    |> :erlang.iolist_to_binary()
  end
end

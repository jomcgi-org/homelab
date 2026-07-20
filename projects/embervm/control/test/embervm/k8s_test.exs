defmodule Embervm.K8sTest do
  @moduledoc """
  Unit tests for the parts of `Embervm.K8s` that are testable without a live
  apiserver: the NDJSON line framing that reassembles watch events across TCP
  chunk boundaries. The streaming/auth/request plumbing itself is exercised
  in-cluster (the deploy-verify step), not here.
  """
  use ExUnit.Case, async: true

  alias Embervm.K8s

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
end

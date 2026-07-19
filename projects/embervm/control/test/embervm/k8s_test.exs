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

  describe "endpoints_from_slices/2 (headless discovery feeds the node list)" do
    test "flattens EndpointSlices into a per-pod node list keyed by node name" do
      slices = [
        %{
          "endpoints" => [
            %{"addresses" => ["10.0.1.5"], "nodeName" => "node-1", "targetRef" => %{"name" => "embervm-noded-aaa"}},
            %{"addresses" => ["10.0.2.6"], "nodeName" => "node-2", "targetRef" => %{"name" => "embervm-noded-bbb"}}
          ]
        }
      ]

      assert [
               %{id: "node-1", address: "10.0.1.5:9090"},
               %{id: "node-2", address: "10.0.2.6:9090"}
             ] = K8s.endpoints_from_slices(slices, 9090)
    end

    test "falls back to pod name then IP when nodeName is absent" do
      slices = [
        %{"endpoints" => [%{"addresses" => ["10.0.3.7"], "targetRef" => %{"name" => "embervm-noded-ccc"}}]},
        %{"endpoints" => [%{"addresses" => ["10.0.4.8"]}]}
      ]

      assert [
               %{id: "embervm-noded-ccc", address: "10.0.3.7:9090"},
               %{id: "10.0.4.8", address: "10.0.4.8:9090"}
             ] = K8s.endpoints_from_slices(slices, 9090)
    end

    test "skips endpoints with no address and yields [] for empty slices" do
      assert [] = K8s.endpoints_from_slices([], 9090)
      assert [] = K8s.endpoints_from_slices([%{"endpoints" => [%{"addresses" => []}]}], 9090)
    end

    test "spans endpoints across multiple slices (large services shard into several)" do
      slices = [
        %{"endpoints" => [%{"addresses" => ["10.0.1.5"], "nodeName" => "node-1"}]},
        %{"endpoints" => [%{"addresses" => ["10.0.2.6"], "nodeName" => "node-2"}]}
      ]

      result = K8s.endpoints_from_slices(slices, 9090)
      assert length(result) == 2
      assert %{id: "node-1", address: "10.0.1.5:9090"} in result
      assert %{id: "node-2", address: "10.0.2.6:9090"} in result
    end
  end
end

defmodule Embervm.SessionTraceTest do
  @moduledoc """
  Unit coverage for the W3C-traceparent plumbing that nests the session invoke/
  bank/relight spans (Task 9). No OTel exporter runs in CI (traces_exporter: :none),
  so `current_traceparent/0` returns nil (no recording span) and these tests pin the
  pure parsing + the never-crash guards; the actual span nesting is an integration
  property verified live in the trace backend per the acceptance criteria.
  """
  use ExUnit.Case, async: true

  alias Embervm.SessionTrace

  describe "parse_traceparent/1" do
    test "parses a well-formed W3C traceparent into integer ids" do
      tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
      assert {trace_id, span_id, flags} = SessionTrace.parse_traceparent(tp)
      assert trace_id == 0x4BF92F3577B34DA6A3CE929D0E0E4736
      assert span_id == 0x00F067AA0BA902B7
      assert flags == 1
    end

    test "rejects malformed / wrong-length / non-hex / nil traceparents" do
      assert SessionTrace.parse_traceparent(nil) == :error
      assert SessionTrace.parse_traceparent("") == :error
      assert SessionTrace.parse_traceparent("not-a-traceparent") == :error
      # trace id too short
      assert SessionTrace.parse_traceparent("00-abcd-00f067aa0ba902b7-01") == :error
      # non-hex span id
      assert SessionTrace.parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-zzzzzzzzzzzzzzzz-01") == :error
    end
  end

  describe "current_traceparent/0" do
    test "is nil when no span is recording (the CI/no-exporter case)" do
      assert SessionTrace.current_traceparent() == nil
    end
  end

  describe "restore_parent/1" do
    test "is a no-op (never raises) for nil and malformed traceparents" do
      assert SessionTrace.restore_parent(nil) == :ok
      assert SessionTrace.restore_parent("garbage") == :ok
      assert SessionTrace.restore_parent("") == :ok
    end

    test "accepts a well-formed traceparent without raising" do
      assert SessionTrace.restore_parent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01") == :ok
    end
  end

  test "key/0 is the traceparent map key" do
    assert SessionTrace.key() == "traceparent"
  end
end

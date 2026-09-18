defmodule Embervm.SessionApiPolicyTest do
  use ExUnit.Case, async: true

  import Plug.Test

  alias Embervm.SessionApiPolicy

  @allowed [
    {"POST", "/v1/workloads/claude-runtime/sessions"},
    {"GET", "/v1/workloads/claude-runtime/sessions"},
    {"POST", "/v1/sessions/s-0123_ABCD/invoke"},
    {"GET", "/v1/sessions/s-0123_ABCD"},
    {"DELETE", "/v1/sessions/s-0123_ABCD"}
  ]

  test "permits every supported session method and canonical path" do
    for {method, path} <- @allowed do
      conn = conn(method, path) |> SessionApiPolicy.call([])
      refute conn.halted, "expected #{method} #{path} to pass the policy"
    end
  end

  test "does not interfere with probes or unrelated control-plane routes" do
    for {method, path} <- [{"GET", "/healthz"}, {"GET", "/livez"}, {"GET", "/v1/nodes"}] do
      assert :outside == SessionApiPolicy.classify(conn(method, path))
    end
  end

  test "denies unsupported verbs before router dispatch" do
    for method <- ["CONNECT", "HEAD", "OPTIONS", "PATCH", "PUT"] do
      response = conn(method, "/v1/sessions/s-1/invoke") |> SessionApiPolicy.call([])
      assert response.halted
      assert response.status == 403
    end
  end

  test "denies non-canonical and expanded session paths" do
    paths = [
      "/v1/sessions/s-1/invoke/",
      "/v1/sessions/s-1/invoke/extra",
      "/v1/sessions/../nodes",
      "/v1/sessions/s-1%2Finvoke",
      "/v1%2Fsessions/s-1/invoke",
      "/v1/%73essions/s-1/invoke",
      "/v1//sessions/s-1/invoke",
      "/v1/workloads/claude-runtime/%73essions",
      "/v1/workloads/claude-runtime%2Fsessions"
    ]

    for path <- paths do
      response = conn("POST", path) |> SessionApiPolicy.call([])
      assert response.halted, "expected POST #{path} to be denied"
      assert response.status == 403
    end
  end
end

defmodule Embervm.RouterDurabilityTest do
  @moduledoc """
  Request tests for GET /v1/health/durability (#4338), driving the LIVE Bandit
  instance the application boots (same harness as RouterTest) with an injected
  fake authenticator and an injected durability module (the router's
  `:embervm :durability` app-env seam, mirroring the `:authenticator` seam).
  `async: false` because these mutate global application env and share the one
  running control-plane instance.
  """

  use ExUnit.Case, async: false

  defmodule FakeAuth do
    # The monolith service account: the caller the durability flip would use
    # (it is already on the embervm auth allow-list in deploy values).
    def authenticate("good"), do: {:ok, "system:serviceaccount:monolith:monolith"}
    def authenticate(_), do: {:error, :unauthenticated}
  end

  # A healthy report shaped exactly like Embervm.Durability.evaluate's output.
  @healthy_report %{
    ok: true,
    evaluated_at_unix_ms: 1_756_000_000_000,
    tier1: %{
      ok: true,
      verdict: :ok,
      detail: "all tracked artifact kinds have confirmed store copies",
      streaks: %{base: 0, group_set: 0, serving: 0, session: 0, stateful: 0},
      failing_kinds: [],
      threshold_rounds: 10,
      fresh_nodes: ["node-1"],
      missing_nodes: []
    },
    tier2: %{
      ok: true,
      verdict: :ok,
      detail: "newest gc-manifests object is fresh",
      newest_manifest_age_ms: 3_600_000,
      stall_bound_ms: 90_000_000
    }
  }

  defmodule StubDurability do
    def snapshot do
      case Application.get_env(:embervm, :durability_stub_report, :suspended) do
        :raise -> {:error, :s3_down}
        other -> other
      end
    end
  end

  setup do
    Application.put_env(:embervm, :authenticator, FakeAuth)
    on_exit(fn ->
      Application.delete_env(:embervm, :authenticator)
      Application.delete_env(:embervm, :durability)
      Application.delete_env(:embervm, :durability_stub_report)
    end)

    :ok
  end

  defp req(method, path, headers \\ []) do
    Finch.build(method, "http://127.0.0.1:8080#{path}", headers)
    |> Finch.request(Embervm.Finch)
    |> then(fn {:ok, resp} -> resp end)
  end

  defp auth(token), do: [{"authorization", "Bearer " <> token}]

  test "with the detector dark (no seam wired) the route reads as absent" do
    resp = req(:get, "/v1/health/durability", auth("good"))
    assert resp.status == 404
  end

  test "a healthy report answers 200 with both tiers ok" do
    Application.put_env(:embervm, :durability, StubDurability)
    Application.put_env(:embervm, :durability_stub_report, @healthy_report)

    resp = req(:get, "/v1/health/durability", auth("good"))
    assert resp.status == 200

    body = :json.decode(resp.body)
    assert body["ok"] == true
    assert body["tier1"]["verdict"] == "ok"
    assert body["tier2"]["verdict"] == "ok"
  end

  test "an unhealthy report answers 503 with the SAME report body (never green)" do
    Application.put_env(:embervm, :durability, StubDurability)

    not_ok = %{
      @healthy_report
      | ok: false,
        tier1: %{
          @healthy_report.tier1
          | ok: false,
            verdict: :export_failure_streak,
            failing_kinds: [:session],
            detail: "artifact exports have been failing"
        }
    }

    Application.put_env(:embervm, :durability_stub_report, not_ok)

    resp = req(:get, "/v1/health/durability", auth("good"))
    assert resp.status == 503
    body = :json.decode(resp.body)
    assert body["ok"] == false
    assert body["tier1"]["verdict"] == "export_failure_streak"
    assert body["tier1"]["failing_kinds"] == ["session"]
  end

  test "an evaluator crash answers 503 unknown rather than 500 or green" do
    Application.put_env(:embervm, :durability, StubDurability)
    Application.put_env(:embervm, :durability_stub_report, :raise)

    resp = req(:get, "/v1/health/durability", auth("good"))
    assert resp.status == 503
    body = :json.decode(resp.body)
    assert body["ok"] == false
  end

  test "the route requires the management bearer token like every /v1 route" do
    Application.put_env(:embervm, :durability, StubDurability)
    Application.put_env(:embervm, :durability_stub_report, @healthy_report)

    resp = req(:get, "/v1/health/durability")
    assert resp.status == 401
  end
end
